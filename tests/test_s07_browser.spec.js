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
    `xiaopeng-task4-s07-browser-${process.pid}-${port}-${Date.now()}.sqlite3`,
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
        PYTHONPYCACHEPREFIX: "/tmp/xiaopeng-task4-s07-browser-pycache",
        TASK4_S01_STATE_PATH: statePath,
        TASK4_S01_TEST_STATE_PATH: statePath,
        TASK4_S01_TEST_BACKGROUND_ENABLED: "0",
        TASK4_S07_TEST_VERIFIER: "verified",
        TASK4_S01_DEMO_CREDENTIAL: DEMO_CREDENTIAL,
        TASK4_S01_DEMO_SUBJECT: "s07-browser-reviewer",
        TASK4_S01_OPERATOR_CREDENTIAL: OPERATOR_CREDENTIAL,
        TASK4_S01_OPERATOR_SUBJECT: "s07-browser-operator",
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
  throw new Error(`S07 browser server did not start: ${output.join("")}`);
}

async function stopServer(server) {
  if (server.child.exitCode !== null) return;
  server.child.kill("SIGTERM");
  const exited = once(server.child, "exit");
  if ((await Promise.race([exited, new Promise((resolve) => setTimeout(resolve, 5_000, "timeout"))])) === "timeout") {
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

test("Reviewer sees minimized recovery and Operator verifies after authoritative reload", async ({
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

  try {
    expect((await reviewer.goto(`${server.baseURL}/controlled/s01`)).status()).toBe(200);
    await reviewer.getByRole("button", { name: "提交受控场景" }).click();
    await expect(reviewer.getByTestId("receipt-panel")).toBeVisible();
    const failedResponse = await reviewer.request.post(
      `${server.baseURL}/controlled/s01/api/_test/commands/process`,
      { data: { worker_id: "s07-browser-failure", now: 10 } },
    );
    expect(failedResponse.ok()).toBeTruthy();
    const failed = await failedResponse.json();
    expect(failed.status).toBe("blocked");
    const workId = failed.recovery_work_id;
    const recoveryURL = `${server.baseURL}/controlled/s01?recovery=${encodeURIComponent(workId)}`;

    expect((await reviewer.goto(recoveryURL, { waitUntil: "networkidle" })).status()).toBe(200);
    await expect(reviewer.getByTestId("recovery-panel")).toBeVisible();
    await expect(reviewer.getByTestId("boundary-gate")).toContainText("G2");
    await expect(reviewer.getByTestId("recovery-status")).toHaveText("open");
    await expect(reviewer.getByTestId("recovery-phase")).toHaveText("Unprocessable");
    await expect(reviewer.getByTestId("recovery-primary-reason")).toHaveText(
      "configuration.checker_unavailable",
    );
    await expect(reviewer.getByTestId("recovery-related-reasons")).toHaveText("None");
    await expect(reviewer.getByTestId("recovery-operation")).toHaveText("execute_check_run");
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
    await expect(reviewer.getByRole("button", { name: "验证恢复" })).toBeDisabled();
    const reviewerText = await reviewer.locator("body").innerText();
    for (const restricted of restrictedStrings()) expect(reviewerText).not.toContain(restricted);

    const authorityResponse = await operator.request.get(
      `${server.baseURL}/controlled/s01/api/queries/recovery-work-items/${encodeURIComponent(workId)}`,
    );
    expect(authorityResponse.ok()).toBeTruthy();
    const authority = await authorityResponse.json();
    const authorityText = JSON.stringify(authority);
    for (const restricted of restrictedStrings()) expect(authorityText).not.toContain(restricted);
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

    expect((await operator.goto(recoveryURL, { waitUntil: "networkidle" })).status()).toBe(200);
    await expect(operator.getByTestId("recovery-panel")).toBeVisible();
    await expect(operator.getByTestId("recovery-lifecycle-revision")).toHaveText(
      String(authority.lifecycle_revision - 1),
    );
    const staleResponse = operator.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        new URL(response.url()).pathname.endsWith(`/recovery-work-items/${workId}/verify`),
    );
    await operator.getByRole("button", { name: "验证恢复" }).click();
    expect((await staleResponse).status()).toBe(409);
    await expect(operator.getByTestId("recovery-command-status")).toHaveText(
      "recovery.context_changed",
    );
    await expect(operator.getByTestId("recovery-status")).toHaveText("open");
    await expect(operator.getByTestId("recovery-phase")).toHaveText("Unprocessable");

    await operator.unroute(
      `**/controlled/s01/api/queries/recovery-work-items/${encodeURIComponent(workId)}`,
    );
    await operator.getByRole("button", { name: "重新加载" }).click();
    await expect(operator.getByTestId("recovery-lifecycle-revision")).toHaveText(
      String(authority.lifecycle_revision),
    );
    await expect(operator.getByTestId("recovery-watermark")).toHaveText(
      String(authority.projection_watermark),
    );
    const verifyRequests = [];
    operator.on("request", (request) => {
      if (
        request.method() === "POST" &&
        new URL(request.url()).pathname.endsWith(`/recovery-work-items/${workId}/verify`)
      ) {
        verifyRequests.push(request.postDataJSON());
      }
    });
    await operator.getByRole("button", { name: "验证恢复" }).click();
    await expect(operator.getByTestId("recovery-command-status")).toHaveText("恢复事实已接受");
    await expect(operator.getByTestId("recovery-status")).toHaveText("resolved");
    await expect(operator.getByTestId("recovery-phase")).toHaveText("Evidence Ready");
    await expect(operator.getByTestId("recovery-fact-count")).toHaveText("1");
    await expect(operator.getByTestId("recovery-resolution-count")).toHaveText("1");
    await expect(operator.getByRole("button", { name: "验证恢复" })).toBeDisabled();
    expect(verifyRequests).toHaveLength(1);
    expect(Object.keys(verifyRequests[0]).sort()).toEqual([
      "expected_criterion_digest",
      "expected_lifecycle_revision",
      "idempotency_key",
    ]);
    expect(JSON.stringify(verifyRequests[0])).not.toContain("target");
    expect(JSON.stringify(verifyRequests[0])).not.toContain("verifier");
    expect(JSON.stringify(verifyRequests[0])).not.toContain("recovered");
    const operatorText = await operator.locator("body").innerText();
    expect(operatorText).not.toContain("Manual Review");
    expect(operatorText).not.toContain("Verification Completed");
    for (const restricted of restrictedStrings()) expect(operatorText).not.toContain(restricted);

    await operator.setViewportSize({ width: 390, height: 844 });
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
