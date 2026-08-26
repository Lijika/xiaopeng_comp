/**
 * Ticket #50 / T16 production tracer.
 *
 * Two independent browser contexts exercise the released S14 lifecycle
 * through the real FastAPI/uvicorn authority and the shared production React
 * build: the registered integrator cancels an eligible application and
 * observes authoritative Terminating -> Terminated transitions through
 * bounded refetching, while the registered operator settles the termination,
 * delivers the notification, grants an independent reopen permission and
 * explicitly opens successor cycle 2.  Navigation, reload and back/forward
 * never issue lifecycle commands.
 */
const { spawn } = require("node:child_process");
const { once } = require("node:events");
const fs = require("node:fs");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");

const { expect, test } = require("@playwright/test");

const ROOT = path.resolve(__dirname, "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");
const INTEGRATOR_CREDENTIAL = "t16-integrator-credential";
const OPERATOR_CREDENTIAL = "t16-operator-credential";

async function reservePort() {
  const server = net.createServer();
  const port = await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      resolve(typeof address === "object" && address !== null ? address.port : 0);
    });
  });
  server.close();
  return port;
}

function cleanupTree(root) {
  try {
    fs.rmSync(root, { recursive: true, force: true });
  } catch {
    // The test owns only its mkdtemp root; assertion state lives elsewhere.
  }
}

async function startServer(fixtureRoot) {
  const port = await reservePort();
  const output = [];
  const child = spawn(
    PYTHON,
    [
      "-m",
      "uvicorn",
      "tests.test_t16_react_app:create_t16_react_test_app",
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
        TASK4_T16_FIXTURE_ROOT: fixtureRoot,
        TASK4_WEB_TOKEN: "t16-global-web-token",
        NO_PROXY: "127.0.0.1,localhost",
        no_proxy: "127.0.0.1,localhost",
        PYTHONDONTWRITEBYTECODE: "1",
        PYTHONPYCACHEPREFIX: "/tmp/xiaopeng-task4-t16-pycache",
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  child.stdout.on("data", (chunk) => output.push(chunk.toString()));
  child.stderr.on("data", (chunk) => output.push(chunk.toString()));

  const baseURL = `http://127.0.0.1:${port}`;
  const deadline = Date.now() + 60_000;
  while (Date.now() < deadline && child.exitCode === null) {
    try {
      if ((await fetch(`${baseURL}/api/health`)).ok) {
        return { baseURL, child };
      }
    } catch (_) {
      // Bounded readiness retry.
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  child.kill("SIGKILL");
  throw new Error(`T16 server did not start: ${output.join("")}`);
}

async function stopServer(current) {
  const exited = once(current.child, "exit");
  if (current.child.exitCode !== null) return;
  current.child.kill("SIGTERM");
  if (
    (await Promise.race([
      exited,
      new Promise((resolve) => setTimeout(resolve, 5_000, "timeout")),
    ])) === "timeout"
  ) {
    current.child.kill("SIGKILL");
    await exited;
  }
}

function readFixture(fixtureRoot) {
  return JSON.parse(
    fs.readFileSync(path.join(fixtureRoot, "fixture.json"), "utf8"),
  );
}

function assertAllowlistedRequests(requests) {
  const allowed = [
    { method: "GET", pattern: /^\/controlled\/s14(?:\/settlement)?(?:\/react)?(?:\?.*)?$/ },
    { method: "GET", pattern: /^\/controlled\/s01\/api\/queries\/[a-z0-9_/-]+$/ },
    { method: "GET", pattern: /^\/controlled\/s13\/delivery\/[a-z0-9_]+$/ },
    { method: "POST", pattern: /^\/controlled\/s01\/api\/commands\/applications\/[^/]+\/(?:cancel|settle-termination|grant-reopen-permission|reopen)$/ },
    { method: "POST", pattern: /^\/controlled\/s01\/api\/commands\/process-termination-notification$/ },
    { method: "GET", pattern: /^\/static\/react\/(?:index\.html|assets\/[A-Za-z0-9._-]+\.(?:js|css))$/ },
    { method: "GET", pattern: /^\/favicon\.ico$/ },
  ];
  const violations = requests.filter(({ method, url }) => {
    const parsed = new URL(url);
    const target = `${parsed.pathname}${parsed.search}`;
    return !allowed.some(
      (entry) => entry.method === method && entry.pattern.test(target),
    );
  });
  expect(violations).toEqual([]);
}

async function assertNoHorizontalOverflow(page, sectionTestIds) {
  const result = await page.evaluate((ids) => {
    const boxes = ids.map((id) => {
      const element = document.querySelector(`[data-testid="${id}"]`);
      if (element === null) return null;
      const box = element.getBoundingClientRect();
      return {
        left: box.left,
        right: box.right,
        contentFits: element.scrollWidth <= element.clientWidth,
      };
    });
    return {
      documentFits: document.documentElement.scrollWidth <= window.innerWidth,
      boxesPresent: boxes.every((box) => box !== null),
      boxesFit: boxes.every(
        (box) =>
          box !== null &&
          box.contentFits &&
          box.left >= 0 &&
          box.right <= window.innerWidth,
      ),
    };
  }, sectionTestIds);
  expect(result).toEqual({
    documentFits: true,
    boxesPresent: true,
    boxesFit: true,
  });
}

let server;
let fixtureRoot;

test.beforeEach(() => {
  fixtureRoot = fs.mkdtempSync(path.join(os.tmpdir(), "xiaopeng-t16-"));
});

test.afterEach(async () => {
  if (server !== undefined) {
    await stopServer(server);
    server = undefined;
  }
  cleanupTree(fixtureRoot);
});

for (const [label, viewport] of [
  ["desktop 1280x800", { width: 1280, height: 800 }],
  ["mobile 390x844", { width: 390, height: 844 }],
]) {
  test(`cancellation, reconciliation, settlement, and explicit reopen stay FastAPI-owned at ${label}`, async ({
    browser,
  }) => {
    test.setTimeout(180_000);
    server = await startServer(fixtureRoot);
    const fixture = readFixture(fixtureRoot);
    const activeApplicationId = fixture.active_application_id;
    const lateApplicationId = fixture.late_application_id;

    const integratorRequests = [];
    const operatorRequests = [];
    const browserErrors = [];

    // Context A — the registered upstream integrator: demo bearer identity,
    // session cookie issued by the canonical shell, cancel authority only.
    const integratorContext = await browser.newContext({
      extraHTTPHeaders: { Authorization: `Bearer ${INTEGRATOR_CREDENTIAL}` },
      viewport,
    });
    const integratorPage = await integratorContext.newPage();
    integratorPage.on("request", (request) =>
      integratorRequests.push({ method: request.method(), url: request.url() }),
    );
    integratorPage.on("pageerror", (error) => browserErrors.push(error.message));
    integratorPage.on("console", (message) => {
      if (message.type() === "error") browserErrors.push(message.text());
    });

    // Context B — the registered operator control-plane identity: bearer
    // only, no session, settlement authority only.
    const operatorContext = await browser.newContext({
      extraHTTPHeaders: { Authorization: `Bearer ${OPERATOR_CREDENTIAL}` },
      viewport,
    });
    const operatorPage = await operatorContext.newPage();
    operatorPage.on("request", (request) =>
      operatorRequests.push({ method: request.method(), url: request.url() }),
    );
    operatorPage.on("pageerror", (error) => browserErrors.push(error.message));

    // --- integrator: authoritative facts, explicit cancel ------------------
    await integratorPage.goto(
      `${server.baseURL}/controlled/s14?application=${encodeURIComponent(activeApplicationId)}`,
      { waitUntil: "domcontentloaded" },
    );
    await expect(integratorPage.getByTestId("s14-boundary-gate")).toHaveText("S14");
    await expect(integratorPage.getByTestId("t16-phase")).toHaveText("Manual Review");
    await expect(integratorPage.getByTestId("t16-cycle")).toHaveText("1");

    await integratorPage.getByTestId("t16-cancel-reason").focus();
    await integratorPage.keyboard.press("Tab");
    await expect(integratorPage.getByTestId("t16-cancel-button")).toBeFocused();

    await integratorPage.getByTestId("t16-cancel-button").click();
    await expect(integratorPage.getByTestId("t16-result-status")).toContainText(
      "accepted",
    );
    await expect(integratorPage.getByTestId("t16-cancel-fenced-effects")).toBeVisible();
    // The UI shows exactly what FastAPI reports while effects reconcile.
    await expect(integratorPage.getByTestId("t16-phase")).toHaveText("Terminating");
    await expect(integratorPage.getByTestId("t16-terminating-status")).toBeVisible();
    await expect(integratorPage.getByRole("status")).not.toHaveCount(0);

    // --- operator: settle-arm, notify, settle-seal, grant, reopen ----------
    await operatorPage.goto(
      `${server.baseURL}/controlled/s14/settlement?application=${encodeURIComponent(activeApplicationId)}`,
      { waitUntil: "domcontentloaded" },
    );
    await expect(
      operatorPage.getByTestId("s14-settlement-boundary-gate"),
    ).toHaveText("S14");
    await expect(operatorPage.getByTestId("t16-settlement-phase")).toHaveText(
      "Terminating",
    );

    await operatorPage.getByTestId("t16-settle-button").click();
    await expect(operatorPage.getByTestId("t16-result-status")).toContainText(
      "outstanding",
    );
    await expect(operatorPage.getByTestId("t16-unresolved-effects")).toContainText(
      "termination_notification",
    );

    // A full reload must not lose the pending-effect fact: availability
    // derives from the authoritative settlement read, not local results.
    const operatorPostsBeforeReload = operatorRequests.filter(
      ({ method }) => method === "POST",
    ).length;
    await operatorPage.reload({ waitUntil: "domcontentloaded" });
    await expect(operatorPage.getByTestId("t16-settlement-phase")).toHaveText(
      "Terminating",
    );
    await expect(operatorPage.getByTestId("t16-notification-button")).toBeEnabled();

    await operatorPage.getByTestId("t16-notification-button").click();
    await expect(operatorPage.getByTestId("t16-notification-status")).toContainText(
      "delivered",
    );

    await operatorPage.getByTestId("t16-settle-button").click();
    await expect(operatorPage.getByTestId("t16-result-status")).toContainText(
      "terminated",
    );

    // The integrator's bounded poll observes Terminated strictly from the
    // authoritative route before any successor-cycle fact exists.
    await expect(integratorPage.getByTestId("t16-phase")).toHaveText(
      "Terminated",
      { timeout: 30_000 },
    );
    await expect(integratorPage.getByTestId("t16-terminated")).toBeVisible();
    await expect(integratorPage.getByTestId("t16-poll-timeout")).toHaveCount(0);

    await operatorPage.getByTestId("t16-grant-approver").fill("t16-independent-approver");
    await operatorPage
      .getByTestId("t16-grant-permission-id")
      .fill("institutional-reopen-permission/t16-e2e");
    await operatorPage.getByTestId("t16-grant-button").click();
    await expect(operatorPage.getByTestId("t16-grant-binding")).toContainText(
      "institutional-reopen-permission/t16-e2e",
    );

    // The server-owned binding survives a full reload through the
    // authoritative settlement read; reopen stays enabled without a
    // duplicate grant.
    await operatorPage.reload({ waitUntil: "domcontentloaded" });
    await expect(
      operatorPage.getByTestId("s14-settlement-boundary-gate"),
    ).toHaveText("S14");
    const reopenButton = operatorPage.getByTestId("t16-reopen-button");
    await expect(reopenButton).toBeEnabled({ timeout: 10_000 });
    await operatorPage.getByTestId("t16-reopen-target").selectOption("Intake");
    await reopenButton.click();
    await expect(operatorPage.getByTestId("t16-reopen-result")).toContainText(
      "cycle 2",
    );
    await expect(operatorPage.getByTestId("t16-reopen-result")).toContainText("Intake");

    // Reload picks up the server-created successor cycle without issuing any
    // command; back/forward navigation likewise stays read-only.
    const cycleButtons = integratorPage.getByTestId("t16-history-run-cycle");
    await expect(cycleButtons).not.toHaveCount(0);
    const postsBeforeReload = integratorRequests.filter(
      ({ method }) => method === "POST",
    ).length;
    await integratorPage.reload({ waitUntil: "domcontentloaded" });
    await expect(integratorPage.getByTestId("t16-phase")).toHaveText("Intake");
    await expect(integratorPage.getByTestId("t16-cycle")).toHaveText("2");
    // A successor Intake cycle is genuinely cancellable again: eligibility
    // tracks the server-owned phase, never the page history.
    await expect(integratorPage.getByTestId("t16-cancel-button")).toBeEnabled();

    await assertNoHorizontalOverflow(integratorPage, [
      "t16-facts-section",
      "t16-cancel-section",
      "t16-history-section",
    ]);

    // Old-cycle navigation stays presentation-only on the reopened cycle.
    const cycleNavButtons = integratorPage.getByTestId("t16-history-run-cycle");
    await expect(cycleNavButtons.first()).toContainText("1");
    await cycleNavButtons.first().click();
    await expect(integratorPage).toHaveURL(/cycle=1/);
    // The selected historical cycle renders authoritative cycle-scoped facts
    // (immutable cancellation/termination/reopen events and the late-input
    // receipt), with no command surface.
    await expect(integratorPage.getByTestId("t16-cycle-view")).toBeVisible();
    await expect(integratorPage.getByTestId("t16-cycle-banner")).toContainText(
      "Cycle 1",
    );
    await expect(
      integratorPage.getByTestId("t16-cycle-cancellation"),
    ).toContainText("UPSTREAM_WITHDRAWN");
    await expect(
      integratorPage.getByTestId("t16-cycle-termination"),
    ).toBeVisible();
    await expect(integratorPage.getByTestId("t16-cycle-reopen")).toContainText(
      "Intake",
    );
    await expect(integratorPage.getByTestId("t16-cancel-button")).toHaveCount(0);

    // The sealed late-work application renders its immutable cycle-scoped
    // facts including the late-input receipt demanding explicit reopen.
    await integratorPage.goto(
      `${server.baseURL}/controlled/s14?application=${encodeURIComponent(lateApplicationId)}`,
      { waitUntil: "domcontentloaded" },
    );
    await expect(integratorPage.getByTestId("t16-phase")).toHaveText(
      "Intake",
    );
    await expect(integratorPage.getByTestId("t16-cycle")).toHaveText("2");
    const lateCycleNav = integratorPage.getByTestId("t16-history-run-cycle");
    await lateCycleNav.first().click();
    await expect(integratorPage.getByTestId("t16-cycle-view")).toBeVisible();
    await expect(
      integratorPage.getByTestId("t16-cycle-cancellation"),
    ).toContainText("UPSTREAM_WITHDRAWN");
    await expect(
      integratorPage.getByTestId("t16-cycle-termination"),
    ).toBeVisible();
    await expect(integratorPage.getByTestId("t16-late-receipt")).toContainText(
      "evidence.late_input_requires_reopen",
    );
    // Immutable work pins and route facts render for the selected cycle.
    const lateFindings = integratorPage.getByTestId("t16-cycle-run-findings");
    await expect(lateFindings.first()).not.toHaveText("—");
    await expect(
      integratorPage.getByTestId("t16-cycle-run-currentness"),
    ).toBeVisible();
    await expect(integratorPage.getByTestId("t16-cancel-button")).toHaveCount(0);

    // Selecting the current reopened cycle shows no cycle-1 leakage.
    await integratorPage.goto(
      `${server.baseURL}/controlled/s14?application=${encodeURIComponent(lateApplicationId)}&cycle=2`,
      { waitUntil: "domcontentloaded" },
    );
    await expect(integratorPage.getByTestId("t16-cycle-view")).toBeVisible();
    await expect(
      integratorPage.getByTestId("t16-late-receipts-empty"),
    ).toContainText("No late-input receipts");
    await expect(
      integratorPage.getByTestId("t16-late-receipt"),
    ).toHaveCount(0);

    // Back/forward navigation stays read-only.  The overflow and POST
    // assertions run only after the forward URL and the required sections
    // have settled, so React navigation is never raced.
    await integratorPage.goBack();
    await expect(integratorPage).toHaveURL(/cycle=1/);
    await expect(integratorPage.getByTestId("t16-cycle-view")).toBeVisible();
    await integratorPage.goForward();
    await expect(integratorPage).toHaveURL(/cycle=2/);
    await expect(integratorPage.getByTestId("t16-cycle-view")).toBeVisible();
    await expect(
      integratorPage.getByTestId("t16-late-receipts-empty"),
    ).toContainText("No late-input receipts");
    const postsAfterReload = integratorPosts(integratorRequests);
    expect(postsAfterReload.length).toBe(postsBeforeReload);
    expect(postsAfterReload.every(({ url }) => url.endsWith("/cancel"))).toBe(true);

    // --- cross-context request contract ------------------------------------
    const cancelPosts = integratorPosts(integratorRequests).filter(({ url }) =>
      url.endsWith("/cancel"),
    );
    expect(cancelPosts).toHaveLength(1);

    const operatorPosts = operatorRequests.filter(
      ({ method }) => method === "POST",
    );
    const postPathCounts = {};
    for (const { url } of operatorPosts) {
      const pathname = new URL(url).pathname;
      postPathCounts[pathname.split("/").pop()] =
        (postPathCounts[pathname.split("/").pop()] ?? 0) + 1;
    }
    expect(postPathCounts).toEqual({
      "settle-termination": 2,
      "process-termination-notification": 1,
      "grant-reopen-permission": 1,
      reopen: 1,
    });

    expect(browserErrors).toEqual([]);

    for (const page of [integratorPage, operatorPage]) {
      expect(
        await page.evaluate(() => ({
          local: localStorage.length,
          session: sessionStorage.length,
        })),
      ).toEqual({ local: 0, session: 0 });
    }

    assertAllowlistedRequests([...integratorRequests, ...operatorRequests]);
    // The active-app current view and the selected historical cycle view are
    // both overflow-checked on their own section sets.
    await assertNoHorizontalOverflow(integratorPage, [
      "t16-facts-section",
      "t16-cancel-section",
      "t16-history-section",
    ]);
    await integratorPage.goto(
      `${server.baseURL}/controlled/s14?application=${encodeURIComponent(lateApplicationId)}&cycle=1`,
      { waitUntil: "domcontentloaded" },
    );
    await expect(integratorPage.getByTestId("t16-cycle-view")).toBeVisible();
    // The late app's cycle-1 selected view is overflow-checked on the
    // cycle-scoped section set.
    await assertNoHorizontalOverflow(integratorPage, [
      "t16-cycle-section",
      "t16-history-section",
    ]);
    await assertNoHorizontalOverflow(operatorPage, [
      "t16-settlement-facts-section",
      "t16-settle-section",
      "t16-reopen-section",
    ]);

    await integratorContext.close();
    await operatorContext.close();
  });
}

function integratorPosts(requests) {
  return requests.filter(({ method }) => method === "POST");
}
