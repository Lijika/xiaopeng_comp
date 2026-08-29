/** Ticket #53 / T19 production tracer for S17 controlled export. */
const { spawn } = require("node:child_process");
const { once } = require("node:events");
const fs = require("node:fs");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const { expect, test } = require("@playwright/test");

const ROOT = path.resolve(__dirname, "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");
const CREDENTIALS = {
  requester: "t19-requester-credential",
  approver: "t19-approver-credential",
  worker: "t19-worker-credential",
  recipient: "t19-recipient-credential",
};

function reservePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      const port = typeof address === "object" && address !== null ? address.port : 0;
      server.close((error) => (error ? reject(error) : resolve(port)));
    });
  });
}

async function startServer(fixtureRoot) {
  const port = await reservePort();
  const output = [];
  const child = spawn(PYTHON, ["-m", "uvicorn", "tests.test_t19_react_app:create_t19_react_test_app", "--factory", "--host", "127.0.0.1", "--port", String(port), "--log-level", "warning"], {
    cwd: ROOT,
    env: { ...process.env, TASK4_T19_FIXTURE_ROOT: fixtureRoot, NO_PROXY: "127.0.0.1,localhost", no_proxy: "127.0.0.1,localhost", PYTHONDONTWRITEBYTECODE: "1" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  child.stdout.on("data", (chunk) => output.push(chunk.toString()));
  child.stderr.on("data", (chunk) => output.push(chunk.toString()));
  const baseURL = `http://127.0.0.1:${port}`;
  const deadline = Date.now() + 60_000;
  while (Date.now() < deadline && child.exitCode === null) {
    try {
      if ((await fetch(`${baseURL}/api/health`)).ok) return { child, baseURL };
    } catch (_) {
      // bounded readiness retry
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  child.kill("SIGKILL");
  throw new Error(`T19 server did not start: ${output.join("")}`);
}

async function stopServer(server) {
  if (!server || server.child.exitCode !== null) return;
  const exited = once(server.child, "exit");
  server.child.kill("SIGTERM");
  if ((await Promise.race([exited, new Promise((resolve) => setTimeout(resolve, 5_000, "timeout"))])) === "timeout") {
    server.child.kill("SIGKILL");
    await exited;
  }
}

function readFixture(root) {
  return JSON.parse(fs.readFileSync(path.join(root, "fixture.json"), "utf8"));
}

function assertAllowlistedRequests(requests) {
  const allowed = [
    { method: "GET", pattern: /^\/api\/health$/ },
    { method: "GET", pattern: /^\/controlled\/s17(?:\/react)?(?:\?request=[^&]+)?$/ },
    { method: "GET", pattern: /^\/static\/react\/(?:index\.html|assets\/[A-Za-z0-9._-]+\.(?:js|css))$/ },
    { method: "GET", pattern: /^\/controlled\/s17\/api\/exports\/[^/]+$/ },
    { method: "GET", pattern: /^\/controlled\/s17\/api\/exports\/[^/]+\/receipt$/ },
    { method: "POST", pattern: /^\/controlled\/s17\/api\/exports\/preview$/ },
    { method: "POST", pattern: /^\/controlled\/s17\/api\/exports\/[^/]+\/(?:approve|commit|access|confirm|revoke|expire)$/ },
    { method: "POST", pattern: /^\/controlled\/s17\/api\/process$/ },
  ];
  const violations = requests.filter(({ method, url }) => {
    const parsed = new URL(url);
    const target = parsed.pathname + parsed.search;
    return !allowed.some((entry) => entry.method === method && entry.pattern.test(target));
  });
  expect(violations).toEqual([]);
}

async function runWorkflow(page, approverPage, apiRequest, baseURL, fixtureRoot, viewport) {
  const requests = [];
  page.on("request", (request) => requests.push({ method: request.method(), url: request.url() }));
  approverPage.on("request", (request) => requests.push({ method: request.method(), url: request.url() }));
  await page.setViewportSize(viewport);
  await approverPage.setViewportSize(viewport);
  await page.goto(`${baseURL}/controlled/s17`, { waitUntil: "domcontentloaded" });
  await expect(page.getByTestId("s17-boundary-gate")).toHaveText("S17");
  const responsePromise = page.waitForResponse((response) => response.url().endsWith("/controlled/s17/api/exports/preview") && response.request().method() === "POST");
  await page.getByTestId("s17-purpose").fill("audit_response");
  await page.getByTestId("s17-recipient").fill("s17-recipient-1");
  await page.getByTestId("s17-fields").fill("application_fingerprint");
  await page.getByTestId("s17-artifacts").fill("route_metadata");
  await page.getByTestId("s17-preview-button").click();
  const preview = await (await responsePromise).json();
  const requestId = preview.request_id;
  await expect(page.getByTestId("s17-export-state")).toBeVisible();

  await approverPage.goto(`${baseURL}/controlled/s17?request=${encodeURIComponent(requestId)}`, { waitUntil: "domcontentloaded" });
  await approverPage.getByTestId("s17-approver-token").fill(CREDENTIALS.approver);
  await approverPage.getByTestId("s17-approve-button").click();
  await expect(approverPage.getByTestId("s17-approval-status")).toHaveText("已批准");

  await page.goto(`${baseURL}/controlled/s17?request=${encodeURIComponent(requestId)}`, { waitUntil: "domcontentloaded" });
  await expect(page.getByTestId("s17-approval-status")).toHaveText("已批准");
  await page.getByTestId("s17-commit-confirm").check();
  await page.getByTestId("s17-commit-button").click();
  await page.getByTestId("s17-worker-token").fill(CREDENTIALS.worker);
  // Playwright context-level requester headers intentionally remain on the
  // operator page. The worker and recipient calls use an isolated request
  // context so each server identity is presented independently.
  const processResponse = await apiRequest.post(`${baseURL}/controlled/s17/api/process`, { headers: { Authorization: `Bearer ${CREDENTIALS.worker}` } });
  requests.push({ method: "POST", url: `${baseURL}/controlled/s17/api/process` });
  expect(processResponse.status()).toBe(200);
  expect((await processResponse.json()).status).toBe("delivered");
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(page.getByTestId("s17-request-status")).toHaveText("delivered", { timeout: 15_000 });

  const fixture = readFixture(fixtureRoot);
  await expect.poll(() => {
    try { return fs.readFileSync(fixture.token_path, "utf8"); } catch (_) { return ""; }
  }, { timeout: 10_000 }).not.toBe("");
  const token = fs.readFileSync(fixture.token_path, "utf8");
  await page.getByTestId("s17-recipient-credential").fill(CREDENTIALS.recipient);
  await page.getByTestId("s17-delivery-token").fill(token);
  const accessResponse = await apiRequest.post(`${baseURL}/controlled/s17/api/exports/${requestId}/access`, { headers: { Authorization: `Bearer ${CREDENTIALS.recipient}` }, data: { token } });
  requests.push({ method: "POST", url: `${baseURL}/controlled/s17/api/exports/${requestId}/access` });
  expect(accessResponse.status()).toBe(200);
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(page.getByTestId("s17-request-status")).toHaveText("accessed", { timeout: 15_000 });
  const confirmResponse = await apiRequest.post(`${baseURL}/controlled/s17/api/exports/${requestId}/confirm`, { headers: { Authorization: `Bearer ${CREDENTIALS.recipient}`, "Idempotency-Key": "t19-confirm" } });
  requests.push({ method: "POST", url: `${baseURL}/controlled/s17/api/exports/${requestId}/confirm` });
  expect(confirmResponse.status()).toBe(200);
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(page.getByTestId("s17-request-status")).toHaveText("confirmed", { timeout: 15_000 });
  await page.getByTestId("s17-receipt-button").click();
  await expect(page.getByTestId("s17-export-receipt")).toBeVisible();
  await expect(page.getByTestId("s17-receipt-cleanup")).toHaveText("none");
  expect(await page.evaluate(() => Object.keys(localStorage).concat(Object.keys(sessionStorage)))).toEqual([]);
  expect(await page.locator("body").textContent()).not.toContain(token);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
  assertAllowlistedRequests(requests);
}

let server;
let fixtureRoot;
test.beforeEach(() => { fixtureRoot = fs.mkdtempSync(path.join(os.tmpdir(), "xiaopeng-t19-")); });
test.afterEach(async () => { await stopServer(server); server = undefined; fs.rmSync(fixtureRoot, { recursive: true, force: true }); });

for (const [name, viewport] of [["desktop", { width: 1280, height: 800 }], ["mobile", { width: 390, height: 844 }]]) {
  test(`T19 controlled export at ${name}`, async ({ browser, request }) => {
    server = await startServer(fixtureRoot);
    const requesterContext = await browser.newContext({ viewport, extraHTTPHeaders: { Authorization: `Bearer ${CREDENTIALS.requester}` } });
    const approverContext = await browser.newContext({ viewport, extraHTTPHeaders: { Authorization: `Bearer ${CREDENTIALS.requester}` } });
    await runWorkflow(await requesterContext.newPage(), await approverContext.newPage(), request, server.baseURL, fixtureRoot, viewport);
    await approverContext.close();
    await requesterContext.close();
  });
}
