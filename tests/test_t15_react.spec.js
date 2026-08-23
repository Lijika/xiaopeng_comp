/**
 * Ticket #49 T15 production tracer.
 *
 * One real FastAPI authority drives the released manual, correction,
 * supplement, exception, and recovery paths into Verification Completed.
 * A distinct S13 operator then reads the shared production React build at
 * 1280x800 and 390x844.  The browser issues GETs only and renders routing,
 * obligation, and receipt facts as separate immutable regions.
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
const S13_CREDENTIAL = "t15-s13-operator-credential";
const CANONICAL = "/controlled/s13";
const ALIAS = "/controlled/s13/react";

test.use({ extraHTTPHeaders: { Authorization: `Bearer ${S13_CREDENTIAL}` } });

async function reservePort() {
  const listener = net.createServer();
  await new Promise((resolve, reject) => {
    listener.once("error", reject);
    listener.listen(0, "127.0.0.1", resolve);
  });
  const port = listener.address().port;
  await new Promise((resolve, reject) =>
    listener.close((error) => (error ? reject(error) : resolve())),
  );
  return port;
}

function cleanupTree(root) {
  try {
    fs.rmSync(root, { recursive: true, force: true });
  } catch {
    // The test owns only its mkdtemp root; assertion state lives elsewhere.
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
      "tests.test_t15_react_app:create_t15_react_test_app",
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
        TASK4_T15_FIXTURE_ROOT: fixtureRoot,
        TASK4_WEB_TOKEN: "t15-global-web-token",
        ...(reactRoot === null ? {} : { TASK4_T15_REACT_DIR: reactRoot }),
        NO_PROXY: "127.0.0.1,localhost",
        no_proxy: "127.0.0.1,localhost",
        PYTHONDONTWRITEBYTECODE: "1",
        PYTHONPYCACHEPREFIX: "/tmp/xiaopeng-task4-t15-pycache",
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
  throw new Error(`T15 server did not start: ${output.join("")}`);
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
    { method: "GET", pattern: /^\/controlled\/s13(?:\?.*)?$/ },
    {
      method: "GET",
      pattern:
        /^\/static\/react\/(?:index\.html|assets\/[A-Za-z0-9._-]+\.(?:js|css))$/,
    },
    { method: "GET", pattern: /^\/favicon\.ico$/ },
    { method: "GET", pattern: /^\/controlled\/s13\/delivery\/[^/]+$/ },
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

async function expectResponsiveProjection(page) {
  const testIds = [
    "s13-gate-section",
    "s13-routing-section",
    "s13-obligation-section",
    "s13-receipt-section",
  ];
  const result = await page.evaluate((ids) => {
    const boxes = ids.map((id) => {
      const element = document.querySelector(`[data-testid="${id}"]`);
      if (element === null) return null;
      const box = element.getBoundingClientRect();
      return {
        left: box.left,
        right: box.right,
        top: box.top,
        bottom: box.bottom,
        contentFits: element.scrollWidth <= element.clientWidth,
      };
    });
    const overlaps = boxes.some((box, index) =>
      boxes.slice(index + 1).some(
        (other) =>
          box !== null &&
          other !== null &&
          box.left < other.right &&
          box.right > other.left &&
          box.top < other.bottom &&
          box.bottom > other.top,
      ),
    );
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
      overlaps,
    };
  }, testIds);
  expect(result).toEqual({
    documentFits: true,
    boxesPresent: true,
    boxesFit: true,
    overlaps: false,
  });
}

async function readDeliverySnapshots(request, current, fixture) {
  const snapshots = {};
  for (const application of fixture.applications) {
    const response = await request.get(
      `${current.baseURL}/controlled/s13/delivery/${encodeURIComponent(application.application_id)}`,
    );
    expect(response.status()).toBe(200);
    snapshots[application.workflow] = await response.json();
  }
  return snapshots;
}

let server;
let fixtureRoot;

test.beforeEach(() => {
  fixtureRoot = fs.mkdtempSync(path.join(os.tmpdir(), "xiaopeng-t15-"));
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
  test(`five released workflows render authoritative S13 facts at ${label}`, async ({
    page,
  }) => {
    server = await startServer(fixtureRoot);
    const fixture = readFixture(fixtureRoot);
    expect(fixture.applications.map((entry) => entry.workflow)).toEqual([
      "manual",
      "correction",
      "supplement",
      "exception",
      "recovery",
    ]);
    expect(new Set(fixture.applications.map((entry) => entry.delivery_status))).toEqual(
      new Set(["pending", "received", "compensation_failed"]),
    );

    const requests = [];
    const browserErrors = [];
    page.on("request", (request) =>
      requests.push({ method: request.method(), url: request.url() }),
    );
    page.on("pageerror", (error) => browserErrors.push(error.message));
    page.on("console", (message) => {
      if (message.type() === "error") browserErrors.push(message.text());
    });
    await page.setViewportSize(viewport);

    for (const application of fixture.applications) {
      const response = await page.goto(
        `${server.baseURL}${CANONICAL}?application=${encodeURIComponent(application.application_id)}`,
        { waitUntil: "domcontentloaded" },
      );
      expect(response.status()).toBe(200);
      await expect(page.getByTestId("s13-boundary-gate")).toHaveText("S13");
      await expect(page.getByTestId("s13-verification-completed")).toHaveText(
        "completed",
      );
      await expect(page.getByTestId("s13-phase")).toHaveText(
        "Verification Completed",
      );
      await expect(page.getByTestId("s13-route")).toHaveText(application.route);
      await expect(page.getByTestId("s13-attribution-kind")).toHaveText(
        application.attribution_kind,
      );
      await expect(page.getByTestId("s13-delivery-status")).toHaveText(
        application.delivery_status,
      );
      await expect(page.getByTestId("s13-obligation-id")).toHaveText(
        application.obligation_id,
      );
      await expect(page.getByTestId("s13-operation-id")).toHaveText(
        application.operation_id,
      );
      await expect(page.getByTestId("s13-routing-history-entry")).toHaveCount(1);

      for (const name of [
        "Verification Completed",
        "Verification Routing",
        "Delivery Obligation",
        "Delivery Receipt",
      ]) {
        await expect(page.getByRole("region", { name })).toBeVisible();
      }
      await expect(page.getByRole("button")).toHaveCount(0);
      await expectResponsiveProjection(page);
      const text = await page.locator("main").innerText();
      expect(text).not.toMatch(
        /loan approval|loan rejection|credit decision|disbursement decision/i,
      );
    }

    expect(browserErrors).toEqual([]);
    expect(
      await page.evaluate(() => ({
        local: localStorage.length,
        session: sessionStorage.length,
      })),
    ).toEqual({ local: 0, session: 0 });
    assertAllowlistedRequests(requests);
    expect(requests.filter((entry) => entry.method === "POST")).toEqual([]);
    for (const application of fixture.applications) {
      const target = `/controlled/s13/delivery/${application.application_id}`;
      expect(
        requests.filter(
          (entry) =>
            entry.method === "GET" && new URL(entry.url).pathname === target,
        ),
      ).toHaveLength(1);
    }
  }, 120_000);
}

test("identity, alias, and current-to-unavailable-to-current rollback preserve facts", async ({
  request,
}) => {
  server = await startServer(fixtureRoot);
  const fixture = readFixture(fixtureRoot);
  const alias = await request.get(`${server.baseURL}${ALIAS}`);
  expect(alias.status()).toBe(200);
  expect(alias.headers()["cache-control"]).toBe("no-store");

  for (const headers of [
    {},
    { Authorization: "Bearer t15-global-web-token" },
  ]) {
    const denied = await fetch(`${server.baseURL}${CANONICAL}`, { headers });
    expect(denied.status).toBe(403);
    expect((await denied.json()).detail.error).toBe("S13_FORBIDDEN");
  }
  const unknown = await request.get(
    `${server.baseURL}/controlled/s13/delivery/app-t15-unknown`,
  );
  expect(unknown.status()).toBe(404);
  expect((await unknown.json()).detail.error).toBe("S13_NOT_FOUND");

  const before = await readDeliverySnapshots(request, server, fixture);
  await stopServer(server);
  server = undefined;

  const missingRoot = path.join(fixtureRoot, "missing-react-build");
  fs.mkdirSync(missingRoot);
  server = await startServer(fixtureRoot, missingRoot);
  for (const shellPath of [CANONICAL, ALIAS]) {
    const unavailable = await request.get(`${server.baseURL}${shellPath}`);
    expect(unavailable.status()).toBe(503);
    expect(await unavailable.json()).toEqual({
      detail: {
        error: "S13_REACT_UNAVAILABLE",
        message: "Controlled S13 delivery shell is not built",
      },
    });
    expect(unavailable.headers()["cache-control"]).toBe("no-store");
  }
  expect(await readDeliverySnapshots(request, server, fixture)).toEqual(before);
  await stopServer(server);
  server = undefined;

  server = await startServer(fixtureRoot);
  const restored = await request.get(`${server.baseURL}${CANONICAL}`);
  expect(restored.status()).toBe(200);
  expect(await readDeliverySnapshots(request, server, fixture)).toEqual(before);
}, 120_000);
