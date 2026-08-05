const { spawn } = require("node:child_process");
const { once } = require("node:events");
const fs = require("node:fs");
const net = require("node:net");
const path = require("node:path");

const { expect, test } = require("@playwright/test");

const ROOT = path.resolve(__dirname, "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");
const DEMO_CREDENTIAL = "s01-registered-demo-test-credential";
const OPERATOR_CREDENTIAL = "s01-registered-operator-test-credential";
const SCENARIO = "app_r53_bad_engine.json";
const REACT_URL = "/controlled/s01/react";

async function reservePort() {
  const server = net.createServer();
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const port = server.address().port;
  await new Promise((resolve, reject) =>
    server.close((error) => (error ? reject(error) : resolve())),
  );
  return port;
}

async function startServer() {
  const port = await reservePort();
  const statePath = path.join(
    "/tmp",
    `xiaopeng-task4-t01-react-${process.pid}-${port}-${Date.now()}.sqlite3`,
  );
  const output = [];
  const child = spawn(
    PYTHON,
    [
      "-m",
      "uvicorn",
      "tests.test_s07_http:create_s07_test_app",
      "--factory",
      "--host",
      "127.0.0.1",
      "--port",
      String(port),
      "--log-level",
      "warning",
    ],
    {
      cwd: ROOT,
      env: {
        ...process.env,
        NO_PROXY: "127.0.0.1,localhost",
        no_proxy: "127.0.0.1,localhost",
        PYTHONDONTWRITEBYTECODE: "1",
        PYTHONPYCACHEPREFIX: "/tmp/xiaopeng-task4-t01-react-pycache",
        TASK4_S01_STATE_PATH: statePath,
        TASK4_S01_TEST_STATE_PATH: statePath,
        TASK4_S01_TEST_BACKGROUND_ENABLED: "0",
        TASK4_S07_TEST_VERIFIER: "verified",
        TASK4_S01_DEMO_CREDENTIAL: DEMO_CREDENTIAL,
        TASK4_S01_DEMO_SUBJECT: "t01-browser-reviewer",
        TASK4_S01_OPERATOR_CREDENTIAL: OPERATOR_CREDENTIAL,
        TASK4_S01_OPERATOR_SUBJECT: "t01-browser-operator",
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  child.stdout.on("data", (chunk) => output.push(chunk.toString()));
  child.stderr.on("data", (chunk) => output.push(chunk.toString()));

  const baseURL = `http://127.0.0.1:${port}`;
  const deadline = Date.now() + 8_000;
  while (Date.now() < deadline && child.exitCode === null) {
    try {
      if ((await fetch(`${baseURL}/api/health`)).ok) {
        return { baseURL, child, output };
      }
    } catch (_) {
      // The bounded readiness loop owns the retry.
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  child.kill("SIGKILL");
  throw new Error(`T01 React server did not start: ${output.join("")}`);
}

async function stopServer(server) {
  if (server.child.exitCode !== null) return;
  server.child.kill("SIGTERM");
  const exited = once(server.child, "exit");
  if (
    (await Promise.race([
      exited,
      new Promise((resolve) => setTimeout(resolve, 5_000, "timeout")),
    ])) === "timeout"
  ) {
    server.child.kill("SIGKILL");
    await once(server.child, "exit");
  }
}

function restrictedStrings() {
  const fixture = JSON.parse(
    fs.readFileSync(path.join(ROOT, "fixtures", "applications", SCENARIO), "utf8"),
  );
  const values = [fixture.application_id];
  for (const document of fixture.documents || []) {
    for (const field of Object.values(document.fields || {})) {
      if (typeof field.raw === "string" && field.raw.length > 1) values.push(field.raw);
    }
  }
  return values;
}

async function installRecoveryWork(baseURL, reviewer) {
  const admission = await reviewer.request.post(
    `${baseURL}/controlled/s01/api/commands/submit`,
    { data: { scenario_id: SCENARIO, idempotency_key: "t01-react-admission" } },
  );
  expect(admission.ok()).toBeTruthy();
  const failed = await reviewer.request.post(
    `${baseURL}/controlled/s01/api/_test/commands/process`,
    { data: { worker_id: "t01-react-failure", now: 10 } },
  );
  expect(failed.ok()).toBeTruthy();
  const body = await failed.json();
  expect(body.status).toBe("blocked");
  return body.recovery_work_id;
}

test("T01 production tracer: queue discovery, stale reload, one accepted VerifyRecovery, server-owned gate", async ({
  browser,
}) => {
  const server = await startServer();
  const reviewerContext = await browser.newContext({
    viewport: { width: 1280, height: 800 },
    extraHTTPHeaders: { Authorization: `Bearer ${DEMO_CREDENTIAL}` },
  });
  const operatorContext = await browser.newContext({
    viewport: { width: 1280, height: 800 },
    extraHTTPHeaders: { Authorization: `Bearer ${OPERATOR_CREDENTIAL}` },
  });
  const reviewer = await reviewerContext.newPage();
  const operator = await operatorContext.newPage();
  const browserErrors = [];
  reviewer.on("pageerror", (error) => browserErrors.push(error.message));
  operator.on("pageerror", (error) => browserErrors.push(error.message));
  const restricted = restrictedStrings();

  let verifyPosts = 0;
  let acceptedPosts = 0;
  let stalePosts = 0;
  const verifyBodies = [];
  const countVerify = (page) =>
    page.on("request", (request) => {
      const url = new URL(request.url());
      if (
        request.method() === "POST" &&
        url.pathname.includes("/commands/recovery-work-items/") &&
        url.pathname.endsWith("/verify")
      ) {
        verifyPosts += 1;
        verifyBodies.push(request.postDataJSON());
      }
    });

  try {
    const shellResponse = await reviewer.goto(`${server.baseURL}${REACT_URL}`, {
      waitUntil: "networkidle",
    });
    expect(shellResponse.status()).toBe(200);
    expect(shellResponse.headers()["cache-control"]).toContain("no-store");
    await expect(reviewer.getByTestId("queue-panel")).toBeVisible();
    await expect(reviewer.getByTestId("queue-empty")).toBeVisible();

    const workId = await installRecoveryWork(server.baseURL, reviewer);
    await reviewer.reload({ waitUntil: "networkidle" });
    const queueItem = reviewer.getByRole("link", { name: new RegExp(workId) });
    await expect(queueItem).toBeVisible();
    const restrictedInQueue = await reviewer
      .getByTestId("queue-panel")
      .innerText();
    for (const value of restricted) expect(restrictedInQueue).not.toContain(value);

    countVerify(reviewer);
    // Keyboard operation: Tab reaches the queue link with visible focus and
    // Enter opens the work; the panel heading receives focus.
    await reviewer.keyboard.press("Tab");
    await expect(reviewer.locator(":focus")).toHaveAttribute(
      "href",
      expect.stringContaining(encodeURIComponent(workId)),
    );
    expect(
      await reviewer.evaluate(() => {
        const element = document.activeElement;
        if (!(element instanceof HTMLElement)) return false;
        const style = window.getComputedStyle(element);
        return style.outlineStyle !== "none" || style.boxShadow !== "none";
      }),
    ).toBe(true);
    await reviewer.keyboard.press("Enter");
    await expect(reviewer.getByTestId("recovery-panel")).toBeVisible();
    await expect(reviewer.locator(":focus")).toHaveText("恢复工作");
    await expect(reviewer.getByTestId("recovery-status")).toHaveText("open");
    await expect(reviewer.getByTestId("recovery-phase")).toHaveText("Unprocessable");
    await expect(reviewer.getByTestId("recovery-route")).toHaveText("unprocessable");
    await expect(reviewer.getByTestId("recovery-primary-reason")).toHaveText(
      "configuration.checker_unavailable",
    );
    await expect(reviewer.getByTestId("recovery-related-reasons")).toHaveText("None");
    await expect(reviewer.getByTestId("recovery-operation")).toHaveText(
      "execute_check_run",
    );
    await expect(reviewer.getByTestId("recovery-dependency")).toHaveText(
      "c-demo-target-checker",
    );
    await expect(reviewer.getByTestId("recovery-attempts")).toContainText(
      "1 · terminal · blocked",
    );
    await expect(reviewer.getByTestId("recovery-responsible-party")).toHaveText(
      "policy_owner",
    );
    await expect(reviewer.getByTestId("recovery-action")).toHaveText(
      "restore_exact_release_or_activate_compatible_successor",
    );
    await expect(reviewer.getByTestId("recovery-target")).toHaveText("Evidence Ready");
    await expect(reviewer.getByTestId("recovery-criterion-id")).toHaveText(
      "s07-checker-compatibility/1",
    );
    await expect(reviewer.getByTestId("recovery-criterion-digest")).toHaveText(
      /^[0-9a-f]{64}$/,
    );
    await expect(
      reviewer.getByRole("button", { name: "验证恢复" }),
    ).toBeDisabled();
    const reviewerText = await reviewer.locator("body").innerText();
    for (const value of restricted) expect(reviewerText).not.toContain(value);

    const authorityResponse = await operator.request.get(
      `${server.baseURL}/controlled/s01/api/queries/recovery-work-items/${encodeURIComponent(workId)}`,
    );
    expect(authorityResponse.ok()).toBeTruthy();
    const authority = await authorityResponse.json();
    const authorityText = JSON.stringify(authority);
    for (const value of restricted) expect(authorityText).not.toContain(value);

    let staleProjectionDelivered = false;
    await operator.route(
      `**/controlled/s01/api/queries/recovery-work-items/${encodeURIComponent(workId)}`,
      async (route) => {
        const response = await route.fetch();
        const projected = await response.json();
        if (!staleProjectionDelivered) {
          projected.lifecycle_revision -= 1;
          staleProjectionDelivered = true;
        }
        await route.fulfill({ response, json: projected });
      },
    );

    countVerify(operator);
    await operator.goto(
      `${server.baseURL}${REACT_URL}?work=${encodeURIComponent(workId)}`,
      { waitUntil: "networkidle" },
    );
    await expect(operator.getByTestId("recovery-panel")).toBeVisible();
    await expect(operator.getByTestId("recovery-lifecycle-revision")).toHaveText(
      String(authority.lifecycle_revision - 1),
    );
    await operator.getByRole("button", { name: "验证恢复" }).click();
    await expect(operator.getByTestId("recovery-command-status")).toContainText(
      "recovery.context_changed",
    );
    await expect(operator.getByTestId("recovery-status")).toHaveText("open");
    await expect(operator.getByTestId("recovery-phase")).toHaveText("Unprocessable");
    await expect(
      operator.getByRole("button", { name: "验证恢复" }),
    ).toBeDisabled();

    await operator.unroute(
      `**/controlled/s01/api/queries/recovery-work-items/${encodeURIComponent(workId)}`,
    );
    // Authoritative reload and the accepted command are both keyboard-driven.
    await operator.keyboard.press("Tab");
    await expect(operator.locator(":focus")).toHaveText("重新加载");
    await operator.keyboard.press("Enter");
    await expect(operator.getByTestId("recovery-lifecycle-revision")).toHaveText(
      String(authority.lifecycle_revision),
    );
    await expect(operator.getByTestId("recovery-watermark")).toHaveText(
      String(authority.projection_watermark),
    );

    await operator.keyboard.press("Tab");
    await expect(operator.locator(":focus")).toHaveText("验证恢复");
    await operator.keyboard.press("Enter");
    await expect(operator.getByTestId("recovery-command-status")).toHaveText(
      "恢复事实已接受",
    );
    await expect(operator.getByTestId("recovery-status")).toHaveText("resolved");
    await expect(operator.getByTestId("recovery-phase")).toHaveText("Evidence Ready");
    await expect(operator.getByTestId("recovery-route")).toHaveText("pending_check");
    await expect(operator.getByTestId("recovery-fact-count")).toHaveText("1");
    await expect(operator.getByTestId("recovery-resolution-count")).toHaveText("1");
    await expect(
      operator.getByRole("button", { name: "验证恢复" }),
    ).toBeDisabled();

    await reviewer.goto(
      `${server.baseURL}${REACT_URL}?work=${encodeURIComponent(workId)}`,
      { waitUntil: "networkidle" },
    );
    await expect(reviewer.getByTestId("recovery-status")).toHaveText("resolved");
    await expect(reviewer.getByTestId("recovery-phase")).toHaveText("Evidence Ready");
    await expect(reviewer.getByTestId("gate-phase")).toHaveText("Evidence Ready");
    await expect(reviewer.getByTestId("gate-route")).toHaveText("pending_check");
    await expect(reviewer.getByTestId("gate-currentness")).toHaveText(
      "NO_CURRENT_RUN",
    );

    expect(verifyPosts).toBe(2);
    expect(verifyBodies).toHaveLength(2);
    for (const body of verifyBodies) {
      expect(Object.keys(body).sort()).toEqual([
        "expected_criterion_digest",
        "expected_lifecycle_revision",
        "idempotency_key",
      ]);
      const serializedBody = JSON.stringify(body);
      expect(serializedBody).not.toContain("target");
      expect(serializedBody).not.toContain("verifier");
      expect(serializedBody).not.toContain("recovered");
    }

    const operatorText = await operator.locator("body").innerText();
    expect(operatorText).not.toContain("Manual Review");
    expect(operatorText).not.toContain("Verification Completed");
    for (const value of restricted) expect(operatorText).not.toContain(value);

    const reviewerUrl = new URL(reviewer.url());
    expect(reviewerUrl.pathname).toBe(REACT_URL);
    for (const value of restricted) expect(reviewerUrl.search).not.toContain(value);
    expect(await reviewer.evaluate(() => localStorage.length)).toBe(0);
    expect(await reviewer.evaluate(() => sessionStorage.length)).toBe(0);

    const assetRequests = await reviewer.evaluate(async () => {
      const scripts = [...document.querySelectorAll("script[src]")].map(
        (node) => node.src,
      );
      return scripts;
    });
    expect(assetRequests.length).toBeGreaterThan(0);
    for (const asset of assetRequests) {
      expect(new URL(asset).pathname.startsWith("/static/react/")).toBeTruthy();
    }
    const assetsBundle = await Promise.all(
      assetRequests.map((asset) =>
        reviewer.request.get(new URL(asset, server.baseURL).toString()),
      ),
    );
    for (const assetResponse of assetsBundle) {
      expect(assetResponse.ok()).toBeTruthy();
      expect(assetResponse.headers()["cache-control"]).toContain("immutable");
    }
    const bundleText = (await Promise.all(assetsBundle.map((r) => r.text()))).join("");
    for (const value of restricted) expect(bundleText).not.toContain(value);
    expect(bundleText).not.toContain(DEMO_CREDENTIAL);
    expect(bundleText).not.toContain(OPERATOR_CREDENTIAL);

    const legacyResponse = await reviewer.goto(`${server.baseURL}/controlled/s01`);
    expect(legacyResponse.status()).toBe(200);

    await operator.setViewportSize({ width: 390, height: 844 });
    await operator.goto(
      `${server.baseURL}${REACT_URL}?work=${encodeURIComponent(workId)}`,
      { waitUntil: "networkidle" },
    );
    await expect(operator.getByTestId("recovery-panel")).toBeVisible();
    expect(
      await operator.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth + 1,
      ),
    ).toBe(true);

    expect(browserErrors).toEqual([]);
  } finally {
    await reviewerContext.close();
    await operatorContext.close();
    await stopServer(server);
  }
});
