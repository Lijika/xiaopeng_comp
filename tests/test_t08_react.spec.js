const { spawn } = require("node:child_process");
const { once } = require("node:events");
const fs = require("node:fs");
const net = require("node:net");
const path = require("node:path");

const { expect, test } = require("@playwright/test");

const ROOT = path.resolve(__dirname, "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");
const S08_URL = "/controlled/s08/react";
const S01_URL = "/controlled/s01/react";
const S09_URL = "/controlled/s09/react";
const ADMIN_CREDENTIAL = "s08-registered-admin-test-credential";
const APPROVER_CREDENTIAL = "s08-registered-approver-test-credential";
const OPERATOR_CREDENTIAL = "s08-registered-operator-test-credential";
const AUDITOR_CREDENTIAL = "s01-registered-auditor-test-credential";
const DEMO_CREDENTIAL = "s01-registered-demo-test-credential";
const SCENARIO = "app_uncertain_ocr_noise.json";
const SOURCE_BUNDLE_ID = "c-demo-legacy-baseline/1";
const S08_SCOPE = "C-DEMO/demo";
const VIEWPORTS = [
  { label: "desktop", width: 1280, height: 800 },
  { label: "mobile", width: 390, height: 844 },
];

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

/** Starts the real FastAPI/uvicorn test-factory app serving the production
 * Vite build with the S08 governance authority configured for three distinct
 * identities (the same factory the S08 HTTP contract suite runs against).
 * The background worker stays ON so the S08 validation and activation jobs
 * actually run against the real ledger.  ``extraEnv`` lets a test point the
 * S08 corpus at its own directory or inject a worker fault point without
 * touching shared fixtures. */
async function startServer(extraEnv = {}) {
  const port = await reservePort();
  const statePath = path.join(
    "/tmp",
    `xiaopeng-task4-t08-react-${process.pid}-${port}-${Date.now()}.sqlite3`,
  );
  const env = { ...process.env };
  delete env.TASK4_WEB_TOKEN;
  const output = [];
  const child = spawn(
    PYTHON,
    [
      "-m",
      "uvicorn",
      "task4_consistency.web.app:create_s01_test_app",
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
        ...env,
        NO_PROXY: "127.0.0.1,localhost",
        no_proxy: "127.0.0.1,localhost",
        PYTHONDONTWRITEBYTECODE: "1",
        PYTHONPYCACHEPREFIX: "/tmp/xiaopeng-task4-t08-react-pycache",
        TASK4_S01_STATE_PATH: statePath,
        TASK4_S01_TEST_STATE_PATH: statePath,
        TASK4_S01_TEST_BACKGROUND_ENABLED: "1",
        TASK4_S01_TEST_SCENARIO_ID: SCENARIO,
        TASK4_S01_DEMO_CREDENTIAL: DEMO_CREDENTIAL,
        TASK4_S01_DEMO_SUBJECT: "t08-browser-reviewer",
        TASK4_S01_OPERATOR_CREDENTIAL: "s01-registered-operator-test-credential",
        TASK4_S01_OPERATOR_SUBJECT: "t08-browser-operator",
        TASK4_S08_ADMIN_CREDENTIAL: ADMIN_CREDENTIAL,
        TASK4_S08_ADMIN_SUBJECT: "t08-browser-policy-admin",
        TASK4_S08_APPROVER_CREDENTIAL: APPROVER_CREDENTIAL,
        TASK4_S08_APPROVER_SUBJECT: "t08-browser-policy-approver",
        TASK4_S08_OPERATOR_CREDENTIAL: OPERATOR_CREDENTIAL,
        TASK4_S08_OPERATOR_SUBJECT: "t08-browser-policy-operator",
        TASK4_S01_AUDITOR_CREDENTIAL: AUDITOR_CREDENTIAL,
        TASK4_S01_AUDITOR_SUBJECT: "t09-browser-auditor",
        // F-SPEC-1: the T09 governance scope gate covers all six controlled
        // identities, so the browser server must register distinct replay
        // and simulation identities beside the four governance roles.
        TASK4_S09_REPLAY_CREDENTIAL: "s09-browser-replay-test-credential",
        TASK4_S09_REPLAY_SUBJECT: "t09-browser-replay-operator",
        TASK4_S09_SIMULATION_CREDENTIAL:
          "s09-browser-simulation-test-credential",
        TASK4_S09_SIMULATION_SUBJECT: "t09-browser-simulation-operator",
        ...extraEnv,
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  child.stdout.on("data", (chunk) => output.push(chunk.toString()));
  child.stderr.on("data", (chunk) => output.push(chunk.toString()));
  // The server's own log lands next to the state file so a failing tracer
  // can be diagnosed from the real uvicorn traceback.
  const logStream = fs.createWriteStream(`${statePath}.log`, { flags: "a" });
  child.stdout.on("data", (chunk) => logStream.write(chunk));
  child.stderr.on("data", (chunk) => logStream.write(chunk));
  child.on("exit", () => logStream.end());

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
    return { baseURL, child, statePath };
  }
  const exited = once(child, "exit");
  if (child.exitCode === null) {
    child.kill("SIGKILL");
    await exited;
  }
  cleanupStatePath(statePath);
  throw new Error(`T08 React server did not start: ${output.join("")}`);
}

/** Removes exactly this server's owned SQLite state and its -wal/-shm
 * siblings plus the captured server log; every artifact is attempted even
 * if one removal rejects. */
function cleanupStatePath(statePath) {
  let firstError;
  for (const suffix of ["", "-wal", "-shm", ".log"]) {
    try {
      fs.rmSync(`${statePath}${suffix}`, { force: true });
    } catch (error) {
      if (firstError === undefined) firstError = error;
    }
  }
  if (firstError !== undefined) throw firstError;
}

async function stopServer(server) {
  const failures = [];
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

function assertNoOverflow(page) {
  return page.evaluate(
    () => document.documentElement.scrollWidth <= window.innerWidth + 1,
  );
}

/** Records page errors, console errors, and network failures (favicon
 * excluded) for the final diagnostics assertion. */
function trackPageDiagnostics(page) {
  const browserErrors = [];
  const consoleErrors = [];
  const networkErrors = [];
  page.on("pageerror", (error) => browserErrors.push(error.message));
  page.on("console", (message) => {
    if (message.type() !== "error") return;
    if (message.location().url.endsWith("/favicon.ico")) return;
    consoleErrors.push(message.text());
  });
  page.on("requestfailed", (request) => {
    const url = request.url();
    if (url.endsWith("/favicon.ico")) return;
    // Navigation/reload aborts in-flight requests; that is the browser's
    // normal teardown, not an application failure.
    if (request.failure()?.errorText === "net::ERR_ABORTED") return;
    networkErrors.push({
      url,
      failure: request.failure()?.errorText ?? "failed",
    });
  });
  return { browserErrors, consoleErrors, networkErrors };
}

/** Records every governed S08 command POST with its decoded body so the
 * tracer can prove exact request keys and exactly one POST per action. */
function trackS08Posts(page, posts) {
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (request.method() === "POST" && url.pathname.includes("/s08/api/commands/")) {
      posts.push({ url: url.pathname, body: request.postDataJSON() });
    }
  });
}

/** Records every governed S09 command POST (impact preview, hold, rollback,
 * recovery) with its decoded body; kept separate so the retained T08 request
 * discipline assertions stay byte-identical. */
function trackS09Posts(page, posts) {
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (request.method() === "POST" && url.pathname.includes("/s09/api/commands/")) {
      posts.push({ url: url.pathname, body: request.postDataJSON() });
    }
  });
}

function pad(value) {
  return String(value).padStart(2, "0");
}

/** The earliest activation minute at least 60s in the future (datetime-local
 * has minute precision; the server shares the machine clock). */
function nextActivationMinute() {
  const minutes = Math.floor((Date.now() + 60_000) / 60_000) + 1;
  const d = new Date(minutes * 60_000);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(
    d.getHours(),
  )}:${pad(d.getMinutes())}`;
}

/** The earliest activation minute at least 30s in the future: the panel's
 * bounded activation poll (80 x 1.5s = 120s) must comfortably cover the
 * worst-case 90s wait, so the T09 tracer never outlives the poll budget. */
function nextActivationMinuteShort() {
  const minutes = Math.floor((Date.now() + 30_000) / 60_000) + 1;
  const d = new Date(minutes * 60_000);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(
    d.getHours(),
  )}:${pad(d.getMinutes())}`;
}

/** Waits for the candidate workspace status to become active through the
 * UI, and reloads the page once if the panel's bounded poll ended first
 * (the poll ceiling is 120s while the server activation may be up to 90s
 * away plus worker time; the reload is the documented manual-refresh path
 * the panel itself surfaces). */
async function waitForWorkspaceActive(page, timeoutMs = 240_000) {
  try {
    await expect(page.getByTestId("t08-workspace-status")).toHaveText(
      "active",
      { timeout: timeoutMs },
    );
  } catch {
    await page.reload({ waitUntil: "networkidle" });
    await expect(page.getByTestId("t08-workspace-status")).toHaveText(
      "active",
      { timeout: timeoutMs },
    );
  }
}

async function waitForCompleteRun(reviewer, baseURL, applicationId) {
  const deadline = Date.now() + 60_000;
  while (Date.now() < deadline) {
    const response = await reviewer.request.get(
      `${baseURL}/controlled/s01/api/queries/applications/${encodeURIComponent(
        applicationId,
      )}/history`,
    );
    expect(response.ok()).toBeTruthy();
    const body = await response.json();
    const run = (body.runs || []).find((candidate) => candidate.current === true);
    if (run !== undefined && run.status === "complete") {
      return run;
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`S01 run never completed for ${applicationId}`);
}

/** Waits until the S01 queue exposes the manual-review item for the
 * submitted application (the successor run's manual projection). */
async function waitForManualItem(reviewer, baseURL, applicationId) {
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    const queue = await reviewer.request.get(
      `${baseURL}/controlled/s01/api/queries/queue`,
    );
    expect(queue.ok()).toBeTruthy();
    const body = await queue.json();
    const item = (body.items || []).find(
      (candidate) => candidate.application_id === applicationId,
    );
    if (item !== undefined) return item;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`manual review work never appeared for ${applicationId}`);
}

for (const viewport of VIEWPORTS) {
  test(`T08 production tracer (${viewport.label}): governed policy-release workspace with independent Admin/Approver roles`, async ({
    browser,
  }) => {
    test.setTimeout(300_000);
    const resources = {};
    let failure;
    try {
      resources.server = await startServer();
      const server = resources.server;

      // ---- Rule Administrator: draft -> freeze -> validate -> review ----
      resources.adminContext = await browser.newContext({
        viewport,
        extraHTTPHeaders: { Authorization: `Bearer ${ADMIN_CREDENTIAL}` },
      });
      const admin = await resources.adminContext.newPage();
      const adminDiag = trackPageDiagnostics(admin);
      const adminPosts = [];
      trackS08Posts(admin, adminPosts);

      const shellResponse = await admin.goto(`${server.baseURL}${S08_URL}`, {
        waitUntil: "networkidle",
      });
      expect(shellResponse.status()).toBe(200);
      expect(shellResponse.headers()["cache-control"]).toContain("no-store");
      await expect(admin.getByTestId("t08-draft-workflow")).toBeVisible();
      await expect(admin.getByTestId("s08-boundary-track")).toHaveText(
        "C-DEMO",
      );
      await expect(admin.getByTestId("s08-boundary-gate")).toHaveText("S08");

      await admin.getByLabel("来源包标识").fill(SOURCE_BUNDLE_ID);
      await admin.getByTestId("t08-import-button").click();
      await expect(admin.getByTestId("t08-draft-editor")).toBeVisible();
      await admin.getByLabel("适用范围").fill(S08_SCOPE);
      await admin.getByLabel("来源").fill(SOURCE_BUNDLE_ID);
      await admin.getByLabel("变更原因").fill("T08 browser tracer");
      await admin.getByLabel("生效起始").fill("2000-01-01T00:00");
      await admin.getByTestId("t08-revise-button").click();
      await expect(admin.getByTestId("t08-revise-ok")).toBeVisible();
      await admin.getByTestId("t08-freeze-button").click();
      await admin.waitForURL(/[?&]candidate=/);
      await expect(admin.getByTestId("t08-workspace-status")).toHaveText(
        "candidate",
      );
      await expect(admin.getByTestId("t08-workspace-role")).toHaveText("admin");
      const candidateId = new URL(admin.url()).searchParams.get("candidate");

      await admin.getByTestId("t08-validate-button").click();
      await expect(admin.getByTestId("t08-workspace-status")).toHaveText(
        "validated",
        { timeout: 90_000 },
      );
      await expect(admin.getByTestId("t08-validation")).toBeVisible();
      const manifestDigest = await admin
        .getByTestId("t08-workspace-digest")
        .textContent();

      await admin.getByTestId("t08-submit-button").click();
      await expect(admin.getByTestId("t08-workspace-status")).toHaveText(
        "in_review",
        { timeout: 30_000 },
      );

      // ---- Independent Policy Approver: exact digest review + approval ----
      resources.approverContext = await browser.newContext({
        viewport,
        extraHTTPHeaders: { Authorization: `Bearer ${APPROVER_CREDENTIAL}` },
      });
      const approver = await resources.approverContext.newPage();
      const approverDiag = trackPageDiagnostics(approver);
      const approverPosts = [];
      trackS08Posts(approver, approverPosts);

      await approver.goto(
        `${server.baseURL}${S08_URL}?candidate=${encodeURIComponent(
          candidateId,
        )}`,
        { waitUntil: "networkidle" },
      );
      await expect(approver.getByTestId("t08-workspace-status")).toHaveText(
        "in_review",
      );
      await expect(approver.getByTestId("t08-workspace-role")).toHaveText(
        "approver",
      );
      expect(await approver.getByTestId("t08-workspace-digest").textContent()).toBe(
        manifestDigest,
      );
      await expect(approver.getByTestId("t08-approve-form")).toBeVisible();
      await expect(approver.getByTestId("t08-reject-form")).toBeVisible();
      // The recovery anchor is prefilled from the server workspace.
      const anchor = await approver
        .getByLabel("回滚发布标识")
        .inputValue();
      expect(anchor.length).toBeGreaterThan(0);

      const activationLocal = nextActivationMinute();
      // Keyboard operability: the activation input accepts keyboard focus
      // and typed entry (proven by the active element), then the approve
      // action is triggered by a real Enter keypress on the button.
      const activationInput = approver.getByLabel("生效时间");
      await activationInput.focus();
      expect(
        await approver.evaluate(() =>
          document.activeElement?.getAttribute("aria-label"),
        ),
      ).toBe("生效时间");
      await activationInput.fill(activationLocal);
      // Approval requires the explicit preview first (P-1): the preview
      // button runs the impact computation and the accepted DTO renders
      // before the keyboard-only approval.
      const previewButton = approver.getByTestId("t08-preview-button");
      await previewButton.focus();
      await approver.keyboard.press("Enter");
      await expect(approver.getByTestId("t08-preview")).toBeVisible();
      const approveButton = approver.getByTestId("t08-approve-button");
      await approveButton.focus();
      expect(
        await approver.evaluate(() =>
          document.activeElement?.getAttribute("data-testid"),
        ),
      ).toBe("t08-approve-button");
      await approver.keyboard.press("Enter");
      await expect(approver.getByTestId("t08-action-ok")).toBeVisible();
      await expect(approver.getByTestId("t08-workspace-status")).toHaveText(
        "approved",
        { timeout: 30_000 },
      );

      // ---- Admin: schedule the immutable binding, wait for activation ----
      await admin.reload({ waitUntil: "networkidle" });
      await expect(admin.getByTestId("t08-workspace-status")).toHaveText(
        "approved",
      );
      // The binding time is server-owned; capture it before the workspace
      // leaves the approved state (the schedule form only exists then).
      await expect(admin.getByTestId("t08-binding-time")).not.toHaveText("—");
      const bindingTimeText = await admin
        .getByTestId("t08-binding-time")
        .textContent();
      await admin.getByTestId("t08-schedule-button").focus();
      await admin.keyboard.press("Enter");
      await expect(admin.getByTestId("t08-workspace-status")).toHaveText(
        "scheduled",
        { timeout: 30_000 },
      );
      // The server-owned activation job flips the status; the UI's bounded
      // poll converges on it (never a client-side active claim).
      await expect(admin.getByTestId("t08-workspace-status")).toHaveText(
        "active",
        { timeout: 180_000 },
      );
      // The workspace renders the server-owned governance timeline: the
      // activation event and the approval that produced this release.
      await expect(admin.getByTestId("t08-events")).toContainText("activated");
      await expect(admin.getByTestId("t08-events")).toContainText("approved");

      // ---- S01 successor run pins the complete governed release ----
      resources.demoContext = await browser.newContext({
        viewport,
        extraHTTPHeaders: { Authorization: `Bearer ${DEMO_CREDENTIAL}` },
      });
      const reviewer = await resources.demoContext.newPage();
      // Loading the S01 shell first registers the reviewer session cookie in
      // this context; the admission POST then carries it (the S08 shell never
      // issues an S01 session, so this ordering is required for role reads).
      const s01Shell = await reviewer.goto(`${server.baseURL}${S01_URL}`, {
        waitUntil: "networkidle",
      });
      expect(s01Shell.status()).toBe(200);
      const admission = await reviewer.request.post(
        `${server.baseURL}/controlled/s01/api/commands/submit`,
        { data: { scenario_id: SCENARIO, idempotency_key: "t08-react-admission" } },
      );
      expect(admission.ok()).toBeTruthy();
      const accepted = await admission.json();
      const run = await waitForCompleteRun(
        reviewer,
        server.baseURL,
        accepted.application_id,
      );
      expect(run.candidate_id).toBe(candidateId);
      expect(run.manifest_digest).toBe(manifestDigest);
      expect(run.activation_event_id).toBeTruthy();
      expect(run.active_generation).toBe(2);
      expect((run.components || []).length).toBeGreaterThan(0);

      // The Reviewer UI renders the governed pin inside the run history.
      const manualItem = await waitForManualItem(
        reviewer,
        server.baseURL,
        accepted.application_id,
      );
      // The queue projection is fetched on load, so a fresh load surfaces the
      // successor work item before the run pin is asserted.
      await reviewer.reload({ waitUntil: "networkidle" });
      const queueLink = reviewer.getByRole("link", {
        name: new RegExp(manualItem.work_item_id),
      });
      await expect(queueLink).toBeVisible();
      await queueLink.click();
      await expect(reviewer.getByTestId("review-history-run").first()).toBeVisible();
      const historyText = await reviewer
        .getByTestId("review-history-run")
        .first()
        .innerText();
      expect(historyText).toContain(candidateId);
      expect(historyText).toContain(manifestDigest);
      expect(historyText).toContain("2"); // active generation
      expect(historyText).toContain("·"); // component rows

      // ---- Protected-behavior evidence: prior active stays authoritative
      // while the successor activates (active query agrees with run pin). ----
      const activeQuery = await admin.request.get(
        `${server.baseURL}/controlled/s08/api/queries/active?scope=${encodeURIComponent(
          S08_SCOPE,
        )}`,
      );
      expect(activeQuery.ok()).toBeTruthy();
      const active = await activeQuery.json();
      expect(active.candidate_id).toBe(candidateId);
      expect(active.manifest_digest).toBe(manifestDigest);

      // ---- Diagnostics: zero page/console/network errors ----
      for (const [name, diag] of [
        ["admin", adminDiag],
        ["approver", approverDiag],
      ]) {
        expect(
          diag.browserErrors,
          `${name} page errors`,
        ).toEqual([]);
        expect(diag.consoleErrors, `${name} console errors`).toEqual([]);
        expect(diag.networkErrors, `${name} network failures`).toEqual([]);
      }

      // ---- Exact request discipline: one POST per action, no duplicates ----
      const adminActions = adminPosts.map((post) => post.url.split("/").pop());
      const adminKeys = adminPosts.map((post) => post.body.idempotency_key);
      expect(new Set(adminKeys).size).toBe(adminKeys.length);
      expect(adminActions).toEqual([
        "import_legacy",
        "revise_draft",
        "freeze_candidate",
        "request_validation",
        "submit_review",
        "schedule",
      ]);
      const approverActions = approverPosts.map((post) => post.url.split("/").pop());
      expect(approverActions).toEqual(["approve"]);
      const approveBody = approverPosts[0].body;
      expect(approveBody.candidate_id).toBe(candidateId);
      expect(approveBody.recovery_release_id).toBe(anchor);
      const scheduleBody = adminPosts.find(
        (post) => post.url.endsWith("/commands/schedule"),
      ).body;
      // The schedule POST carries exactly the binding time the server
      // returned; the Admin can never substitute an activation instant.
      expect(scheduleBody.activation_at).toBe(Number(bindingTimeText));
      expect(approveBody.activation_time).toBe(Number(bindingTimeText));

      // ---- No authoritative data in browser storage ----
      for (const page of [admin, approver, reviewer]) {
        expect(await page.evaluate(() => localStorage.length)).toBe(0);
        expect(await page.evaluate(() => sessionStorage.length)).toBe(0);
      }

      // ---- Viewport evidence: no horizontal overflow ----
      for (const page of [admin, approver, reviewer]) {
        expect(await assertNoOverflow(page)).toBe(true);
      }
    } catch (error) {
      failure = error;
      throw error;
    } finally {
      for (const name of [
        "demoContext",
        "approverContext",
        "adminContext",
      ]) {
        try {
          if (resources[name]) await resources[name].close();
        } catch {
          // Best-effort context close after a failure.
        }
      }
      if (resources.server) {
        try {
          await stopServer(resources.server);
        } catch (error) {
          if (failure === undefined) throw error;
        }
      }
    }
  });
}

test("T08 production terminal: a rejected validation is rendered with evidence and never activates", async ({
  browser,
}) => {
  test.setTimeout(240_000);
  // A private S08 corpus: seeded from the server fixtures so the bootstrap
  // activation succeeds, then emptied so the next candidate's validation
  // fails closed into the authoritative rejected terminal (never touching
  // the shared fixture tree).
  const corpusRoot = fs.mkdtempSync("/tmp/xiaopeng-t08-rejected-corpus-");
  fs.cpSync(path.join(ROOT, "fixtures", "applications"), corpusRoot, {
    recursive: true,
  });
  const resources = {};
  let failure;
  try {
    resources.server = await startServer({
      TASK4_S08_TEST_CORPUS_ROOT: corpusRoot,
    });
    const server = resources.server;

    resources.adminContext = await browser.newContext({
      viewport: { width: 1280, height: 800 },
      extraHTTPHeaders: { Authorization: `Bearer ${ADMIN_CREDENTIAL}` },
    });
    const admin = await resources.adminContext.newPage();
    const adminDiag = trackPageDiagnostics(admin);

    // Bootstrap must complete before the corpus disappears.
    const activeQuery = await admin.request.get(
      `${server.baseURL}/controlled/s08/api/queries/active?scope=${encodeURIComponent(
        S08_SCOPE,
      )}`,
    );
    expect(activeQuery.ok()).toBeTruthy();
    const bootstrap = await activeQuery.json();
    expect(bootstrap.status).toBe("active");
    expect(bootstrap.candidate_id).toBeTruthy();
    fs.rmSync(corpusRoot, { recursive: true, force: true });
    fs.mkdirSync(corpusRoot);

    await admin.goto(`${server.baseURL}${S08_URL}`, {
      waitUntil: "networkidle",
    });
    await expect(admin.getByTestId("t08-draft-workflow")).toBeVisible();
    await admin.getByLabel("来源包标识").fill(SOURCE_BUNDLE_ID);
    await admin.getByTestId("t08-import-button").click();
    await expect(admin.getByTestId("t08-draft-editor")).toBeVisible();
    await admin.getByLabel("适用范围").fill(S08_SCOPE);
    await admin.getByLabel("来源").fill(SOURCE_BUNDLE_ID);
    await admin.getByLabel("变更原因").fill("T08 rejected terminal");
    await admin.getByLabel("生效起始").fill("2000-01-01T00:00");
    await admin.getByTestId("t08-revise-button").click();
    await expect(admin.getByTestId("t08-revise-ok")).toBeVisible();
    await admin.getByTestId("t08-freeze-button").click();
    await admin.waitForURL(/[?&]candidate=/);
    await expect(admin.getByTestId("t08-workspace-status")).toHaveText(
      "candidate",
    );
    await admin.getByTestId("t08-validate-button").click();
    // The poll converges on the authoritative rejected terminal: pending
    // stops, the registered reason and the server evidence render, and no
    // activation surface exists.
    await expect(admin.getByTestId("t08-validation-rejected")).toBeVisible({
      timeout: 90_000,
    });
    await expect(admin.getByTestId("t08-validation-rejected")).toContainText(
      "S08_VALIDATION_REJECTED",
    );
    await expect(
      admin.getByTestId("t08-validation-rejected-evidence"),
    ).toContainText("corpus_bound: fail");
    await expect(admin.getByTestId("t08-workspace-status")).toHaveText(
      "rejected",
    );
    await expect(admin.getByTestId("t08-polling")).toBeHidden();
    await expect(admin.getByTestId("t08-validate-button")).toBeHidden();
    await expect(admin.getByTestId("t08-approve-form")).toBeHidden();
    await expect(admin.getByTestId("t08-schedule-form")).toBeHidden();
    // The prior active release stays visible and authoritative.
    await expect(admin.getByTestId("t08-workspace-anchor")).toContainText(
      bootstrap.candidate_id,
    );
    const activeAfter = await admin.request.get(
      `${server.baseURL}/controlled/s08/api/queries/active?scope=${encodeURIComponent(
        S08_SCOPE,
      )}`,
    );
    expect(activeAfter.ok()).toBeTruthy();
    expect((await activeAfter.json()).candidate_id).toBe(bootstrap.candidate_id);

    expect(adminDiag.browserErrors).toEqual([]);
    expect(adminDiag.consoleErrors).toEqual([]);
    expect(adminDiag.networkErrors).toEqual([]);
    expect(await assertNoOverflow(admin)).toBe(true);
  } catch (error) {
    failure = error;
    throw error;
  } finally {
    try {
      if (resources.adminContext) await resources.adminContext.close();
    } catch {
      // Best-effort close.
    }
    if (resources.server) {
      try {
        await stopServer(resources.server);
      } catch (error) {
        if (failure === undefined) throw error;
      }
    }
    fs.rmSync(corpusRoot, { recursive: true, force: true });
  }
});

test("T08 production terminal: an activation diagnostic failure keeps the prior active visible", async ({
  browser,
}) => {
  test.setTimeout(300_000);
  // The worker's activation write point faults, so the scheduled candidate's
  // activation ends diagnostic: the UI must present the failure without ever
  // claiming active, with the prior-active anchor still visible.
  const resources = {};
  let failure;
  try {
    resources.server = await startServer({
      TASK4_S08_TEST_FAULT_POINT: "s08.activation",
    });
    const server = resources.server;

    resources.adminContext = await browser.newContext({
      viewport: { width: 1280, height: 800 },
      extraHTTPHeaders: { Authorization: `Bearer ${ADMIN_CREDENTIAL}` },
    });
    const admin = await resources.adminContext.newPage();
    const adminDiag = trackPageDiagnostics(admin);

    const activeQuery = await admin.request.get(
      `${server.baseURL}/controlled/s08/api/queries/active?scope=${encodeURIComponent(
        S08_SCOPE,
      )}`,
    );
    expect(activeQuery.ok()).toBeTruthy();
    const bootstrap = await activeQuery.json();
    expect(bootstrap.status).toBe("active");

    await admin.goto(`${server.baseURL}${S08_URL}`, {
      waitUntil: "networkidle",
    });
    await expect(admin.getByTestId("t08-draft-workflow")).toBeVisible();
    await admin.getByLabel("来源包标识").fill(SOURCE_BUNDLE_ID);
    await admin.getByTestId("t08-import-button").click();
    await expect(admin.getByTestId("t08-draft-editor")).toBeVisible();
    await admin.getByLabel("适用范围").fill(S08_SCOPE);
    await admin.getByLabel("来源").fill(SOURCE_BUNDLE_ID);
    await admin.getByLabel("变更原因").fill("T08 activation failure");
    await admin.getByLabel("生效起始").fill("2000-01-01T00:00");
    await admin.getByTestId("t08-revise-button").click();
    await expect(admin.getByTestId("t08-revise-ok")).toBeVisible();
    await admin.getByTestId("t08-freeze-button").click();
    await admin.waitForURL(/[?&]candidate=/);
    await admin.getByTestId("t08-validate-button").click();
    await expect(admin.getByTestId("t08-workspace-status")).toHaveText(
      "validated",
      { timeout: 90_000 },
    );
    await admin.getByTestId("t08-submit-button").click();
    await expect(admin.getByTestId("t08-workspace-status")).toHaveText(
      "in_review",
      { timeout: 30_000 },
    );

    resources.approverContext = await browser.newContext({
      viewport: { width: 1280, height: 800 },
      extraHTTPHeaders: { Authorization: `Bearer ${APPROVER_CREDENTIAL}` },
    });
    const approver = await resources.approverContext.newPage();
    const candidateId = new URL(admin.url()).searchParams.get("candidate");
    await approver.goto(
      `${server.baseURL}${S08_URL}?candidate=${encodeURIComponent(candidateId)}`,
      { waitUntil: "networkidle" },
    );
    await expect(approver.getByTestId("t08-workspace-status")).toHaveText(
      "in_review",
    );
    await approver.getByLabel("生效时间").fill(nextActivationMinute());
    // The approve action requires the explicit preview first (P-1).
    await approver.getByTestId("t08-preview-button").click();
    await expect(approver.getByTestId("t08-preview")).toBeVisible();
    await approver.getByTestId("t08-approve-button").click();
    await expect(approver.getByTestId("t08-workspace-status")).toHaveText(
      "approved",
      { timeout: 30_000 },
    );

    await admin.reload({ waitUntil: "networkidle" });
    await expect(admin.getByTestId("t08-workspace-status")).toHaveText(
      "approved",
    );
    await expect(admin.getByTestId("t08-binding-time")).not.toHaveText("—");
    await admin.getByTestId("t08-schedule-button").click();
    await expect(admin.getByTestId("t08-workspace-status")).toHaveText(
      "scheduled",
      { timeout: 30_000 },
    );

    // The worker's activation attempt fails closed into diagnostic: the UI
    // presents the registered stable reason (never the internal write point
    // or a raw exception), the candidate stays scheduled (never active), and
    // the prior-active anchor remains visible.
    await expect(admin.getByTestId("t08-activation-failed")).toBeVisible({
      timeout: 180_000,
    });
    await expect(admin.getByTestId("t08-activation-failed")).toContainText(
      "S08_ACTIVATION_INTERNAL",
    );
    await expect(admin.getByTestId("t08-activation-failed")).not.toContainText(
      "s08.activation",
    );
    await expect(admin.getByTestId("t08-activation-failed")).not.toContainText(
      "RuntimeError",
    );
    await expect(admin.getByTestId("t08-workspace-status")).toHaveText(
      "scheduled",
    );
    await expect(admin.getByTestId("t08-workspace-anchor")).toContainText(
      bootstrap.candidate_id,
    );
    const activeAfter = await admin.request.get(
      `${server.baseURL}/controlled/s08/api/queries/active?scope=${encodeURIComponent(
        S08_SCOPE,
      )}`,
    );
    expect(activeAfter.ok()).toBeTruthy();
    expect((await activeAfter.json()).candidate_id).toBe(bootstrap.candidate_id);

    // The prior active release stays usable at the product boundary: a new
    // S01 run submitted after the activation failure completes pinned to the
    // bootstrap generation with its exact manifest identity.
    resources.demoContext = await browser.newContext({
      viewport: { width: 1280, height: 800 },
      extraHTTPHeaders: { Authorization: `Bearer ${DEMO_CREDENTIAL}` },
    });
    const demo = await resources.demoContext.newPage();
    const s01Shell = await demo.goto(`${server.baseURL}${S01_URL}`, {
      waitUntil: "networkidle",
    });
    expect(s01Shell.status()).toBe(200);
    const admission = await demo.request.post(
      `${server.baseURL}/controlled/s01/api/commands/submit`,
      {
        data: {
          scenario_id: SCENARIO,
          idempotency_key: "t08-activation-failure-run-1",
        },
      },
    );
    expect(admission.ok()).toBeTruthy();
    const accepted = await admission.json();
    const run = await waitForCompleteRun(
      demo,
      server.baseURL,
      accepted.application_id,
    );
    expect(run.candidate_id).toBe(bootstrap.candidate_id);
    expect(run.manifest_digest).toBe(bootstrap.manifest_digest);
    expect(run.activation_event_id).toBeTruthy();
    expect(run.active_generation).toBe(1);

    expect(adminDiag.browserErrors).toEqual([]);
    expect(adminDiag.consoleErrors).toEqual([]);
    expect(adminDiag.networkErrors).toEqual([]);
    expect(await assertNoOverflow(admin)).toBe(true);
  } catch (error) {
    failure = error;
    throw error;
  } finally {
    for (const name of ["approverContext", "adminContext", "demoContext"]) {
      try {
        if (resources[name]) await resources[name].close();
      } catch {
        // Best-effort close.
      }
    }
    if (resources.server) {
      try {
        await stopServer(resources.server);
      } catch (error) {
        if (failure === undefined) throw error;
      }
    }
  }
});

/** Waits until Lifecycle has consumed the imposed hold: the application has
 * runs and none of them is current (the hold frame is in force).  Every
 * authority read must succeed: an unexpected non-2xx history response fails
 * the tracer immediately instead of passing with an unrecorded failure. */
async function waitForNoCurrentRun(reviewer, baseURL, applicationId) {
  const deadline = Date.now() + 60_000;
  while (Date.now() < deadline) {
    const response = await reviewer.request.get(
      `${baseURL}/controlled/s01/api/queries/applications/${encodeURIComponent(
        applicationId,
      )}/history`,
    );
    expect(response.ok()).toBeTruthy();
    const body = await response.json();
    const runs = body.runs || [];
    if (runs.length > 0 && !runs.some((run) => run.current === true)) {
      return body;
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`current run never became non-current for ${applicationId}`);
}

/** Waits until the operational re-evaluation under a target active
 * generation produced a current complete run (recovery recheck).  Every
 * authority read must succeed; an unexpected non-2xx history response fails
 * the tracer immediately. */
async function waitForCurrentGeneration(reviewer, baseURL, applicationId, generation) {
  const deadline = Date.now() + 90_000;
  while (Date.now() < deadline) {
    const response = await reviewer.request.get(
      `${baseURL}/controlled/s01/api/queries/applications/${encodeURIComponent(
        applicationId,
      )}/history`,
    );
    expect(response.ok()).toBeTruthy();
    const body = await response.json();
    const run = (body.runs || []).find(
      (candidate) =>
        candidate.current === true &&
        candidate.status === "complete" &&
        candidate.active_generation === generation,
    );
    if (run !== undefined) return body;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(
    `generation ${generation} run never became current for ${applicationId}`,
  );
}

/** The immutable run facts the recovery evidence compares: the derived
 * ``currentness_reason`` legitimately evolves as the recheck completes, so
 * deep equality is asserted on the stable pinned release facts only. */
function stableRunFacts(runs) {
  return runs.map((run) => ({
    run_id: run.run_id,
    status: run.status,
    cycle: run.cycle,
    active_generation: run.active_generation,
    candidate_id: run.candidate_id,
    release_id: run.release_id,
    release_digest: run.release_digest,
    activation_event_id: run.activation_event_id,
  }));
}

/** The authoritative release facts from the Auditor's workspace read: the
 * append-only Governance event refs and the minimized Security Audit refs
 * come from one closed response under the Auditor identity (P-3); the
 * /queries/events endpoint stays the release-history view and is never
 * relabeled as audit. */
async function fetchReleaseFacts(request, baseURL) {
  const workspaceResponse = await request.get(
    `${baseURL}/controlled/s09/api/queries/workspace`,
    { headers: { Authorization: `Bearer ${AUDITOR_CREDENTIAL}` } },
  );
  expect(workspaceResponse.ok()).toBeTruthy();
  const workspace = await workspaceResponse.json();
  return {
    workspaceEvents: workspace.events,
    auditEvents: workspace.audit_events,
  };
}

for (const viewport of VIEWPORTS) {
  test(`T09 production tracer (${viewport.label}): impact, scoped hold, compatible rollback activation and explicit recovery across five roles`, async ({
    browser,
  }) => {
    test.setTimeout(600_000);
    const resources = {};
    let failure;
    try {
      resources.server = await startServer();
      const server = resources.server;

      // The bootstrap (prior) release is the recorded known-good anchor.
      resources.adminContext = await browser.newContext({
        viewport,
        extraHTTPHeaders: { Authorization: `Bearer ${ADMIN_CREDENTIAL}` },
      });
      const admin = await resources.adminContext.newPage();
      const adminDiag = trackPageDiagnostics(admin);
      const adminPosts = [];
      trackS08Posts(admin, adminPosts);

      const bootstrapQuery = await admin.request.get(
        `${server.baseURL}/controlled/s08/api/queries/active?scope=${encodeURIComponent(
          S08_SCOPE,
        )}`,
      );
      expect(bootstrapQuery.ok()).toBeTruthy();
      const bootstrap = await bootstrapQuery.json();
      expect(bootstrap.status).toBe("active");
      const bootstrapCandidateId = bootstrap.candidate_id;

      // ---- Rule Administrator: the existing T08 smoke path to review ----
      const shellResponse = await admin.goto(`${server.baseURL}${S08_URL}`, {
        waitUntil: "networkidle",
      });
      expect(shellResponse.status()).toBe(200);
      await admin.getByLabel("来源包标识").fill(SOURCE_BUNDLE_ID);
      await admin.getByTestId("t08-import-button").click();
      await expect(admin.getByTestId("t08-draft-editor")).toBeVisible();
      await admin.getByLabel("适用范围").fill(S08_SCOPE);
      await admin.getByLabel("来源").fill(SOURCE_BUNDLE_ID);
      await admin.getByLabel("变更原因").fill("T09 browser tracer");
      await admin.getByLabel("生效起始").fill("2000-01-01T00:00");
      await admin.getByTestId("t08-revise-button").click();
      await expect(admin.getByTestId("t08-revise-ok")).toBeVisible();
      await admin.getByTestId("t08-freeze-button").click();
      await admin.waitForURL(/[?&]candidate=/);
      const changedCandidateId = new URL(admin.url()).searchParams.get(
        "candidate",
      );
      await admin.getByTestId("t08-validate-button").click();
      await expect(admin.getByTestId("t08-workspace-status")).toHaveText(
        "validated",
        { timeout: 90_000 },
      );
      await admin.getByTestId("t08-submit-button").click();
      await expect(admin.getByTestId("t08-workspace-status")).toHaveText(
        "in_review",
        { timeout: 30_000 },
      );

      // ---- Independent Policy Approver: approve the changed release ----
      resources.approverContext = await browser.newContext({
        viewport,
        extraHTTPHeaders: { Authorization: `Bearer ${APPROVER_CREDENTIAL}` },
      });
      const approver = await resources.approverContext.newPage();
      const approverDiag = trackPageDiagnostics(approver);
      const approverPosts = [];
      trackS09Posts(approver, approverPosts);

      await approver.goto(
        `${server.baseURL}${S08_URL}?candidate=${encodeURIComponent(
          changedCandidateId,
        )}`,
        { waitUntil: "networkidle" },
      );
      await expect(approver.getByTestId("t08-workspace-status")).toHaveText(
        "in_review",
      );
      await approver.getByLabel("生效时间").fill(nextActivationMinuteShort());
      // Approval requires an explicit preview first (P-1): the preview
      // button runs the server impact computation, the accepted DTO renders
      // its manifest/digest/scope/counts/expansion/generation, and only
      // then does the keyboard-only approval become available.
      const previewButton = approver.getByTestId("t08-preview-button");
      await previewButton.focus();
      await approver.keyboard.press("Enter");
      await expect(approver.getByTestId("t08-preview")).toBeVisible();
      await expect(approver.getByTestId("t08-preview")).toHaveAttribute(
        "role",
        "status",
      );
      await expect(approver.getByTestId("t08-preview-manifest")).not.toHaveText(
        "",
      );
      await expect(approver.getByTestId("t08-preview-members")).toHaveText(
        /\d+/,
      );
      await expect(approver.getByTestId("t08-preview-expansion")).toHaveText(
        /未扩张|已扩张到完整范围/,
      );
      await expect(approver.getByTestId("t08-preview-generation")).toHaveText(
        /\d+/,
      );
      const approveButton = approver.getByTestId("t08-approve-button");
      await expect(approveButton).toBeEnabled();
      await approveButton.focus();
      await approver.keyboard.press("Enter");
      await expect(approver.getByTestId("t08-action-ok")).toBeVisible();
      await expect(approver.getByTestId("t08-workspace-status")).toHaveText(
        "approved",
        { timeout: 30_000 },
      );

      // ---- Admin: schedule and wait for the changed release activation ----
      await admin.reload({ waitUntil: "networkidle" });
      await expect(admin.getByTestId("t08-workspace-status")).toHaveText(
        "approved",
      );
      await expect(admin.getByTestId("t08-binding-time")).not.toHaveText("—");
      await admin.getByTestId("t08-schedule-button").focus();
      await admin.keyboard.press("Enter");
      await expect(admin.getByTestId("t08-workspace-status")).toHaveText(
        "scheduled",
        { timeout: 30_000 },
      );
      await waitForWorkspaceActive(admin);

      // ---- An affected application completes a current run under the
      // changed (later failed) release ----
      resources.demoContext = await browser.newContext({
        viewport,
        extraHTTPHeaders: { Authorization: `Bearer ${DEMO_CREDENTIAL}` },
      });
      const reviewer = await resources.demoContext.newPage();
      const reviewerDiag = trackPageDiagnostics(reviewer);
      const s01Shell = await reviewer.goto(`${server.baseURL}${S01_URL}`, {
        waitUntil: "networkidle",
      });
      expect(s01Shell.status()).toBe(200);
      const admission = await reviewer.request.post(
        `${server.baseURL}/controlled/s01/api/commands/submit`,
        {
          data: {
            scenario_id: SCENARIO,
            idempotency_key: "t09-react-admission-1",
          },
        },
      );
      expect(admission.ok()).toBeTruthy();
      const accepted = await admission.json();
      const failedRun = await waitForCompleteRun(
        reviewer,
        server.baseURL,
        accepted.application_id,
      );
      expect(failedRun.active_generation).toBe(2);
      expect(failedRun.candidate_id).toBe(changedCandidateId);

      // ---- Operator: impose a scoped, non-expiring safety hold ----
      resources.operatorContext = await browser.newContext({
        viewport,
        extraHTTPHeaders: { Authorization: `Bearer ${OPERATOR_CREDENTIAL}` },
      });
      const operator = await resources.operatorContext.newPage();
      const operatorDiag = trackPageDiagnostics(operator);
      const operatorPosts = [];
      trackS09Posts(operator, operatorPosts);

      const s09Shell = await operator.goto(`${server.baseURL}${S09_URL}`, {
        waitUntil: "networkidle",
      });
      expect(s09Shell.status()).toBe(200);
      expect(s09Shell.headers()["cache-control"]).toContain("no-store");
      await expect(operator.getByTestId("s09-boundary-track")).toHaveText(
        "C-DEMO",
      );
      await expect(operator.getByTestId("s09-boundary-gate")).toHaveText(
        "S09",
      );
      await expect(operator.getByTestId("t09-role")).toHaveText("operator");
      await expect(operator.getByTestId("t09-active-release")).toContainText(
        "代次 2",
      );
      await expect(operator.getByTestId("t09-recovery-anchor")).toHaveText(
        bootstrapCandidateId,
      );
      await expect(operator.getByTestId("t09-holds-empty")).toBeVisible();

      await operator.getByLabel("冻结原因码").fill("S09_TEST_HOLD");
      await operator.getByTestId("t09-impose-button").focus();
      expect(
        await operator.evaluate(() =>
          document.activeElement?.getAttribute("data-testid"),
        ),
      ).toBe("t09-impose-button");
      await operator.keyboard.press("Enter");
      await expect(operator.getByTestId("t09-action-ok")).toBeVisible();
      await expect(operator.getByTestId("t09-hold")).toBeVisible();
      await expect(operator.getByTestId("t09-hold-scope")).toHaveText(
        "open_cycle",
      );
      await expect(operator.getByTestId("t09-hold-reason")).toHaveText(
        "S09_TEST_HOLD",
      );
      await expect(operator.getByTestId("t09-hold-criterion")).toHaveText(
        "s09-hold-recovery/1",
      );
      // The hold never auto-expires: no expiry surface exists on the page.
      expect(await operator.getByTestId("t09-hold").innerText()).not.toContain(
        "expires",
      );

      // Issue #54 cutover: the same-artifact legacy fallback metadata was
      // removed from the React shell; deployment-only rollback to the
      // prior wheel (where /, /controlled/s01 and /controlled/s02 resolve
      // to their legacy owners) is rehearsed in the installed release
      // harness under rollback-probe.

      // The hold is consumed: the affected application's old run is no
      // longer current (stale/recheck fact, server-owned).
      await waitForNoCurrentRun(
        reviewer,
        server.baseURL,
        accepted.application_id,
      );

      // ---- Operator: compatible rollback through the known-good release ----
      expect(
        await operator.getByLabel("回滚发布标识").inputValue(),
      ).toBe(bootstrapCandidateId);
      await operator.getByLabel("回滚原因码").fill("S09_TEST_ROLLBACK");
      await operator.getByTestId("t09-rollback-button").focus();
      await operator.keyboard.press("Enter");
      await expect(operator.getByTestId("t09-rollback-result")).toBeVisible();
      await expect(operator.getByTestId("t09-rollback-compatibility")).toContainText(
        "S09_ROLLBACK_COMPATIBLE",
      );
      const rollbackHref = await operator
        .getByTestId("t09-rollback-link")
        .getAttribute("href");
      const rollbackCandidateId = new URL(
        rollbackHref,
        server.baseURL,
      ).searchParams.get("candidate");
      expect(rollbackCandidateId).toBeTruthy();
      await expect(operator.getByTestId("t09-events")).toContainText(
        "rollback_proposed",
      );

      // ---- Admin: continue the rollback candidate through the retained
      // /controlled/s08/react route, navigating the exact href the page
      // served (S-3) ----
      await admin.goto(new URL(rollbackHref, server.baseURL).href, {
        waitUntil: "networkidle",
      });
      await expect(admin.getByTestId("t08-workspace-status")).toHaveText(
        "validated",
      );
      await admin.getByTestId("t08-submit-button").click();
      await expect(admin.getByTestId("t08-workspace-status")).toHaveText(
        "in_review",
        { timeout: 30_000 },
      );

      // ---- Independent Policy Approver: approve the rollback candidate ----
      await approver.goto(
        `${server.baseURL}${S08_URL}?candidate=${encodeURIComponent(
          rollbackCandidateId,
        )}`,
        { waitUntil: "networkidle" },
      );
      await expect(approver.getByTestId("t08-workspace-status")).toHaveText(
        "in_review",
      );
      await approver.getByLabel("生效时间").fill(nextActivationMinuteShort());
      // The rollback approval also requires the explicit preview first.
      const rollbackPreviewButton = approver.getByTestId("t08-preview-button");
      await rollbackPreviewButton.focus();
      await approver.keyboard.press("Enter");
      await expect(approver.getByTestId("t08-preview")).toBeVisible();
      await approver.getByTestId("t08-approve-button").focus();
      await approver.keyboard.press("Enter");
      await expect(approver.getByTestId("t08-action-ok")).toBeVisible();
      await expect(approver.getByTestId("t08-workspace-status")).toHaveText(
        "approved",
        { timeout: 30_000 },
      );

      // ---- Admin: schedule the rollback release and wait for activation ----
      await admin.reload({ waitUntil: "networkidle" });
      await expect(admin.getByTestId("t08-workspace-status")).toHaveText(
        "approved",
      );
      await expect(admin.getByTestId("t08-binding-time")).not.toHaveText("—");
      await admin.getByTestId("t08-schedule-button").focus();
      await admin.keyboard.press("Enter");
      await expect(admin.getByTestId("t08-workspace-status")).toHaveText(
        "scheduled",
        { timeout: 30_000 },
      );
      await waitForWorkspaceActive(admin);

      // ---- Release facts before the explicit recovery ----
      const beforeFacts = await fetchReleaseFacts(admin.request, server.baseURL);
      const beforeHistoryResponse = await reviewer.request.get(
        `${server.baseURL}/controlled/s01/api/queries/applications/${encodeURIComponent(
          accepted.application_id,
        )}/history`,
      );
      expect(beforeHistoryResponse.ok()).toBeTruthy();
      const beforeHistory = await beforeHistoryResponse.json();
      const beforeActivated = beforeFacts.workspaceEvents.filter(
        (event) => event.kind === "activated",
      );
      expect(beforeActivated.map((event) => event.active_generation)).toEqual([
        1, 2, 3,
      ]);

      // ---- Independent Policy Approver: explicit hold recovery ----
      await approver.goto(`${server.baseURL}${S09_URL}`, {
        waitUntil: "networkidle",
      });
      await expect(approver.getByTestId("t09-role")).toHaveText("approver");
      await expect(approver.getByTestId("t09-recover-form")).toBeVisible();
      expect(await approver.getByLabel("恢复代次").inputValue()).toBe("3");
      await approver.getByTestId("t09-recover-button").focus();
      await approver.keyboard.press("Enter");
      await expect(approver.getByTestId("t09-action-ok")).toBeVisible();
      await expect(approver.getByTestId("t09-holds-empty")).toBeVisible({
        timeout: 30_000,
      });

      // ---- Release facts after the explicit recovery: immutable prior and
      // failed facts, one new recovery fact with its own identity ----
      await approver.reload({ waitUntil: "networkidle" });
      const afterFacts = await fetchReleaseFacts(admin.request, server.baseURL);
      expect(
        JSON.stringify(
          afterFacts.workspaceEvents.slice(0, beforeFacts.workspaceEvents.length),
        ),
      ).toBe(JSON.stringify(beforeFacts.workspaceEvents));
      expect(
        JSON.stringify(
          afterFacts.auditEvents.slice(0, beforeFacts.auditEvents.length),
        ),
      ).toBe(JSON.stringify(beforeFacts.auditEvents));
      // Recovery appends exactly one new Security Audit fact with the
      // release reference; nothing prior is rewritten (P-3).
      const recoveryAudits = afterFacts.auditEvents.filter(
        (record) => record.action === "s08_recover_hold",
      );
      expect(recoveryAudits).toHaveLength(1);
      expect(recoveryAudits[0].hold_id).toBeTruthy();
      expect(recoveryAudits[0].recovery_generation).toBe(3);
      expect(afterFacts.auditEvents.length).toBe(
        beforeFacts.auditEvents.length + 1,
      );
      const recoveryFacts = afterFacts.workspaceEvents.filter(
        (event) => event.kind === "hold_released",
      );
      expect(recoveryFacts).toHaveLength(1);
      const recoveryFact = recoveryFacts[0];
      expect(recoveryFact.event_id).toBeTruthy();
      expect(recoveryFact.recovery_generation).toBe(3);
      expect(recoveryFact.revision).toBeGreaterThan(
        beforeFacts.workspaceEvents.length,
      );
      // The recovery fact is the only new event: append-only, nothing
      // rewritten.
      expect(afterFacts.workspaceEvents.length).toBe(
        beforeFacts.workspaceEvents.length + 1,
      );

      // ---- Reviewer: the old run stays non-current and the recovery
      // generation run becomes current with the release pinned ----
      const afterHistory = await waitForCurrentGeneration(
        reviewer,
        server.baseURL,
        accepted.application_id,
        3,
      );
      // The immutable run facts are preserved exactly; only the derived
      // currentness fields evolve as the recheck lands.
      expect(
        JSON.stringify(
          stableRunFacts(afterHistory.runs.slice(0, beforeHistory.runs.length)),
        ),
      ).toBe(JSON.stringify(stableRunFacts(beforeHistory.runs)));
      const currentRuns = afterHistory.runs.filter(
        (run) => run.current === true,
      );
      expect(currentRuns).toHaveLength(1);
      expect(currentRuns[0].active_generation).toBe(3);
      // The recovery run pins the rollback candidate -- the compatible
      // known-good release re-activated as a NEW server fact with its own
      // identity -- never the original bootstrap candidate id.
      expect(currentRuns[0].candidate_id).toBe(rollbackCandidateId);
      expect(currentRuns[0].status).toBe("complete");

      // The Reviewer UI opens the successor work from the server queue and
      // renders the server-owned history: old run non-current, recovery run
      // current, both release pins visible.
      const manualItem = await waitForManualItem(
        reviewer,
        server.baseURL,
        accepted.application_id,
      );
      await reviewer.reload({ waitUntil: "networkidle" });
      const queueLink = reviewer.getByRole("link", {
        name: new RegExp(manualItem.work_item_id),
      });
      await expect(queueLink).toBeVisible();
      await queueLink.click();
      await expect(
        reviewer.getByTestId("review-history-run").first(),
      ).toBeVisible();
      const historyTexts = await reviewer
        .getByTestId("review-history-run")
        .allInnerTexts();
      const historyJoined = historyTexts.join("\n");
      expect(historyJoined).toContain(changedCandidateId);
      expect(historyJoined).toContain(rollbackCandidateId);
      expect(historyJoined).toContain("3");

      // ---- Auditor: the reconciliation shows the affected member and its
      // reevaluation receipts ----
      resources.auditorContext = await browser.newContext({
        viewport,
        extraHTTPHeaders: { Authorization: `Bearer ${AUDITOR_CREDENTIAL}` },
      });
      const auditor = await resources.auditorContext.newPage();
      const auditorDiag = trackPageDiagnostics(auditor);
      await auditor.goto(`${server.baseURL}${S09_URL}`, {
        waitUntil: "networkidle",
      });
      await expect(auditor.getByTestId("t09-role")).toHaveText("auditor");
      await expect(auditor.getByTestId("t09-recon")).toBeVisible({
        timeout: 30_000,
      });
      await expect(auditor.getByTestId("t09-recon-members")).toContainText(
        accepted.application_id,
        { timeout: 60_000 },
      );
      await expect(auditor.getByTestId("t09-recon-members")).toContainText(
        "applied",
      );

      // ---- Command discipline: exact one-shot sequences, unique keys ----
      const operatorActions = operatorPosts.map((post) =>
        post.url.split("/").pop(),
      );
      expect(operatorActions).toEqual(["impose_hold", "propose_rollback"]);
      const operatorKeys = operatorPosts.map(
        (post) => post.body.idempotency_key,
      );
      expect(new Set(operatorKeys).size).toBe(2);
      expect(operatorPosts[0].body.hold_scope).toBe("open_cycle");
      expect(operatorPosts[0].body.reason_code).toBe("S09_TEST_HOLD");
      expect(operatorPosts[1].body.release_candidate_id).toBe(
        bootstrapCandidateId,
      );
      expect(operatorPosts[1].body.reason_code).toBe("S09_TEST_ROLLBACK");
      const approverActions = approverPosts.map((post) =>
        post.url.split("/").pop(),
      );
      // The S09 tracker sees the two impact previews (the approvals
      // themselves live on the retained /s08/ router) and the recovery.
      expect(approverActions).toEqual([
        "preview_impact",
        "preview_impact",
        "recover_hold",
      ]);
      expect(approverPosts[2].body.recovery_generation).toBe(3);
      const adminActions = adminPosts.map((post) =>
        post.url.split("/").pop(),
      );
      expect(adminActions).toEqual([
        "import_legacy",
        "revise_draft",
        "freeze_candidate",
        "request_validation",
        "submit_review",
        "schedule",
        "submit_review",
        "schedule",
      ]);
      const allKeys = [
        ...operatorKeys,
        ...approverPosts.map((post) => post.body.idempotency_key),
        ...adminPosts.map((post) => post.body.idempotency_key),
      ];
      expect(new Set(allKeys).size).toBe(allKeys.length);

      // ---- Diagnostics: zero page/console/network errors ----
      for (const [name, diag] of [
        ["admin", adminDiag],
        ["approver", approverDiag],
        ["operator", operatorDiag],
        ["reviewer", reviewerDiag],
        ["auditor", auditorDiag],
      ]) {
        expect(diag.browserErrors, `${name} page errors`).toEqual([]);
        expect(diag.consoleErrors, `${name} console errors`).toEqual([]);
        expect(diag.networkErrors, `${name} network failures`).toEqual([]);
      }

      // ---- No authoritative data in browser storage; no overflow ----
      for (const page of [admin, approver, operator, reviewer, auditor]) {
        expect(await page.evaluate(() => localStorage.length)).toBe(0);
        expect(await page.evaluate(() => sessionStorage.length)).toBe(0);
        expect(await assertNoOverflow(page)).toBe(true);
      }
    } catch (error) {
      failure = error;
      // Preserve the exact server state for offline diagnosis of the
      // reviewer's transient S01_INTERNAL_ERROR history response.
      if (resources.server) {
        try {
          fs.copyFileSync(
            resources.server.statePath,
            `/tmp/xiaopeng-t09-failed-${Date.now()}.sqlite3`,
          );
        } catch {
          // Best-effort state preservation.
        }
      }
      throw error;
    } finally {
      for (const name of [
        "auditorContext",
        "demoContext",
        "operatorContext",
        "approverContext",
        "adminContext",
      ]) {
        try {
          if (resources[name]) await resources[name].close();
        } catch {
          // Best-effort context close after a failure.
        }
      }
      if (resources.server) {
        try {
          await stopServer(resources.server);
        } catch (error) {
          if (failure === undefined) throw error;
        }
      }
    }
  });
}
