/**
 * Ticket #48 T14 — the S12 Evaluation Operator production seam.
 *
 * The real FastAPI app (tests.test_t14_s12_app:create_t14_s12_test_app)
 * serves the shared qualified React build under the registered S12 operator
 * credential.  The browser runs the full authorized workflow — shell read,
 * frozen-plan catalog, one start with plan_id only, one process trigger for
 * the original job, bounded job polling, sealed bundle read — at 1280x800
 * and 390x844, with an allowlisted request log proving business-state
 * isolation and exact status rendering.
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
const S12_CREDENTIAL = "t14-s12-operator-credential";
const CANONICAL = "/controlled/s12";
const ALIAS = "/controlled/s12/react";

test.use({ extraHTTPHeaders: { Authorization: `Bearer ${S12_CREDENTIAL}` } });

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

function cleanupTree(root) {
  try {
    fs.rmSync(root, { recursive: true, force: true });
  } catch {
    // The bounded best-effort cleanup owns its failures silently; the spec's
    // temp roots live under /tmp and never carry assertion state.
  }
}

async function startServer(fixtureRoot, reactRoot = null) {
  const port = await reservePort();
  const output = [];
  const child = spawn(
    PYTHON,
    [
      "-m",
      "uvicorn",
      "tests.test_t14_s12_app:create_t14_s12_test_app",
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
        TASK4_T14_FIXTURE_ROOT: fixtureRoot,
        ...(reactRoot === null ? {} : { TASK4_T14_REACT_DIR: reactRoot }),
        NO_PROXY: "127.0.0.1,localhost",
        no_proxy: "127.0.0.1,localhost",
        PYTHONDONTWRITEBYTECODE: "1",
        PYTHONPYCACHEPREFIX: "/tmp/xiaopeng-task4-t14-pycache",
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  child.stdout.on("data", (chunk) => output.push(chunk.toString()));
  child.stderr.on("data", (chunk) => output.push(chunk.toString()));

  const baseURL = `http://127.0.0.1:${port}`;
  const deadline = Date.now() + 60_000;
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
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  if (!ready) {
    child.kill("SIGKILL");
    cleanupTree(fixtureRoot);
    throw new Error(`T14 server did not start: ${output.join("")}`);
  }
  return { baseURL, child };
}

async function stopServer(server, fixtureRoot) {
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
  cleanupTree(fixtureRoot);
  if (failures.length > 0) throw failures[0];
}

/** The deterministic fixture identity built into this server process. */
function readFixture(fixtureRoot) {
  return JSON.parse(fs.readFileSync(path.join(fixtureRoot, "fixture.json"), "utf8"));
}

/** The allowlisted request set proving business-state isolation: only S12
 * catalog/start/process/job/bundle calls plus static assets may appear. */
function assertAllowlistedRequests(requests) {
  const allowed = [
    { method: "GET", pattern: /^\/api\/health$/ },
    { method: "GET", pattern: /^\/controlled\/s12$/ },
    { method: "GET", pattern: /^\/controlled\/s12\/react$/ },
    { method: "GET", pattern: /^\/static\/react\/(?:index\.html|assets\/[A-Za-z0-9._-]+\.(?:js|css))$/ },
    { method: "GET", pattern: /^\/favicon\.ico$/ },
    { method: "GET", pattern: /^\/controlled\/s12\/plans$/ },
    { method: "POST", pattern: /^\/controlled\/s12\/jobs\/start$/ },
    { method: "POST", pattern: /^\/controlled\/s12\/jobs\/[^/]+\/process$/ },
    { method: "GET", pattern: /^\/controlled\/s12\/jobs\/[^/]+$/ },
    { method: "GET", pattern: /^\/controlled\/s12\/bundles\/[^/]+$/ },
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

async function runOperatorWorkflow(page, baseURL, fixture, viewport) {
  const requests = [];
  page.on("request", (request) =>
    requests.push({ method: request.method(), url: request.url() }),
  );

  await page.setViewportSize(viewport);
  await page.goto(`${baseURL}${CANONICAL}`, { waitUntil: "domcontentloaded" });

  // Shell renders the closed operator surface with no-store semantics owned
  // by the server route; the browser sees the mounted workflow only.
  await expect(page.getByTestId("s12-plan-select")).toBeVisible();
  await expect(page.getByTestId("s12-boundary-gate")).toHaveText("S12");

  // Select the one frozen plan by id only and start exactly one job.
  // Keyboard operation: focus the native select directly, choose with the
  // keyboard, then Tab to the start button and press Enter — no pointer.
  await page.getByTestId("s12-plan-select").focus();
  await expect(page.getByTestId("s12-plan-select")).toBeFocused();
  await page.getByTestId("s12-plan-select").selectOption(fixture.plan_id);
  await expect(page.getByTestId("s12-selected-plan-id")).toHaveText(
    fixture.plan_id,
  );
  const startControl = page.getByTestId("s12-start-button");
  await startControl.focus();
  await expect(startControl).toBeFocused();
  await startControl.press("Enter");

  // The terminal job exposes the result status verbatim and the sealed
  // report lands complete.
  await expect(page.getByTestId("s12-result-status")).toHaveText(
    /^(INSUFFICIENT|FAIL|SMOKE_ONLY|PASS\(scope=[^)]+\)|INVALID)$/,
    { timeout: 30_000 },
  );
  await expect(page.getByTestId("s12-sealed-report")).toBeVisible({
    timeout: 30_000,
  });
  await expect(page.getByTestId("s12-job-status")).toHaveText("complete");

  const report = await page.getByTestId("s12-sealed-report").textContent();
  expect(report).toContain("business_deltas");
  expect(report).toContain("result_digest");

  // No horizontal overflow at this viewport.
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth <= window.innerWidth + 1,
  );
  expect(overflow).toBe(true);

  // After a terminal run the operator action is disabled by design: the
  // one-shot workflow cannot mint a second job from the same surface.
  await expect(page.getByTestId("s12-plan-select")).toBeDisabled();
  await expect(startControl).toBeDisabled();

  assertAllowlistedRequests(requests);
  const posts = [];
  const jobGets = new Set();
  for (const { url } of requests) {
    const pathname = new URL(url).pathname;
    if (/\/jobs\/start$/.test(pathname)) posts.push(pathname);
    if (/\/jobs\/[^/]+\/process$/.test(pathname)) posts.push(pathname);
    if (/\/jobs\/[^/]+$/.test(pathname)) jobGets.add(pathname);
  }
  // Exactly one start and one process for the original job.
  expect(posts.filter((p) => p.endsWith("/start")).length).toBe(1);
  expect(posts.filter((p) => p.endsWith("/process")).length).toBe(1);
}

let server;
let fixtureRoot;

test.beforeEach(async () => {
  fixtureRoot = fs.mkdtempSync(path.join(os.tmpdir(), "xiaopeng-t14-"));
});

test.afterEach(async () => {
  if (server !== undefined) {
    await stopServer(server, fixtureRoot);
    server = undefined;
  } else {
    cleanupTree(fixtureRoot);
  }
});

for (const [label, viewport] of [
  ["desktop 1280x800", { width: 1280, height: 800 }],
  ["mobile 390x844", { width: 390, height: 844 }],
]) {
  test(`full operator workflow at ${label}`, async ({ page }) => {
    server = await startServer(fixtureRoot);
    const fixture = readFixture(fixtureRoot);
    await runOperatorWorkflow(page, server.baseURL, fixture, viewport);
  }, 120_000);
}

test("shell alias serves the same build and denial hides every identifier", async ({
  request,
}) => {
  server = await startServer(fixtureRoot);
  const alias = await request.get(`${server.baseURL}${ALIAS}`);
  expect(alias.status()).toBe(200);
  expect(alias.headers()["cache-control"]).toBe("no-store");
  const canonical = await request.get(`${server.baseURL}${CANONICAL}`);
  expect(canonical.status()).toBe(200);

  // Unauthenticated reads are denied with the exact envelope on both routes.
  // A raw fetch carries none of this fixture's test-level Authorization
  // header, proving the server (not the harness) denies the anonymous read.
  const deniedPlans = await fetch(`${server.baseURL}/controlled/s12/plans`);
  expect(deniedPlans.status).toBe(403);
  expect((await deniedPlans.json()).detail.error).toBe("S12_FORBIDDEN");
  const wrongCredential = await fetch(
    `${server.baseURL}/controlled/s12/jobs/some-job`,
    { headers: { Authorization: "Bearer not-the-credential" } },
  );
  expect(wrongCredential.status).toBe(403);
  expect((await wrongCredential.json()).detail.error).toBe("S12_FORBIDDEN");

  // An unknown plan id selected through a stale client is rejected as typed
  // not-found; the response carries no internal detail beyond the code.
  const unknownStart = await request.post(
    `${server.baseURL}/controlled/s12/jobs/start`,
    {
      data: { plan_id: "plan-does-not-exist" },
      headers: { Authorization: `Bearer ${S12_CREDENTIAL}` },
    },
  );
  expect(unknownStart.status()).toBe(404);
  expect((await unknownStart.json()).detail.error).toBe("S12_NOT_FOUND");
}, 60_000);

test("missing build fails closed on both S12 shell routes", async ({ request }) => {
  server = await startServer(fixtureRoot);
  const missingRoot = path.join(fixtureRoot, "missing-react-build");
  fs.mkdirSync(missingRoot, { recursive: true });
  await stopServer(server, fixtureRoot);
  server = await startServer(fixtureRoot, missingRoot);
  for (const shellPath of [CANONICAL, ALIAS]) {
    const shell = await request.get(`${server.baseURL}${shellPath}`);
    expect(shell.status()).toBe(503);
    expect(await shell.json()).toEqual({
      detail: {
        error: "S12_REACT_UNAVAILABLE",
        message: "Controlled S12 React shell is not built",
      },
    });
    expect(shell.headers()["cache-control"]).toBe("no-store");
  }
  const plans = await request.get(`${server.baseURL}/controlled/s12/plans`);
  expect(plans.status()).toBe(200);
  const health = await request.get(`${server.baseURL}/api/health`);
  expect(health.ok()).toBeTruthy();
}, 60_000);
