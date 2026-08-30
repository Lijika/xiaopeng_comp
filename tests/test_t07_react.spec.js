const { spawn } = require("node:child_process");
const { once } = require("node:events");
const fs = require("node:fs");
const net = require("node:net");
const path = require("node:path");

const { expect, test } = require("@playwright/test");

const ROOT = path.resolve(__dirname, "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");
const DEMO_URL = "/demo/react";
const FIXTURES_ROUTE = "**/api/demo/fixtures";
const BATCH_ROUTE = "**/api/demo/check/batch";
const SUMMARY_ROUTE = "**/api/demo/evaluate/summary";
const FIXTURE_OK = "app_demo_layout_ok";
const FIXTURE_BAD_VIN = "app_demo_layout_bad_vin";
const FIXTURE_FMT = "app_demo_layout_fmt";
const BATCH_CAP = 50;

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
    `xiaopeng-task4-t07-react-${process.pid}-${port}-${Date.now()}.sqlite3`,
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
        PYTHONPYCACHEPREFIX: "/tmp/xiaopeng-task4-t07-react-pycache",
        TASK4_S01_STATE_PATH: statePath,
        TASK4_S01_BACKGROUND_ENABLED: "0",
        TASK4_S01_DEMO_CREDENTIAL: "s01-registered-demo-test-credential",
        TASK4_S01_DEMO_SUBJECT: "t07-browser-reviewer",
        TASK4_S01_OPERATOR_CREDENTIAL: "s01-registered-operator-test-credential",
        TASK4_S01_OPERATOR_SUBJECT: "t07-browser-operator",
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
  throw new Error(`T07 React server did not start: ${output.join("")}`);
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
 * overlap.  Panels and long result regions are exempt from the vertical
 * viewport fit only (their content is intentionally taller and scrolls). */
async function assertControlsFitAndDoNotOverlap(page, testIds) {
  const isScrollableRegion = (testId) =>
    testId.endsWith("-panel") || testId.endsWith("-results");
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

/** Typed route-injection payloads for otherwise-rare states.  These never
 * change the real API: the production build still talks to real FastAPI,
 * only the routed response is swapped for the state under test. */
function partialBatchPayload() {
  return {
    track: "C-DEMO",
    data_scope: "synthetic",
    requested: 2,
    completed: 1,
    failed: 1,
    outcome: "partial",
    totals: { consistent: 4, inconsistent: 1, uncertain: 0, skipped: 0 },
    results: [
      {
        fixture_id: FIXTURE_OK,
        outcome: "completed",
        application_id: "DEMO-STEP2-JFL25P02L080310-01-OK",
        summary: {
          consistent: 4,
          inconsistent: 0,
          uncertain: 0,
          skipped: 0,
          coverage: 1,
          total: 4,
          total_including_skipped: 4,
        },
        issues: [],
        error: null,
      },
      {
        fixture_id: FIXTURE_BAD_VIN,
        outcome: "failed",
        application_id: null,
        summary: null,
        issues: [],
        error: "internal /srv/secret/rules.yaml exploded",
      },
    ],
  };
}

function emptySummaryPayload() {
  return {
    summary_state: "empty",
    suite: "main",
    claim: "C-DEV-REG",
    performance_gap: "UNVERIFIED",
    scope: "合成开发/回归语料（suite=main）",
    counts: null,
    rates: null,
    warnings: ["smoke_mode: labeled_files=0; FP/FN not computed"],
    honesty_note: "Official delivery metrics from suite=main only.",
  };
}

function availableSummaryPayload() {
  return {
    summary_state: "available",
    suite: "main",
    claim: "C-DEV-REG",
    performance_gap: "UNVERIFIED",
    scope: "合成开发/回归语料（suite=main）",
    counts: {
      n_apps_loaded: 154,
      n_check_ok: 154,
      n_check_fail: 0,
      total_pairs: 1646,
      decisive_pairs: 1624,
      true_positive: 106,
      true_negative: 1518,
      false_positive: 0,
      false_negative: 0,
      uncertain_when_labeled: 0,
      n_inconsistent_labeled_decisive: 106,
      n_expected_inconsistent: 106,
      n_missed_inconsistent: 0,
    },
    rates: {
      coverage: 0.9882,
      false_positive_rate: 0,
      false_negative_rate: 0,
      accuracy: 1,
      miss_rate: 0,
      uncertain_rate: 0,
      mean_app_coverage: 0.9882,
    },
    warnings: [],
    honesty_note: "Official delivery metrics from suite=main only.",
  };
}

/** The real two-fixture batch success pass at one viewport: shell, cap
 * label, keyboard selection, one held (pending) POST released to the real
 * server, ordered terminal items, server totals, no polling, no PASS. */
async function runBatchFlow(page, baseURL, label, posts, demoRequests) {
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

  // The batch section is mounted with native checkboxes and the server-owned
  // cap label (never a client-hard-coded second limit).
  await expect(page.getByTestId("demo-batch-panel")).toBeVisible();
  await expect(page.getByTestId("demo-batch-cap")).toContainText(
    `服务端上限：${BATCH_CAP}`,
  );
  for (const fixtureId of [FIXTURE_OK, FIXTURE_BAD_VIN, FIXTURE_FMT]) {
    await expect(
      page.getByTestId(`demo-batch-fixture-${fixtureId}`),
    ).toBeVisible();
  }

  // Keyboard-operable: focus the first fixture checkbox, Space to select,
  // Tab to the second checkbox, Space, then Tab through to the run button
  // with a visible focus style; Enter activates exactly one POST.
  await page.getByTestId(`demo-batch-fixture-${FIXTURE_OK}`).focus();
  await page.keyboard.press("Space");
  await expect(
    page.getByTestId(`demo-batch-fixture-${FIXTURE_OK}`),
  ).toBeChecked();
  await page.keyboard.press("Tab");
  await expect(page.locator(":focus")).toHaveAttribute(
    "data-testid",
    `demo-batch-fixture-${FIXTURE_BAD_VIN}`,
  );
  await page.keyboard.press("Space");
  await expect(
    page.getByTestId(`demo-batch-fixture-${FIXTURE_BAD_VIN}`),
  ).toBeChecked();
  await page.keyboard.press("Tab");
  await page.keyboard.press("Tab");
  await expect(page.locator(":focus")).toHaveAttribute(
    "data-testid",
    "demo-batch-run-button",
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

  // Hold the real POST to observe the pending live status, then release it.
  // While held, the run button and every selection control stay locked, so
  // no overlapping POST or selection change can disturb the active mutation.
  let releaseBatch;
  const held = new Promise((resolve) => {
    releaseBatch = resolve;
  });
  await page.route(BATCH_ROUTE, async (route) => {
    await held;
    await route.continue();
  });
  try {
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("demo-batch-status")).toContainText(
      "批量校验中",
    );
    await expect(page.getByTestId("demo-batch-run-button")).toBeDisabled();
    await expect(
      page.getByTestId(`demo-batch-fixture-${FIXTURE_OK}`),
    ).toBeDisabled();
    await expect(
      page.getByTestId(`demo-batch-fixture-${FIXTURE_BAD_VIN}`),
    ).toBeDisabled();
    await expect(
      page.getByTestId(`demo-batch-fixture-${FIXTURE_FMT}`),
    ).toBeDisabled();
    await expect(page.getByTestId("demo-batch-status")).toContainText(
      "批量校验中",
    );
  } finally {
    releaseBatch();
  }
  await expect(page.getByTestId("demo-batch-status")).toContainText(
    "批量校验完成",
  );
  await page.unroute(BATCH_ROUTE);

  // Exactly one POST carrying fixture ids only, in selection order.
  expect(posts, `${label} batch POST count`).toHaveLength(1);
  expect(posts[0]).toEqual({
    fixture_ids: [FIXTURE_OK, FIXTURE_BAD_VIN],
  });

  // Ordered terminal items with server totals and the enclosing outcome.
  await expect(page.getByTestId("demo-batch-outcome")).toContainText(
    "全部完成",
  );
  await expect(page.getByTestId("demo-batch-totals")).toContainText(
    "不一致 1",
  );
  const okItem = page.getByTestId(`demo-batch-item-${FIXTURE_OK}`);
  await expect(okItem).toContainText("已完成");
  await expect(okItem).toContainText("DEMO-STEP2-JFL25P02L080310-01-OK");
  const badVinItem = page.getByTestId(`demo-batch-item-${FIXTURE_BAD_VIN}`);
  await expect(badVinItem).toContainText("已完成");
  await expect(badVinItem).toContainText("R_VIN_CROSS");

  // No PASS anywhere in the shell, and no queue/job/poll abstraction.
  expect(
    await page.locator("#root").innerText(),
    `${label} no PASS claim`,
  ).not.toContain("PASS");
  await expect(page.locator("progress").count()).resolves.toBe(0);

  // No polling: after the run settles, no further /api/demo/* traffic.
  await page.waitForTimeout(2_500);
  const demoPaths = demoRequests.filter((url) =>
    url.startsWith("/api/demo/"),
  );
  expect(
    demoPaths.filter((url) => url === "/api/demo/fixtures"),
    `${label} fixtures reads`,
  ).toHaveLength(1);
  expect(
    demoPaths.filter((url) => url === "/api/demo/check/batch"),
    `${label} batch calls`,
  ).toHaveLength(1);

  expect(await assertNoOverflow(page), `${label} results overflow`).toBe(true);
  await assertControlsFitAndDoNotOverlap(page, [
    "demo-batch-panel",
    `demo-batch-fixture-${FIXTURE_OK}`,
    `demo-batch-fixture-${FIXTURE_BAD_VIN}`,
    "demo-batch-run-button",
    "demo-batch-status",
    "demo-batch-outcome",
    "demo-batch-totals",
    `demo-batch-item-${FIXTURE_OK}`,
    `demo-batch-item-${FIXTURE_BAD_VIN}`,
  ]);
}

/** The read-only fixed-main evaluation summary: explicit load control, real
 * GET, server-owned claim labels, and no PASS. */
async function runSummaryFlow(page, baseURL, label, demoRequests) {
  await page.goto(`${baseURL}${DEMO_URL}`, { waitUntil: "networkidle" });
  await expect(page.getByTestId("demo-eval-panel")).toBeVisible();
  await expect(page.getByTestId("demo-eval-status")).toContainText("未加载");

  await page.getByTestId("demo-eval-load-button").click();
  await expect(page.getByTestId("demo-eval-status")).toContainText(
    "已加载",
    { timeout: 60_000 },
  );
  await expect(page.getByTestId("demo-eval-claim")).toHaveText("C-DEV-REG");
  await expect(page.getByTestId("demo-eval-gap")).toHaveText("UNVERIFIED");
  await expect(page.getByTestId("demo-eval-scope")).not.toBeEmpty();
  await expect(page.getByTestId("demo-eval-counts")).toContainText(
    "total_pairs",
  );
  await expect(page.getByTestId("demo-eval-rates")).toContainText("coverage");
  await expect(page.getByTestId("demo-eval-note")).not.toBeEmpty();

  const summaryReads = demoRequests.filter(
    (url) => url === "/api/demo/evaluate/summary",
  );
  expect(summaryReads, `${label} summary GET count`).toHaveLength(1);
  expect(
    await page.locator("#root").innerText(),
    `${label} summary no PASS`,
  ).not.toContain("PASS");
  expect(await assertNoOverflow(page), `${label} summary overflow`).toBe(true);
  await assertControlsFitAndDoNotOverlap(page, [
    "demo-eval-panel",
    "demo-eval-load-button",
    "demo-eval-status",
    "demo-eval-claim",
    "demo-eval-gap",
    "demo-eval-counts",
    "demo-eval-rates",
  ]);
}

/** Cap rejection and invalid-body contract against the real FastAPI: the
 * server-enforced cap is 50 and rejects 51 fixture ids before any fixture
 * I/O; malformed batch bodies fail with the exact closed 422 envelope. */
async function runApiContract(page, baseURL) {
  const response = await page.request.post(`${baseURL}/api/demo/check/batch`, {
    data: { fixture_ids: Array(BATCH_CAP + 1).fill(FIXTURE_OK) },
  });
  expect(response.status()).toBe(400);
  const body = await response.json();
  expect(body.detail.error).toBe("DEMO_BATCH_TOO_LARGE");
  expect(body.detail.message).toContain(String(BATCH_CAP));

  // B4: invalid shapes use the closed typed error contract, not the generic
  // HTTPValidationError shape, and reflect nothing back.
  const invalid = await page.request.post(`${baseURL}/api/demo/check/batch`, {
    data: { fixture_ids: [] },
  });
  expect(invalid.status()).toBe(422);
  expect(await invalid.json()).toEqual({
    detail: { error: "DEMO_BATCH_INVALID", message: "批量校验请求无效" },
  });
}

/** Bounded state evidence at the current viewport via typed route injection:
 * partial batch with one failed item, cap/500 batch failures, empty and
 * unavailable summary states.  Every route is unrouted before returning. */
async function runStatePhases(page, baseURL, label) {
  // 1. Partial batch: one completed + one failed item; the failed item
  // carries injected internal text that must never reach the UI.
  await page.goto(`${baseURL}${DEMO_URL}`, { waitUntil: "networkidle" });
  await page.route(BATCH_ROUTE, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(partialBatchPayload()),
    }),
  );
  await page.getByTestId(`demo-batch-fixture-${FIXTURE_OK}`).check();
  await page.getByTestId(`demo-batch-fixture-${FIXTURE_BAD_VIN}`).check();
  await page.getByTestId("demo-batch-run-button").click();
  await expect(page.getByTestId("demo-batch-outcome")).toContainText(
    "部分完成",
  );
  const failedItem = page.getByTestId(`demo-batch-item-${FIXTURE_BAD_VIN}`);
  await expect(failedItem).toContainText("失败");
  await expect(failedItem.getByTestId("demo-batch-item-error")).toHaveText(
    "条目校验失败，请稍后重试",
  );
  expect(
    await page.locator("#root").innerText(),
    `${label} partial no internal leak`,
  ).not.toContain("/srv/secret");
  expect(
    await page.locator("#root").innerText(),
    `${label} partial totals exclude failed`,
  ).toContain("一致 4");
  await expect(page.getByTestId("demo-batch-totals")).toContainText("不一致 1");
  await page.unroute(BATCH_ROUTE);

  // 2. Cap rejection through the UI: the live status is terminally failed
  // and the registered cap code maps to fixed bound-specific copy; the
  // server-owned cap bound stays on its separate label.
  await page.reload({ waitUntil: "networkidle" });
  await page.route(BATCH_ROUTE, (route) =>
    route.fulfill({
      status: 400,
      contentType: "application/json",
      body: JSON.stringify({
        detail: {
          error: "DEMO_BATCH_TOO_LARGE",
          message: `批量校验数量超过服务端上限 ${BATCH_CAP}`,
        },
      }),
    }),
  );
  await page.getByTestId(`demo-batch-fixture-${FIXTURE_OK}`).check();
  await page.getByTestId("demo-batch-run-button").click();
  await expect(page.getByTestId("demo-batch-status")).toHaveText(
    "批量校验失败",
  );
  const batchError = page.getByTestId("demo-batch-error");
  await expect(batchError).toBeVisible();
  expect(await batchError.getAttribute("role")).toBe("alert");
  await expect(batchError).toHaveText("所选样例数量超过服务端上限，请减少选择");
  const batchErrorText = await batchError.innerText();
  expect(batchErrorText).not.toContain("DEMO_BATCH_TOO_LARGE");
  expect(batchErrorText).not.toContain(String(BATCH_CAP));
  expect(batchErrorText).not.toContain("等待");
  await expect(page.getByTestId("demo-batch-cap")).toContainText(
    `服务端上限：${BATCH_CAP}`,
  );
  await page.unroute(BATCH_ROUTE);

  // 3. Batch 500: terminal failed status + fixed generic alert, no
  // reflected code/internal detail.
  await page.route(BATCH_ROUTE, (route) =>
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
  await page.getByTestId("demo-batch-run-button").click();
  await expect(page.getByTestId("demo-batch-status")).toHaveText(
    "批量校验失败",
  );
  await expect(page.getByTestId("demo-batch-error")).toBeVisible();
  const batch500Text = await page.getByTestId("demo-batch-error").innerText();
  expect(batch500Text).not.toContain("/srv/secret");
  expect(batch500Text).not.toContain("DEMO_CHECK_FAILED");
  await page.unroute(BATCH_ROUTE);

  // 4. Summary empty: explicit empty state, nullable rates, never zero
  // success, claims still server-owned.
  await page.route(SUMMARY_ROUTE, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(emptySummaryPayload()),
    }),
  );
  await page.getByTestId("demo-eval-load-button").click();
  const evalEmpty = page.getByTestId("demo-eval-empty");
  await expect(evalEmpty).toBeVisible();
  expect(await evalEmpty.getAttribute("role")).toBe("status");
  await expect(page.getByTestId("demo-eval-claim")).toHaveText("C-DEV-REG");
  await expect(page.getByTestId("demo-eval-gap")).toHaveText("UNVERIFIED");
  expect(
    await page.locator("#root").innerText(),
    `${label} empty no zero-success`,
  ).not.toContain("coverage");
  await page.unroute(SUMMARY_ROUTE);

  // 5. Summary unavailable: distinct closed 503 renders the fixed generic
  // alert with no code reflection; a second click refetches.
  await page.route(SUMMARY_ROUTE, (route) =>
    route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({
        detail: {
          error: "DEMO_EVALUATION_UNAVAILABLE",
          message: "internal /srv/secret/evaluate.py crashed",
        },
      }),
    }),
  );
  await page.getByTestId("demo-eval-load-button").click();
  const evalError = page.getByTestId("demo-eval-error");
  await expect(evalError).toBeVisible();
  expect(await evalError.getAttribute("role")).toBe("alert");
  await expect(evalError).toHaveText("评估摘要不可用");
  const evalErrorText = await evalError.innerText();
  expect(evalErrorText).not.toContain("DEMO_EVALUATION_UNAVAILABLE");
  expect(evalErrorText).not.toContain("/srv/secret");
  await page.unroute(SUMMARY_ROUTE);

  // 6. B5: an available summary followed by a held failed reload must never
  // show the cached counts/rates as current — hidden while reloading and
  // after the failure, with only the explicit unavailable alert.
  await page.route(SUMMARY_ROUTE, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(availableSummaryPayload()),
    }),
  );
  await page.getByTestId("demo-eval-load-button").click();
  await expect(page.getByTestId("demo-eval-status")).toHaveText("已加载", {
    timeout: 60_000,
  });
  await expect(page.getByTestId("demo-eval-counts")).toBeVisible();
  await page.unroute(SUMMARY_ROUTE);

  let releaseSummary;
  const heldSummary = new Promise((resolve) => {
    releaseSummary = resolve;
  });
  await page.route(SUMMARY_ROUTE, async (route) => {
    await heldSummary;
    await route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({
        detail: {
          error: "DEMO_EVALUATION_UNAVAILABLE",
          message: "internal /srv/secret/evaluate.py crashed",
        },
      }),
    });
  });
  try {
    await page.getByTestId("demo-eval-load-button").click();
    await expect(page.getByTestId("demo-eval-status")).toContainText("加载中");
    expect(await page.getByTestId("demo-eval-counts").count()).toBe(0);
    expect(await page.getByTestId("demo-eval-rates").count()).toBe(0);
  } finally {
    releaseSummary();
  }
  await expect(page.getByTestId("demo-eval-error")).toBeVisible();
  expect(await page.getByTestId("demo-eval-counts").count()).toBe(0);
  expect(await page.getByTestId("demo-eval-rates").count()).toBe(0);
  expect(await page.getByTestId("demo-eval-status").innerText()).toContain(
    "评估摘要不可用",
  );
  await page.unroute(SUMMARY_ROUTE);

  // Back to a fresh pre-run shell for the real flows.
  await page.goto(`${baseURL}${DEMO_URL}`, { waitUntil: "networkidle" });
}

async function runCanonicalRootProbe(page, baseURL) {
  // Issue #54 cutover: the canonical root serves the qualified React demo
  // shell (the legacy demo shell is rollback-only; the deployment-only
  // rollback rehearsal over the prior wheel lives in the installed harness).
  const response = await page.goto(`${baseURL}/`, {
    waitUntil: "networkidle",
  });
  expect(response.status()).toBe(200);
  await expect(page.getByTestId("demo-panel")).toBeVisible();
  await expect(page.getByTestId("demo-boundary-track")).toHaveText("C-DEMO");
}

const VIEWPORTS = [
  { width: 1280, height: 800, label: "desktop 1280x800" },
  { width: 390, height: 844, label: "mobile 390x844" },
];

for (const viewport of VIEWPORTS) {
  test(`T07 production tracer (${viewport.label}): bounded batch check, read-only summary, cap rejection, state matrix, canonical root`, async ({
    browser,
  }) => {
    test.setTimeout(240_000);
    const resources = {};
    let failure;
    try {
      resources.server = await startServer();
      const server = resources.server;
      resources.context = await browser.newContext({ viewport });
      const page = await resources.context.newPage();
      const diagnostics = trackPageDiagnostics(page, ["400", "500", "503"]);
      const posts = [];
      const demoRequests = [];
      page.on("request", (request) => {
        const url = new URL(request.url()).pathname;
        if (url.startsWith("/api/demo/")) demoRequests.push(url);
        if (request.method() !== "POST") return;
        if (url !== "/api/demo/check/batch") return;
        posts.push(request.postDataJSON());
      });

      await runStatePhases(page, server.baseURL, viewport.label);
      posts.length = 0;
      demoRequests.length = 0;
      await runBatchFlow(page, server.baseURL, viewport.label, posts, demoRequests);
      await runApiContract(page, server.baseURL);
      await runSummaryFlow(page, server.baseURL, viewport.label, demoRequests);

      await runCanonicalRootProbe(page, server.baseURL);

      expect(diagnostics.browserErrors).toEqual([]);
      expect(diagnostics.consoleErrors).toEqual([]);
      expect(diagnostics.networkErrors).toEqual([]);
      // the state phases deliberately exercised one 400, one 500, and two
      // 503s (the batch mutation and the summary query never retry)
      expect(diagnostics.counts["400"]).toBe(1);
      expect(diagnostics.counts["500"]).toBe(1);
      expect(diagnostics.counts["503"]).toBe(2);
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
