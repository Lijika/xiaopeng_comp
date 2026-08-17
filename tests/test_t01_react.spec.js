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

/** Removes exactly this server's owned SQLite state and its -wal/-shm
 * siblings; every artifact is attempted even if one removal rejects. */
function cleanupStatePath(statePath) {
  let firstError;
  for (const suffix of ["", "-wal", "-shm"]) {
    try {
      fs.rmSync(`${statePath}${suffix}`, { force: true });
    } catch (error) {
      if (firstError === undefined) firstError = error;
    }
  }
  if (firstError !== undefined) throw firstError;
}

/** The unique state artifacts this worker process owns in /tmp. */
function listOwnedStateArtifacts() {
  return fs
    .readdirSync("/tmp")
    .filter((name) =>
      name.startsWith(`xiaopeng-task4-t01-react-${process.pid}-`),
    )
    .sort();
}

async function startServer({ appTarget, ...extraEnv } = {}) {
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
      appTarget ?? "tests.test_s07_http:create_s07_test_app",
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
        ...extraEnv,
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  child.stdout.on("data", (chunk) => output.push(chunk.toString()));
  child.stderr.on("data", (chunk) => output.push(chunk.toString()));

  const baseURL = `http://127.0.0.1:${port}`;
  // Readiness window must absorb the first test's chromium cold-start
  // competing with uvicorn import on a memory-pressured host; the loop
  // still aborts early if the child exits.
  const deadline = Date.now() + 30_000;
  let ready = false;
  while (Date.now() < deadline && child.exitCode === null) {
    try {
      if ((await fetch(`${baseURL}/api/health`)).ok) {
        ready = true;
        break;
      }
    } catch (_) {
      // The bounded readiness loop owns the retry.
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  if (ready) {
    return { baseURL, child, output, statePath };
  }
  // Startup failure fully owns its child and its exact temp state: register
  // the exit promise before killing, reap the process, then remove the owned
  // database/-wal/-shm artifacts before throwing.
  const exited = once(child, "exit");
  if (child.exitCode === null) {
    child.kill("SIGKILL");
    await exited;
  }
  cleanupStatePath(statePath);
  throw new Error(`T01 React server did not start: ${output.join("")}`);
}

async function stopServer(server) {
  const failures = [];
  // Child reaping is attempted when it is still running; the owned SQLite
  // state is always cleaned up even if the child already exited or one
  // cleanup step rejects, and the original failure is preserved.
  try {
    const exited = once(server.child, "exit");
    if (server.child.exitCode === null) {
      server.child.kill("SIGTERM");
      if (
        (await Promise.race([
          exited,
          new Promise((resolve) => setTimeout(resolve, 5_000, "timeout")),
        ])) === "timeout"
      ) {
        server.child.kill("SIGKILL");
        await exited;
      }
    }
  } catch (error) {
    failures.push(error);
  }
  try {
    cleanupStatePath(server.statePath);
  } catch (error) {
    failures.push(error);
  }
  if (failures.length > 0) throw failures[0];
}

/**
 * Attempts every owned cleanup even when an earlier one rejects, so a failed
 * context/close or setup step can never skip later cleanup (server stop,
 * temp-file unlink).  Re-throws the first failure so the original error is
 * preserved; the callers suppress it when the test body already failed.
 */
async function settleCleanup(cleanups) {
  const failures = [];
  for (const cleanup of cleanups) {
    try {
      await cleanup();
    } catch (error) {
      if (failures.length === 0) failures.push(error);
    }
  }
  if (failures.length > 0) throw failures[0];
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

function assertNoOverflow(page) {
  return page.evaluate(
    () => document.documentElement.scrollWidth <= window.innerWidth + 1,
  );
}

/**
 * Records page errors, console errors, and network request failures against
 * the live arrays (asserted after the flow), filtering only exact expected
 * resource errors (the deliberate stale-command 409 and named expectations
 * such as the existence-hiding 404 after session expiry, which may be bound
 * to an exact work-detail URL) — each counted separately — so any unexpected
 * 404/500/network/console/page failure stays visible.
 */
function trackPageDiagnostics(page, expectations = []) {
  const browserErrors = [];
  const consoleErrors = [];
  const networkErrors = [];
  const counts = { stale409: 0 };
  for (const expectation of expectations) counts[expectation.name] = 0;
  page.on("pageerror", (error) => browserErrors.push(error.message));
  page.on("console", (message) => {
    if (message.type() !== "error") return;
    const location = message.location().url || "";
    if (location.endsWith("/favicon.ico")) return;
    if (location.endsWith("/verify") && message.text().includes("409")) {
      counts.stale409 += 1;
      return;
    }
    for (const expectation of expectations) {
      if (
        expectation.url !== undefined &&
        location === expectation.url &&
        message.text().includes(expectation.statusText)
      ) {
        counts[expectation.name] += 1;
        return;
      }
    }
    consoleErrors.push(message.text());
  });
  page.on("requestfailed", (request) => {
    const url = request.url();
    if (url.endsWith("/favicon.ico")) return;
    networkErrors.push({
      url,
      failure: request.failure()?.errorText ?? "failed",
    });
  });
  return { browserErrors, consoleErrors, networkErrors, counts };
}

async function assertControlsFitAndDoNotOverlap(page, testIds) {
  const boxes = [];
  const centerHits = [];
  for (const testId of testIds) {
    const locator = page.getByTestId(testId);
    // Every named required control must exist exactly once and be visible:
    // a missing or hidden locator is a hard failure, never a silent skip.
    expect(await locator.count(), `${testId} count`).toBe(1);
    await locator.scrollIntoViewIfNeeded();
    expect(await locator.isVisible(), `${testId} visible`).toBe(true);
    const box = await locator.boundingBox();
    expect(box, `${testId} bounding box`).not.toBeNull();
    // Non-vacuous content/clipping check: a control must not hide its own
    // text/content.  Panels are exempt from the vertical check only because
    // their content is intentionally taller.
    const clip = await page.evaluate((id) => {
      const el = document.querySelector(`[data-testid="${id}"]`);
      if (!el) return null;
      return {
        scrollWidth: el.scrollWidth,
        clientWidth: el.clientWidth,
        scrollHeight: el.scrollHeight,
        clientHeight: el.clientHeight,
      };
    }, testId);
    expect(clip, `${testId} clip metrics`).not.toBeNull();
    expect(
      clip.scrollWidth,
      `${testId} horizontal clipping`,
    ).toBeLessThanOrEqual(clip.clientWidth + 1);
    if (!testId.endsWith("-panel")) {
      expect(
        clip.scrollHeight,
        `${testId} vertical clipping`,
      ).toBeLessThanOrEqual(clip.clientHeight + 1);
    }
    const scroll = await page.evaluate(() => window.scrollY);
    // Hit-test immediately after this element's scroll so a later element's
    // scroll cannot invalidate the measured box.
    centerHits.push(
      await page.evaluate(({ testId, box }) => {
        const element = document.querySelector(`[data-testid="${testId}"]`);
        if (!element) return null;
        if (getComputedStyle(element).pointerEvents === "none") {
          // Only a control with real disabled semantics is exempt from the
          // hit test; a merely pointer-events-none/occluded enabled control
          // is an occlusion blind spot and must fail.
          const disabled =
            element.hasAttribute("disabled") ||
            element.getAttribute("aria-disabled") === "true";
          return disabled;
        }
        const hit = document.elementFromPoint(
          box.x + box.width / 2,
          box.y + box.height / 2,
        );
        return element.contains(hit);
      }, { testId, box }),
    );
    boxes.push({
      testId,
      box,
      docTop: box.y + scroll,
      mustFitViewport: !testId.endsWith("-panel"),
    });
  }
  const { innerWidth, innerHeight } = await page.evaluate(() => ({
    innerWidth: window.innerWidth,
    innerHeight: window.innerHeight,
  }));
  for (const { testId, box, mustFitViewport } of boxes) {
    expect(box.x, `${testId} left edge`).toBeGreaterThanOrEqual(0);
    expect(box.y, `${testId} top edge`).toBeGreaterThanOrEqual(0);
    expect(
      box.x + box.width,
      `${testId} right edge`,
    ).toBeLessThanOrEqual(innerWidth + 1);
    if (mustFitViewport) {
      expect(
        box.y + box.height,
        `${testId} bottom edge`,
      ).toBeLessThanOrEqual(innerHeight + 1);
    }
  }
  const containsRelation = await page.evaluate(
    (ids) => {
      const byId = Object.fromEntries(
        ids.map((id) => [
          id,
          document.querySelector(`[data-testid="${id}"]`),
        ]),
      );
      const contains = {};
      for (const a of ids) {
        for (const b of ids) {
          if (a === b || !byId[a] || !byId[b]) continue;
          if (byId[a].contains(byId[b])) contains[`${a}>${b}`] = true;
        }
      }
      return contains;
    },
    boxes.map((entry) => entry.testId),
  );
  for (let i = 0; i < boxes.length; i += 1) {
    for (let j = i + 1; j < boxes.length; j += 1) {
      if (containsRelation[`${boxes[i].testId}>${boxes[j].testId}`]) continue;
      if (containsRelation[`${boxes[j].testId}>${boxes[i].testId}`]) continue;
      const a = boxes[i];
      const b = boxes[j];
      const overlap =
        a.docTop < b.docTop + b.box.height &&
        a.docTop + a.box.height > b.docTop &&
        a.box.x < b.box.x + b.box.width &&
        a.box.x + a.box.width > b.box.x;
      expect(overlap, `${a.testId} overlaps ${b.testId}`).toBe(false);
    }
  }
  for (const hit of centerHits) {
    expect(await hit).toBe(true);
  }
}

async function runFullChainTracer(browser, viewport, label) {
  const resources = {};
  let failure;
  try {
    resources.server = await startServer();
    const server = resources.server;
    resources.reviewerContext = await browser.newContext({
      viewport,
      extraHTTPHeaders: { Authorization: `Bearer ${DEMO_CREDENTIAL}` },
    });
    resources.operatorContext = await browser.newContext({
      viewport,
      extraHTTPHeaders: { Authorization: `Bearer ${OPERATOR_CREDENTIAL}` },
    });
    const reviewer = await resources.reviewerContext.newPage();
    const operator = await resources.operatorContext.newPage();
    const reviewerDiagnostics = trackPageDiagnostics(reviewer);
    const operatorDiagnostics = trackPageDiagnostics(operator);
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

    const shellResponse = await reviewer.goto(`${server.baseURL}${REACT_URL}`, {
      waitUntil: "networkidle",
    });
    expect(shellResponse.status()).toBe(200);
    expect(shellResponse.headers()["cache-control"]).toContain("no-store");
    await expect(reviewer.getByTestId("queue-panel")).toBeVisible();
    await expect(reviewer.getByTestId("queue-empty")).toBeVisible();
    expect(await assertNoOverflow(reviewer)).toBe(true);

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
    await expect(
      reviewer.getByTestId("recovery-command-status"),
    ).toHaveAttribute("role", "status");
    await expect(
      reviewer.getByTestId("recovery-command-status"),
    ).toHaveAttribute("aria-live", "polite");
    const reviewerText = await reviewer.locator("body").innerText();
    for (const value of restricted) expect(reviewerText).not.toContain(value);

    const authorityResponse = await operator.request.get(
      `${server.baseURL}/controlled/s01/api/queries/recovery-work-items/${encodeURIComponent(workId)}`,
    );
    expect(authorityResponse.ok()).toBeTruthy();
    const authority = await authorityResponse.json();
    const authorityText = JSON.stringify(authority);
    for (const value of restricted) expect(authorityText).not.toContain(value);

    // Reviewer layout while the open Recovery Work and its actionable queue
    // link are both live: every named control exists exactly once, is
    // visible, fits the viewport, does not overlap, and is center-hittable.
    await assertControlsFitAndDoNotOverlap(reviewer, [
      "queue-panel",
      "recovery-panel",
      "recovery-actions",
      "recovery-command-status",
      "queue-work-link",
      "reload-button",
      "verify-button",
    ]);

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
    expect(await assertNoOverflow(operator)).toBe(true);

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
    expect(await assertNoOverflow(reviewer)).toBe(true);

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

    // Layout checks run while both contexts are still on the React page: the
    // Reviewer authoritative gate and the Operator recovery panel/actions plus
    // the individual reload/verify buttons, at the active viewport.  The
    // legacy rollback navigation happens strictly after these.
    await assertControlsFitAndDoNotOverlap(operator, [
      "queue-panel",
      "recovery-panel",
      "recovery-actions",
      "recovery-command-status",
      "reload-button",
      "verify-button",
    ]);
    await assertControlsFitAndDoNotOverlap(reviewer, [
      "queue-panel",
      "recovery-panel",
      "recovery-actions",
      "recovery-command-status",
      "gate-panel",
      "reload-button",
      "verify-button",
    ]);

    const canonicalResponse = await reviewer.goto(`${server.baseURL}/controlled/s01`);
    expect(canonicalResponse.status()).toBe(200);

    expect(reviewerDiagnostics.browserErrors).toEqual([]);
    expect(operatorDiagnostics.browserErrors).toEqual([]);
    expect(reviewerDiagnostics.consoleErrors).toEqual([]);
    expect(operatorDiagnostics.consoleErrors).toEqual([]);
    expect(reviewerDiagnostics.networkErrors).toEqual([]);
    expect(operatorDiagnostics.networkErrors).toEqual([]);
    expect(operatorDiagnostics.counts.stale409).toBe(1);
  } catch (error) {
    failure = error;
    throw error;
  } finally {
    try {
      await settleCleanup([
        resources.reviewerContext
          ? () => resources.reviewerContext.close()
          : () => Promise.resolve(),
        resources.operatorContext
          ? () => resources.operatorContext.close()
          : () => Promise.resolve(),
        resources.server
          ? () => stopServer(resources.server)
          : () => Promise.resolve(),
      ]);
    } catch (cleanupError) {
      if (failure === undefined) throw cleanupError;
    }
  }
}

const VIEWPORTS = [
  { width: 1280, height: 800, label: "desktop 1280x800" },
  { width: 390, height: 844, label: "mobile 390x844" },
];

for (const viewport of VIEWPORTS) {
  test(`T01 production tracer (${viewport.label}): queue discovery, handoff, stale reload, one accepted VerifyRecovery, server-owned gate`, async ({
    browser,
  }) => {
    test.setTimeout(120_000);
    await runFullChainTracer(browser, viewport, viewport.label);
  });
}

test("T01 production tracer: expired session shows the explicit expired state and hides work existence", async ({
  browser,
}) => {
  test.setTimeout(120_000);
  const clockPath = path.join(
    "/tmp",
    `xiaopeng-task4-t01-react-clock-${process.pid}-${Date.now()}.txt`,
  );
  fs.writeFileSync(clockPath, "100", "ascii");
  const resources = {};
  let failure;
  try {
    resources.server = await startServer({
      appTarget: "tests.test_t01_http:create_t01_expiring_app",
      TASK4_S01_TEST_SESSION_CLOCK_PATH: clockPath,
      TASK4_S01_TEST_SESSION_TTL_SECONDS: "10",
    });
    const server = resources.server;
    resources.reviewerContext = await browser.newContext({
      viewport: { width: 1280, height: 800 },
      extraHTTPHeaders: { Authorization: `Bearer ${DEMO_CREDENTIAL}` },
    });
    const reviewer = await resources.reviewerContext.newPage();
    const expiry404 = { name: "hiddenWork404", url: null, statusText: "404" };
    const diagnostics = trackPageDiagnostics(reviewer, [expiry404]);
    const restricted = restrictedStrings();

    await reviewer.goto(`${server.baseURL}${REACT_URL}`, {
      waitUntil: "networkidle",
    });
    await expect(reviewer.getByTestId("queue-panel")).toBeVisible();

    const workId = await installRecoveryWork(server.baseURL, reviewer);
    // Bind the expected existence-hiding 404 to the exact work-detail URL for
    // the generated work ID: an unrelated or late work-detail 404 can no
    // longer substitute for the intended expiry 404.
    expiry404.url = `${server.baseURL}/controlled/s01/api/queries/recovery-work-items/${encodeURIComponent(workId)}`;
    await reviewer.reload({ waitUntil: "networkidle" });
    await expect(
      reviewer.getByRole("link", { name: new RegExp(workId) }),
    ).toBeVisible();

    // Open the work detail before expiry so cached identifiers and facts
    // are present in the DOM when the session ends.
    await reviewer.getByRole("link", { name: new RegExp(workId) }).click();
    await expect(reviewer.getByTestId("recovery-panel")).toBeVisible();
    await expect(reviewer.getByTestId("recovery-status")).toHaveText("open");
    await expect(reviewer.getByTestId("recovery-fact-count")).toHaveText("0");

    // The session expires while the SPA stays open; a focus-driven refetch
    // must surface the explicit expired state, unmount the cached detail,
    // and leak no work identifiers or restricted facts.
    fs.writeFileSync(clockPath, "111", "ascii");
    await reviewer.evaluate(() =>
      window.dispatchEvent(new Event("visibilitychange")),
    );
    await expect(reviewer.getByTestId("queue-access-ended")).toBeVisible();
    await expect(reviewer.getByTestId("queue-access-ended")).toHaveText(
      /会话已过期/,
    );
    await expect(reviewer.getByTestId("queue-status")).toHaveText("会话已过期");
    await expect(reviewer.getByTestId("queue-recovery-items")).toHaveCount(0);
    await expect(reviewer.getByTestId("recovery-panel")).toHaveCount(0);
    const expiredText = await reviewer.locator("body").innerText();
    for (const value of restricted) expect(expiredText).not.toContain(value);
    expect(expiredText).not.toContain(workId);
    expect(await assertNoOverflow(reviewer)).toBe(true);
    expect(diagnostics.browserErrors).toEqual([]);
    expect(diagnostics.consoleErrors).toEqual([]);
    expect(diagnostics.networkErrors).toEqual([]);
    expect(diagnostics.counts.stale409).toBe(0);
    expect(diagnostics.counts.hiddenWork404).toBe(1);
  } catch (error) {
    failure = error;
    throw error;
  } finally {
    try {
      await settleCleanup([
        resources.reviewerContext
          ? () => resources.reviewerContext.close()
          : () => Promise.resolve(),
        resources.server ? () => stopServer(resources.server) : () => Promise.resolve(),
        () => fs.unlinkSync(clockPath),
      ]);
    } catch (cleanupError) {
      if (failure === undefined) throw cleanupError;
    }
  }
});

test("T01 production tracer: a failed authoritative reload keeps the conflict fence", async ({
  browser,
}) => {
  test.setTimeout(120_000);
  const resources = {};
  let failure;
  try {
    resources.server = await startServer();
    const server = resources.server;
    resources.operatorContext = await browser.newContext({
      viewport: { width: 1280, height: 800 },
      extraHTTPHeaders: { Authorization: `Bearer ${OPERATOR_CREDENTIAL}` },
    });
    resources.reviewerContext = await browser.newContext({
      viewport: { width: 1280, height: 800 },
      extraHTTPHeaders: { Authorization: `Bearer ${DEMO_CREDENTIAL}` },
    });
    const operator = await resources.operatorContext.newPage();
    const reviewer = await resources.reviewerContext.newPage();
    const operatorDiagnostics = trackPageDiagnostics(operator);
    const restricted = restrictedStrings();
    const workPath = "**/controlled/s01/api/queries/recovery-work-items/*";

    let verifyPosts = 0;
    let acceptedPosts = 0;
    operator.on("request", (request) => {
      const url = new URL(request.url());
      if (
        request.method() === "POST" &&
        url.pathname.includes("/commands/recovery-work-items/") &&
        url.pathname.endsWith("/verify")
      ) {
        verifyPosts += 1;
      }
    });

    await reviewer.goto(`${server.baseURL}${REACT_URL}`, {
      waitUntil: "networkidle",
    });
    const workId = await installRecoveryWork(server.baseURL, reviewer);

    let staleProjectionDelivered = false;
    await operator.route(workPath, async (route) => {
      const response = await route.fetch();
      const projected = await response.json();
      if (!staleProjectionDelivered) {
        projected.lifecycle_revision -= 1;
        staleProjectionDelivered = true;
      }
      await route.fulfill({ response, json: projected });
    });
    await operator.goto(
      `${server.baseURL}${REACT_URL}?work=${encodeURIComponent(workId)}`,
      { waitUntil: "networkidle" },
    );
    await expect(operator.getByTestId("recovery-panel")).toBeVisible();
    await operator.getByRole("button", { name: "验证恢复" }).click();
    await expect(operator.getByTestId("recovery-command-status")).toContainText(
      "recovery.context_changed",
    );
    await operator.unroute(workPath);

    // The authoritative reload fails at the HTTP boundary (a non-retryable
    // 500); the fence and the semantic key must survive and the button must
    // stay disabled.
    await operator.route(workPath, (route) =>
      route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({ detail: { error: "S01_INTERNAL_ERROR" } }),
      }),
    );
    await operator.getByRole("button", { name: "重新加载" }).click();
    await expect(operator.getByTestId("recovery-command-status")).toContainText(
      "recovery.context_changed",
    );
    await expect(
      operator.getByRole("button", { name: "验证恢复" }),
    ).toBeDisabled();
    expect(verifyPosts).toBe(1);
    await operator.unroute(workPath);

    await operator.getByRole("button", { name: "重新加载" }).click();
    await expect(
      operator.getByRole("button", { name: "验证恢复" }),
    ).toBeEnabled();
    await operator.getByRole("button", { name: "验证恢复" }).click();
    await expect(operator.getByTestId("recovery-command-status")).toHaveText(
      "恢复事实已接受",
    );
    await expect(operator.getByTestId("recovery-status")).toHaveText("resolved");
    expect(verifyPosts).toBe(2);

    const operatorText = await operator.locator("body").innerText();
    for (const value of restricted) expect(operatorText).not.toContain(value);
    // Exactly one deliberate 500 (the failed authoritative reload) and one
    // deliberate stale-command 409; nothing else may surface.
    expect(operatorDiagnostics.browserErrors).toEqual([]);
    expect(operatorDiagnostics.networkErrors).toEqual([]);
    expect(operatorDiagnostics.consoleErrors).toHaveLength(1);
    expect(operatorDiagnostics.consoleErrors[0]).toContain("500");
    expect(operatorDiagnostics.counts.stale409).toBe(1);
  } catch (error) {
    failure = error;
    throw error;
  } finally {
    try {
      await settleCleanup([
        resources.operatorContext
          ? () => resources.operatorContext.close()
          : () => Promise.resolve(),
        resources.reviewerContext
          ? () => resources.reviewerContext.close()
          : () => Promise.resolve(),
        resources.server
          ? () => stopServer(resources.server)
          : () => Promise.resolve(),
      ]);
    } catch (cleanupError) {
      if (failure === undefined) throw cleanupError;
    }
  }
});

test("cleanup settles every owned resource even when a rejected step would previously skip later cleanup", async () => {
  // A rejected context.close() (or a rejected setup step) must not skip the
  // later owned cleanups (stopServer, clock/temp unlink), and the first
  // failure is preserved so an incidental cleanup error never replaces the
  // original test failure.
  const order = [];
  const settle = settleCleanup([
    async () => {
      order.push("context.close");
      throw new Error("context.close failed");
    },
    async () => {
      order.push("stopServer");
    },
    async () => {
      order.push("clock.unlink");
    },
  ]);
  await expect(settle).rejects.toThrow("context.close failed");
  expect(order).toEqual(["context.close", "stopServer", "clock.unlink"]);
});

test("layout oracle distinguishes a semantically disabled control from a merely occluded enabled one", async ({
  page,
}) => {
  // A genuinely disabled control is exempt from the center-hit check, but a
  // merely pointer-events-none/occluded enabled control must fail (no silent
  // occlusion blind spot).
  await page.setViewportSize({ width: 500, height: 400 });
  await page.setContent(`
    <style>
      .stage { position: relative; width: 460px; height: 200px; }
      .occluder { position: absolute; top: 60px; left: 0; width: 460px; height: 90px; }
      .occluded { position: absolute; top: 70px; left: 20px; pointer-events: none; }
    </style>
    <div class="stage">
      <button data-testid="disabled-button" disabled>恢复验证</button>
      <div class="occluder" data-testid="occluder"></div>
      <button class="occluded" data-testid="occluded-button">验证恢复</button>
    </div>
  `);
  await assertControlsFitAndDoNotOverlap(page, ["disabled-button"]);
  await expect(
    assertControlsFitAndDoNotOverlap(page, ["occluded-button"]),
  ).rejects.toThrow();
});

test("cleanupStatePath removes the owned database and its -wal/-shm siblings even when one artifact is missing", () => {
  const base = path.join(
    "/tmp",
    `xiaopeng-task4-t01-react-${process.pid}-${Date.now()}-selfcheck.sqlite3`,
  );
  fs.writeFileSync(base, "db");
  fs.writeFileSync(`${base}-wal`, "wal");
  cleanupStatePath(base);
  expect(fs.existsSync(base)).toBe(false);
  expect(fs.existsSync(`${base}-wal`)).toBe(false);
  expect(fs.existsSync(`${base}-shm`)).toBe(false);
});

test("a failed server startup is reaped and leaves no owned state artifacts", async () => {
  const before = listOwnedStateArtifacts();
  await expect(
    startServer({ appTarget: "tests.test_t01_http:no_such_test_factory" }),
  ).rejects.toThrow(/did not start/);
  const after = listOwnedStateArtifacts();
  expect(after).toEqual(before);
});
