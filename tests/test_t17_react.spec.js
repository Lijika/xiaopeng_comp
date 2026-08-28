/**
 * Ticket #32 / T17 — the S16 governed-deletion production seam.
 *
 * The real FastAPI app (tests.test_t17_react_app:create_t17_react_test_app)
 * serves the shared qualified React build under the registered governance
 * identity.  The browser runs the full authorized workflow — preflight with
 * the nine-class manifest, two distinct approvers, explicit commit, worker
 * fault -> repair -> completion, value-free receipt, hard refresh and
 * post-delete existence hiding — at 1280x800 and 390x844, with an
 * allowlisted request log proving that no S01/S02/S12/S13/S15 read can fire
 * from the S16 shell and that no restricted value is rendered.
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
const CANONICAL = "/controlled/s16";
const ALIAS = "/controlled/s16/react";

function reservePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      const port =
        typeof address === "object" && address !== null ? address.port : 0;
      server.close((error) => (error ? reject(error) : resolve(port)));
    });
  });
}

function cleanupTree(root) {
  try {
    fs.rmSync(root, { recursive: true, force: true });
  } catch {
    // Best-effort cleanup only; assertion state lives in fixture.json.
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
      "tests.test_t17_react_app:create_t17_react_test_app",
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
        TASK4_T17_FIXTURE_ROOT: fixtureRoot,
        NO_PROXY: "127.0.0.1,localhost",
        no_proxy: "127.0.0.1,localhost",
        PYTHONDONTWRITEBYTECODE: "1",
        PYTHONPYCACHEPREFIX: "/tmp/xiaopeng-task4-t17-pycache",
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
  throw new Error(`T17 server did not start: ${output.join("")}`);
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

/** The allowlisted request set proving plane isolation: only S16 routes
 * and static assets may appear; no S01/S02/S12/S13/S15 call can fire. */
function assertAllowlistedRequests(requests) {
  const allowed = [
    { method: "GET", pattern: /^\/api\/health$/ },
    { method: "GET", pattern: /^\/controlled\/s16$/ },
    { method: "GET", pattern: /^\/controlled\/s16\/react$/ },
    { method: "GET", pattern: /^\/static\/react\/(?:index\.html|assets\/[A-Za-z0-9._-]+\.(?:js|css))$/ },
    { method: "GET", pattern: /^\/favicon\.ico$/ },
    { method: "POST", pattern: /^\/controlled\/s16\/api\/deletions\/preflight$/ },
    { method: "POST", pattern: /^\/controlled\/s16\/api\/deletions\/[^/]+\/approve$/ },
    { method: "POST", pattern: /^\/controlled\/s16\/api\/deletions\/[^/]+\/commit$/ },
    { method: "POST", pattern: /^\/controlled\/s16\/api\/deletions\/[^/]+\/cancel$/ },
    { method: "POST", pattern: /^\/controlled\/s16\/api\/deletions\/[^/]+\/repair$/ },
    { method: "GET", pattern: /^\/controlled\/s16\/api\/deletions\/[^/]+$/ },
    { method: "GET", pattern: /^\/controlled\/s16\/api\/deletions\/[^/]+\/receipt$/ },
    { method: "POST", pattern: /^\/controlled\/s16\/api\/process$/ },
  ];
  const violations = requests.filter(
    ({ method, url }) =>
      !allowed.some(
        (entry) =>
          entry.method === method && entry.pattern.test(new URL(url).pathname),
      ),
  );
  expect(violations).toEqual([]);
}

async function runGovernedWorkflow(page, baseURL, fixture, viewport) {
  const requests = [];
  page.on("request", (request) =>
    requests.push({ method: request.method(), url: request.url() }),
  );

  await page.setViewportSize(viewport);
  await page.goto(`${baseURL}${CANONICAL}`, { waitUntil: "domcontentloaded" });

  await expect(page.getByTestId("s16-boundary-gate")).toHaveText("S16");

  // Preflight: keyboard-operated reference input + button.
  const referenceInput = page.getByTestId("s16-reference");
  await referenceInput.focus();
  await expect(referenceInput).toBeFocused();
  await referenceInput.fill(fixture.reference);
  const preflightButton = page.getByTestId("s16-preflight-button");
  await preflightButton.focus();
  await preflightButton.press("Enter");
  await expect(page.getByTestId("s16-manifest")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByTestId("s16-early-deletion")).toHaveText(/是/);

  // The nine copy classes all appear (derived_object may repeat per object).
  const classNames = await page
    .getByTestId("s16-entry-class")
    .allTextContents();
  const classes = new Set(classNames.map((name) => name.trim()));
  expect(classes).toEqual(
    new Set([
      "source_object",
      "derived_object",
      "evidence",
      "run_or_finding",
      "projection_or_cache",
      "export_or_temp",
      "evaluation_copy",
      "replica",
      "backup_manifest",
    ]),
  );
  await expect(page.getByTestId("s16-manifest-digest")).toHaveText(/^[0-9a-f]{64}$/);

  // Two distinct approvers approve with their own credentials.
  await page.getByTestId("s16-approver-token").fill(fixture.approver1_credential);
  await page.getByTestId("s16-approve-button").click();
  await expect(page.getByTestId("s16-approved-count")).toHaveText(/1 \/ 2/);
  await page.getByTestId("s16-approver-token").fill(fixture.approver2_credential);
  await page.getByTestId("s16-approve-button").click();
  await expect(page.getByTestId("s16-approved-count")).toHaveText(/2 \/ 2/);

  // Explicit commit confirmation, then commit.
  await page.getByTestId("s16-commit-confirm").check();
  await page.getByTestId("s16-commit-button").click();
  await expect(page.getByTestId("s16-job")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByTestId("s16-job-status")).toHaveText(/pending|running/, {
    timeout: 15_000,
  });

  // Worker fault: the fixture arms the S02 fault; bounded attempts exhaust
  // into repair_required.
  fs.writeFileSync(fixture.fault_flag, "armed", "utf8");
  for (let attempt = 0; attempt < 8; attempt += 1) {
    await page.getByTestId("s16-process-button").click();
    const status = await page.getByTestId("s16-job-status").textContent();
    if (status === "repair_required") break;
    await page.waitForTimeout(150);
  }
  await expect(page.getByTestId("s16-job-status")).toHaveText("repair_required");
  await expect(page.getByTestId("s16-stable-failure")).toContainText("s02");

  // The operator repairs the owner (writes the repair evidence file), then
  // submits the repair fact; the same job resumes.
  fs.writeFileSync(fixture.repaired_flag, "ok", "utf8");
  await page.getByTestId("s16-repair-fact").fill("s02-repair-verified");
  await page.getByTestId("s16-repair-button").click();
  await expect(page.getByTestId("s16-job-status")).toHaveText(/pending|running/, {
    timeout: 15_000,
  });
  await page.getByTestId("s16-process-button").click();
  await expect(page.getByTestId("s16-job-status")).toHaveText("complete", {
    timeout: 15_000,
  });

  // Value-free receipt with owner counts; no restricted value rendered.
  await expect(page.getByTestId("s16-receipt")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByTestId("s16-receipt-result")).toHaveText("deleted");
  const pageText = await page.locator("body").textContent();
  expect(pageText).not.toContain(fixture.application_id);
  expect(pageText).not.toContain("tenant-test");
  expect(pageText).not.toContain("result-object");
  expect(pageText).not.toContain("page-object");
  expect(pageText).not.toContain("target.sqlite3");

  // No horizontal overflow at this viewport.
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth <= window.innerWidth + 1,
  );
  expect(overflow).toBe(true);

  assertAllowlistedRequests(requests);

  // Hard refresh: the panel remounts empty and the post-delete authority
  // existence-hides the same reference.
  await page.goto(`${baseURL}${ALIAS}`, { waitUntil: "domcontentloaded" });
  await expect(page.getByTestId("s16-governed-deletion")).toBeVisible();
  const response = await page.request.post(
    `${baseURL}/controlled/s16/api/deletions/preflight`,
    {
      headers: {
        Authorization: `Bearer ${fixture.governance_credential}`,
      },
      data: {
        application_reference: fixture.reference,
        idempotency_key: "t17-browser-post-delete",
      },
    },
  );
  expect(response.status()).toBe(404);
  const hidden = await response.json();
  expect(hidden.detail.error).toBe("S16_NOT_FOUND");
}

let server;
let fixtureRoot;

test.beforeEach(async () => {
  fixtureRoot = fs.mkdtempSync(path.join(os.tmpdir(), "xiaopeng-t17-"));
});

test.afterEach(async () => {
  if (server !== undefined) {
    await stopServer(server);
    server = undefined;
  } else {
    cleanupTree(fixtureRoot);
  }
});

for (const [label, viewport] of [
  ["desktop 1280x800", { width: 1280, height: 800 }],
  ["mobile 390x844", { width: 390, height: 844 }],
]) {
  test(`T17 governed deletion ${label}`, async ({ browser }) => {
    server = await startServer(fixtureRoot);
    const fixture = readFixture(fixtureRoot);
    const context = await browser.newContext({
      viewport,
      extraHTTPHeaders: {
        Authorization: `Bearer ${fixture.governance_credential}`,
      },
    });
    const page = await context.newPage();
    await runGovernedWorkflow(page, server.baseURL, fixture, viewport);
    await context.close();
  });
}
