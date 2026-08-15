const { spawn } = require("node:child_process");
const { once } = require("node:events");
const fs = require("node:fs");
const net = require("node:net");
const path = require("node:path");

const { expect, test } = require("@playwright/test");

const ROOT = path.resolve(__dirname, "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");
const DEMO_URL = "/demo/react";
const FIXTURE_ID = "app_demo_step2_bad_vin";
const SAMPLE_ID = "JFL25P02L086208-01";
const FIXTURES_ROUTE = "**/api/demo/fixtures";
const CHECK_ROUTE = "**/api/demo/check";

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

/** Starts the real FastAPI/uvicorn app serving the production Vite build.
 * TASK4_WEB_TOKEN is removed from the environment: the demo and legacy APIs
 * run in their open demo mode exactly like the pytest clients.  The S01
 * state path is a per-server temp sqlite the spec owns and cleans up. */
async function startServer() {
  const port = await reservePort();
  const statePath = path.join(
    "/tmp",
    `xiaopeng-task4-t06-react-${process.pid}-${port}-${Date.now()}.sqlite3`,
  );
  const env = { ...process.env };
  delete env.TASK4_WEB_TOKEN;
  const output = [];
  const child = spawn(
    PYTHON,
    [
      "-m",
      "uvicorn",
      "task4_consistency.web.app:app",
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
        PYTHONPYCACHEPREFIX: "/tmp/xiaopeng-task4-t06-react-pycache",
        TASK4_S01_STATE_PATH: statePath,
        TASK4_S01_BACKGROUND_ENABLED: "0",
        TASK4_S01_DEMO_CREDENTIAL: "s01-registered-demo-test-credential",
        TASK4_S01_DEMO_SUBJECT: "t06-browser-reviewer",
        TASK4_S01_OPERATOR_CREDENTIAL: "s01-registered-operator-test-credential",
        TASK4_S01_OPERATOR_SUBJECT: "t06-browser-operator",
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
    return { baseURL, child, statePath };
  }
  const exited = once(child, "exit");
  if (child.exitCode === null) {
    child.kill("SIGKILL");
    await exited;
  }
  cleanupStatePath(statePath);
  throw new Error(`T06 React server did not start: ${output.join("")}`);
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
 * excluded) for the final diagnostics assertion.  Deliberate route-fulfilled
 * error responses are counted per expected status so the state phases can
 * prove their exact incidence without hiding any unexpected error. */
function trackPageDiagnostics(page, expectedStatuses = []) {
  const browserErrors = [];
  const consoleErrors = [];
  const networkErrors = [];
  const counts = {};
  for (const status of expectedStatuses) counts[status] = 0;
  page.on("pageerror", (error) => browserErrors.push(error.message));
  page.on("console", (message) => {
    if (message.type() !== "error") return;
    if (message.location().url.endsWith("/favicon.ico")) return;
    const match = message
      .text()
      .match(/Failed to load resource: the server responded with a status of (\d+)/);
    if (match !== null && counts[match[1]] !== undefined) {
      counts[match[1]] += 1;
      return;
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

/** Every named control exists exactly once, is visible, fits the viewport,
 * does not clip its own content, is center-hittable, and no two controls
 * overlap.  Panels and the long report region are exempt from the vertical
 * viewport fit only (their content is intentionally taller and scrolls). */
async function assertControlsFitAndDoNotOverlap(page, testIds) {
  const isScrollableRegion = (testId) =>
    testId.endsWith("-panel") || testId.endsWith("-report");
  const { innerWidth, innerHeight } = await page.evaluate(() => ({
    innerWidth: window.innerWidth,
    innerHeight: window.innerHeight,
  }));
  const boxes = [];
  const centerHits = [];
  for (const testId of testIds) {
    const locator = page.getByTestId(testId);
    expect(await locator.count(), `${testId} count`).toBe(1);
    await locator.scrollIntoViewIfNeeded();
    expect(await locator.isVisible(), `${testId} visible`).toBe(true);
    const box = await locator.boundingBox();
    expect(box, `${testId} bounding box`).not.toBeNull();
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
    if (!isScrollableRegion(testId)) {
      expect(
        clip.scrollHeight,
        `${testId} vertical clipping`,
      ).toBeLessThanOrEqual(clip.clientHeight + 1);
    }
    const scroll = await page.evaluate(() => window.scrollY);
    centerHits.push(
      await page.evaluate(({ testId, box, innerHeight }) => {
        const element = document.querySelector(`[data-testid="${testId}"]`);
        if (!element) return null;
        if (getComputedStyle(element).pointerEvents === "none") {
          return (
            element.hasAttribute("disabled") ||
            element.getAttribute("aria-disabled") === "true"
          );
        }
        // Tall scrollable regions have their center beyond the viewport;
        // clamp the probe point to the visible part of the element.
        const y = Math.min(
          box.y + box.height / 2,
          innerHeight - 1,
        );
        const hit = document.elementFromPoint(box.x + box.width / 2, y);
        return element.contains(hit);
      }, { testId, box, innerHeight }),
    );
    boxes.push({ testId, box, docTop: box.y + scroll });
  }
  for (const { testId, box } of boxes) {
    expect(box.x, `${testId} left edge`).toBeGreaterThanOrEqual(0);
    expect(box.y, `${testId} top edge`).toBeGreaterThanOrEqual(0);
    expect(
      box.x + box.width,
      `${testId} right edge`,
    ).toBeLessThanOrEqual(innerWidth + 1);
    if (!isScrollableRegion(testId)) {
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

/** Bounded state evidence at the current viewport: loading (held then
 * released), empty, list failure, and check failure.  Every route is
 * released/unrouted before returning so no promise or server stays open. */
async function runStatePhases(page, baseURL, label) {
  // 1. Loading: hold the fixtures response, assert the named live status and
  // layout, then always release it.
  let releaseFixtures;
  const held = new Promise((resolve) => {
    releaseFixtures = resolve;
  });
  await page.route(FIXTURES_ROUTE, async (route) => {
    await held;
    await route.continue();
  });
  await page.goto(`${baseURL}${DEMO_URL}`, { waitUntil: "domcontentloaded" });
  const loading = page.getByTestId("demo-fixtures-loading");
  await expect(loading).toBeVisible();
  expect(await loading.getAttribute("role")).toBe("status");
  expect(await assertNoOverflow(page), `${label} loading overflow`).toBe(true);
  await assertControlsFitAndDoNotOverlap(page, [
    "demo-panel",
    "demo-fixtures-loading",
  ]);
  releaseFixtures();
  await expect(page.getByTestId("demo-fixture-select")).toBeVisible();
  await page.unroute(FIXTURES_ROUTE);

  // 2. Empty: a typed empty option list is readable, contained, and
  // non-overlapping.
  await page.route(FIXTURES_ROUTE, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ fixtures: [] }),
    }),
  );
  await page.reload({ waitUntil: "networkidle" });
  const empty = page.getByTestId("demo-fixtures-empty");
  await expect(empty).toBeVisible();
  expect(await assertNoOverflow(page), `${label} empty overflow`).toBe(true);
  await assertControlsFitAndDoNotOverlap(page, ["demo-panel", "demo-fixtures-empty"]);
  await page.unroute(FIXTURES_ROUTE);

  // 3. List failure: the closed generic envelope renders the fixed alert with
  // no reflected identifier or code.
  await page.route(FIXTURES_ROUTE, (route) =>
    route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({
        detail: { error: "DEMO_FIXTURE_UNAVAILABLE", message: "演示样例暂不可用" },
      }),
    }),
  );
  await page.reload({ waitUntil: "networkidle" });
  const listError = page.getByTestId("demo-fixtures-error");
  await expect(listError).toBeVisible();
  expect(await listError.getAttribute("role")).toBe("alert");
  await expect(listError).toHaveText("演示样例列表不可用");
  const listErrorText = await listError.innerText();
  expect(listErrorText).not.toContain("app_demo_step2");
  expect(listErrorText).not.toContain("DEMO_FIXTURE_UNAVAILABLE");
  expect(await assertNoOverflow(page), `${label} list-error overflow`).toBe(true);
  await assertControlsFitAndDoNotOverlap(page, [
    "demo-panel",
    "demo-fixtures-error",
  ]);
  await page.unroute(FIXTURES_ROUTE);

  // 4. Check failure: the real fixtures load, the routed 500 injects an
  // internal path, and the fixed generic alert must not reflect it.
  await page.reload({ waitUntil: "networkidle" });
  await page.route(CHECK_ROUTE, (route) =>
    route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({
        detail: {
          error: "DEMO_CHECK_FAILED",
          message: "internal /srv/secret/rules.yaml exploded",
        },
      }),
    }),
  );
  await page.selectOption("#demo-fixture-select", FIXTURE_ID);
  await page.getByTestId("demo-run-button").click();
  const checkError = page.getByTestId("demo-check-error");
  await expect(checkError).toBeVisible();
  expect(await checkError.getAttribute("role")).toBe("alert");
  await expect(checkError).toHaveText("校验失败，请稍后重试");
  const checkErrorText = await checkError.innerText();
  expect(checkErrorText).not.toContain("/srv/secret");
  expect(checkErrorText).not.toContain("rules.yaml");
  expect(checkErrorText).not.toContain("DEMO_CHECK_FAILED");
  expect(await assertNoOverflow(page), `${label} check-error overflow`).toBe(true);
  await assertControlsFitAndDoNotOverlap(page, [
    "demo-panel",
    "demo-fixture-select",
    "demo-run-button",
    "demo-check-status",
    "demo-check-error",
  ]);
  await page.unroute(CHECK_ROUTE);

  // Back to a fresh pre-run shell for the real success flow.
  await page.goto(`${baseURL}${DEMO_URL}`, { waitUntil: "networkidle" });
}

/** One full real selection/check/report/evidence pass at the given viewport.
 * posts is cleared before the pass so the caller can assert exactly one. */
async function runDemoFlow(page, baseURL, label, posts) {
  const shellResponse = await page.goto(`${baseURL}${DEMO_URL}`, {
    waitUntil: "networkidle",
  });
  expect(shellResponse.status(), `${label} shell status`).toBe(200);
  expect(shellResponse.headers()["cache-control"]).toContain("no-store");
  await expect(page.getByTestId("demo-panel")).toBeVisible();
  await expect(page.getByTestId("demo-boundary-track")).toHaveText("C-DEMO");
  await expect(page.getByTestId("demo-boundary-scope")).toHaveText(
    "synthetic",
  );
  expect(await assertNoOverflow(page), `${label} initial overflow`).toBe(true);

  // The demo surface owns no upload/JSON/rule/KB controls.
  expect(await page.locator('input[type="file"]').count()).toBe(0);
  expect(await page.locator("textarea").count()).toBe(0);
  expect(await page.locator('iframe[srcdoc]').count()).toBe(0);

  // Keyboard-operable: Tab focuses the select, ArrowDown picks the second
  // option (fixed allow-list order: ok, bad_vin, fmt), Tab reaches Run,
  // Enter activates exactly one POST.
  await page.keyboard.press("Tab");
  await expect(page.locator(":focus")).toHaveAttribute(
    "id",
    "demo-fixture-select",
  );
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Tab");
  await expect(page.locator(":focus")).toHaveAttribute(
    "data-testid",
    "demo-run-button",
  );
  expect(
    await page.evaluate(() => {
      const element = document.activeElement;
      if (!(element instanceof HTMLElement)) return false;
      const style = window.getComputedStyle(element);
      return style.outlineStyle !== "none" || style.boxShadow !== "none";
    }),
    `${label} visible focus`,
  ).toBe(true);
  await page.keyboard.press("Enter");

  await expect(page.getByTestId("demo-report")).toBeVisible();
  await expect(page.getByTestId("demo-check-status")).toHaveText("校验完成");
  await expect(page.getByTestId("demo-summary")).toContainText("不一致 1");
  await expect(
    page.getByTestId("demo-check-item-R_VIN_CROSS"),
  ).toContainText("R_VIN_CROSS");
  await expect(
    page.getByTestId("demo-check-item-R_VIN_CROSS"),
  ).toContainText("不一致");
  await expect(
    page.getByTestId("demo-snapshot-R_VIN_CROSS").first(),
  ).toBeVisible();
  await expect(page.getByTestId("demo-config-version")).toContainText(
    "规则包版本：",
  );
  expect(await assertNoOverflow(page), `${label} report overflow`).toBe(true);

  // While the complete success report is still mounted, the controls, status,
  // report, finding, and evidence-link region must fit and not overlap.
  await assertControlsFitAndDoNotOverlap(page, [
    "demo-panel",
    "demo-fixture-select",
    "demo-run-button",
    "demo-check-status",
    "demo-report",
    "demo-check-item-R_VIN_CROSS",
    "demo-evidence-link",
  ]);

  // The evidence link is server-projected and navigable to the matching
  // Step2 sample metadata.
  const link = page.getByTestId("demo-evidence-link");
  await expect(link).toBeVisible();
  expect(await link.getAttribute("href")).toBe(`/api/step2/${SAMPLE_ID}`);
  await link.click();
  await page.waitForLoadState("networkidle");
  expect(new URL(page.url()).pathname).toBe(`/api/step2/${SAMPLE_ID}`);
  const evidenceResponse = await page.request.get(page.url());
  expect(evidenceResponse.ok()).toBeTruthy();
  const evidence = await evidenceResponse.json();
  expect(evidence.sample_id).toBe(SAMPLE_ID);
  expect(Array.isArray(evidence.pages)).toBe(true);
  expect(evidence.pages.length).toBeGreaterThan(0);
  await expect(page.locator("body")).toContainText(SAMPLE_ID);

  // Return to the demo shell and verify the fresh pre-run shell layout too.
  await page.goto(`${baseURL}${DEMO_URL}`, { waitUntil: "networkidle" });
  await assertControlsFitAndDoNotOverlap(page, [
    "demo-panel",
    "demo-fixture-select",
    "demo-run-button",
    "demo-check-status",
  ]);
  expect(await assertNoOverflow(page)).toBe(true);
}

async function runLegacyRollback(page, baseURL) {
  const legacyResponse = await page.goto(`${baseURL}/`, {
    waitUntil: "networkidle",
  });
  expect(legacyResponse.status()).toBe(200);
  await page.locator('.scenario[data-id="vin"]').click();
  await expect(page.locator("#kpis")).toContainText("已加载");
  await page.locator("#btn-run-check").click();
  await expect(page.locator("#check-msg")).toContainText("完成");
  await expect(page.locator("#result-panel")).toBeVisible();
}

const VIEWPORTS = [
  { width: 1280, height: 800, label: "desktop 1280x800" },
  { width: 390, height: 844, label: "mobile 390x844" },
];

for (const viewport of VIEWPORTS) {
  test(`T06 production tracer (${viewport.label}): state layout, one POST, structured report, evidence navigation, legacy rollback`, async ({
    browser,
  }) => {
    test.setTimeout(120_000);
    const resources = {};
    let failure;
    try {
      resources.server = await startServer();
      const server = resources.server;
      resources.context = await browser.newContext({ viewport });
      const page = await resources.context.newPage();
      const diagnostics = trackPageDiagnostics(page, ["500", "503"]);
      const posts = [];
      page.on("request", (request) => {
        if (request.method() !== "POST") return;
        if (new URL(request.url()).pathname !== "/api/demo/check") return;
        posts.push(request.postDataJSON());
      });

      await runStatePhases(page, server.baseURL, viewport.label);
      posts.length = 0;
      await runDemoFlow(page, server.baseURL, viewport.label, posts);
      expect(posts, `${viewport.label} demo POST count`).toHaveLength(1);
      expect(posts[0]).toEqual({ fixture_id: FIXTURE_ID });

      await runLegacyRollback(page, server.baseURL);

      expect(diagnostics.browserErrors).toEqual([]);
      expect(diagnostics.consoleErrors).toEqual([]);
      expect(diagnostics.networkErrors).toEqual([]);
      // the state phases deliberately exercised one 500 and one 503 list
      // (the transient 503 is retried twice by the shared policy)
      expect(diagnostics.counts["503"]).toBe(3);
      expect(diagnostics.counts["500"]).toBe(1);
    } catch (error) {
      failure = error;
      throw error;
    } finally {
      try {
        if (resources.context) await resources.context.close();
        if (resources.server) await stopServer(resources.server);
      } catch (cleanupError) {
        if (failure === undefined) throw cleanupError;
      }
    }
  });
}
