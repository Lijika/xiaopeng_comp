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
const S02_CREDENTIAL = "t03-registered-s02-credential";
const S02_SUBJECT = "t03-registered-s02-reviewer";
const S02_TENANT = "tenant-t03-react";
const S02_SOURCE = "registered-t03-react-source";
const SCENARIO = "app_s04_bad_vin.json";
const REACT_URL = "/controlled/s01/react";

/** The OCR misread raw VIN (restricted) and the true source text that only an
 * explicit authorized reveal may display (restricted).  Both are runtime
 * fixture values; neither may survive into history, URL, storage, status,
 * error, or any durable artifact. */
const MISREAD_SENTINEL = "LSVAA4182N500005Z";
const SOURCE_SENTINEL = "LSVAA4182N5000054";

/** Minimal S02 registered-source runtime so the legacy Reviewer shell serves
 * 200 on the same test app (the C-DEMO flow itself never touches S02). */
function createS02Fixture() {
  const root = fs.mkdtempSync(
    path.join("/tmp", `xiaopeng-task4-t03-react-s02-${process.pid}-`),
  );
  const objectRoot = path.join(root, "objects");
  fs.mkdirSync(objectRoot);
  fs.writeFileSync(
    path.join(objectRoot, "result.json"),
    JSON.stringify({ synthetic: true }),
  );
  fs.writeFileSync(
    path.join(root, "registry.json"),
    JSON.stringify({
      schema_version: "s02-runtime-registry/1",
      sources: [
        {
          tenant_id: S02_TENANT,
          source_system_id: S02_SOURCE,
          workload_identity_id: "t03-react-workload",
          adapter_id: "t03-react-adapter",
          adapter_version: "1",
          source_shape: "ocr-detection/unversioned",
          producer_family: "t03-react-ocr",
          enabled: true,
          allowed_media_types: ["application/json"],
          max_result_bytes: 1048576,
          max_attachment_bytes: 1048576,
          max_pages: 1,
          max_observations: 10,
        },
      ],
      objects: [
        {
          tenant_id: S02_TENANT,
          source_system_id: S02_SOURCE,
          object_ref: "t03-react-result-object",
          media_type: "application/json",
          file: "result.json",
        },
      ],
    }),
  );
  return { root, registryPath: path.join(root, "registry.json"), objectRoot };
}

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

async function startServer(extraEnv = {}, options = {}) {
  const port = await reservePort();
  const s02Fixture = options.s02Fixture ?? createS02Fixture();
  const statePath =
    options.statePath ??
    path.join(
      "/tmp",
      `xiaopeng-task4-t03-react-${process.pid}-${port}-${Date.now()}.sqlite3`,
    );
  const output = [];
  const child = spawn(
    PYTHON,
    [
      "-m",
      "uvicorn",
      options.appTarget ?? "task4_consistency.web.app:create_s02_test_app",
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
        PYTHONPYCACHEPREFIX: "/tmp/xiaopeng-task4-t03-react-pycache",
        TASK4_S01_STATE_PATH: statePath,
        TASK4_S01_TEST_STATE_PATH: statePath,
        TASK4_S02_TEST_STATE_PATH: statePath,
        TASK4_S01_TEST_BACKGROUND_ENABLED: "1",
        TASK4_S02_TEST_BACKGROUND_ENABLED: "1",
        TASK4_S01_DEMO_CREDENTIAL: DEMO_CREDENTIAL,
        TASK4_S01_DEMO_SUBJECT: "t03-browser-reviewer",
        TASK4_S01_OPERATOR_CREDENTIAL: OPERATOR_CREDENTIAL,
        TASK4_S01_OPERATOR_SUBJECT: "t03-browser-operator",
        TASK4_S01_TEST_SCENARIO_ID: SCENARIO,
        TASK4_S02_CREDENTIAL: S02_CREDENTIAL,
        TASK4_S02_SUBJECT: S02_SUBJECT,
        TASK4_S02_TENANT_ID: S02_TENANT,
        TASK4_S02_SOURCE_SYSTEM_ID: S02_SOURCE,
        TASK4_S02_TEST_REGISTRY_PATH: s02Fixture.registryPath,
        TASK4_S02_TEST_OBJECT_ROOT: s02Fixture.objectRoot,
        TASK4_S02_TEST_SCENARIO_ID: SCENARIO,
        ...extraEnv,
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  child.stdout.on("data", (chunk) => output.push(chunk.toString()));
  child.stderr.on("data", (chunk) => output.push(chunk.toString()));

  const baseURL = `http://127.0.0.1:${port}`;
  const deadline = Date.now() + 8_000;
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
    return { baseURL, child, output, statePath, s02Fixture };
  }
  const exited = once(child, "exit");
  if (child.exitCode === null) {
    child.kill("SIGKILL");
    await exited;
  }
  cleanupStatePath(statePath);
  fs.rmSync(s02Fixture.root, { recursive: true, force: true });
  throw new Error(`T03 React server did not start: ${output.join("")}`);
}

async function stopServer(server, options = {}) {
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
  if (options.preserveState !== true) {
    try {
      cleanupStatePath(server.statePath);
    } catch (error) {
      failures.push(error);
    }
  }
  try {
    if (server.s02Fixture !== undefined) {
      fs.rmSync(server.s02Fixture.root, { recursive: true, force: true });
    }
  } catch (error) {
    failures.push(error);
  }
  if (failures.length > 0) throw failures[0];
}

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

/** Submits the frozen C-DEMO scenario and waits for the background worker to
 * publish its Manual Review projection into the Reviewer queue. */
async function installManualWork(baseURL, reviewer) {
  const admission = await reviewer.request.post(
    `${baseURL}/controlled/s01/api/commands/submit`,
    { data: { scenario_id: SCENARIO, idempotency_key: "t03-react-admission" } },
  );
  expect(admission.ok()).toBeTruthy();
  const accepted = await admission.json();
  const deadline = Date.now() + 15_000;
  while (Date.now() < deadline) {
    const queue = await reviewer.request.get(
      `${baseURL}/controlled/s01/api/queries/queue`,
    );
    expect(queue.ok()).toBeTruthy();
    const body = await queue.json();
    const item = (body.items || []).find(
      (candidate) => candidate.application_id === accepted.application_id,
    );
    if (item !== undefined) return item;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`manual review work never appeared for ${accepted.application_id}`);
}

function trackPageDiagnostics(page, expectations = []) {
  const browserErrors = [];
  const consoleErrors = [];
  const networkErrors = [];
  const counts = {};
  for (const expectation of expectations) counts[expectation.name] = 0;
  page.on("pageerror", (error) => browserErrors.push(error.message));
  page.on("console", (message) => {
    if (message.type() !== "error") return;
    const location = message.location().url || "";
    if (location.endsWith("/favicon.ico")) return;
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
    consoleErrors.push(`${message.text()} @ ${location}`);
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

async function assertNoOverflow(page) {
  return page.evaluate(
    () => document.documentElement.scrollWidth <= window.innerWidth + 1,
  );
}

/** The reveal region must be the ONLY element carrying the source sentinel. */
async function assertOnlyOneSentinelElement(page, sentinel) {
  const matches = await page.evaluate((value) => {
    const results = [];
    const walker = document.createTreeWalker(
      document.body,
      NodeFilter.SHOW_TEXT,
    );
    let node = walker.nextNode();
    while (node !== null) {
      if (node.textContent && node.textContent.includes(value)) {
        const parent = node.parentElement;
        if (parent) results.push(parent.dataset.testid ?? null);
      }
      node = walker.nextNode();
    }
    return results;
  }, sentinel);
  expect(matches, `elements containing ${sentinel}`).toHaveLength(1);
  expect(matches[0]).toBe("review-reveal-source");
}

async function assertSentinelAbsentEverywhere(page, sentinel) {
  const bodyText = await page.locator("body").innerText();
  expect(bodyText).not.toContain(sentinel);
  const dom = await page.evaluate(() => document.body.innerHTML);
  expect(dom).not.toContain(sentinel);
  const url = await page.evaluate(() => `${location.href}${location.search}`);
  expect(url).not.toContain(sentinel);
  const storage = await page.evaluate(() => ({
    local: { ...localStorage },
    session: { ...sessionStorage },
  }));
  expect(JSON.stringify(storage)).not.toContain(sentinel);
}

async function runRevealCorrectionTracer(browser, viewport, label) {
  const resources = {};
  let failure;
  try {
    resources.server = await startServer();
    const server = resources.server;
    resources.reviewerContext = await browser.newContext({
      viewport,
      extraHTTPHeaders: { Authorization: `Bearer ${DEMO_CREDENTIAL}` },
    });
    const reviewer = await resources.reviewerContext.newPage();
    // The correction invalidates the old workspace and work item; their 404
    // responses are the expected existence-hiding contract while the
    // successor converges, never a fatal panel state.
    const workspaceGone404 = { name: "workspaceGone404", url: null, statusText: "404" };
    const workGone404 = { name: "workGone404", url: null, statusText: "404" };
    const diagnostics = trackPageDiagnostics(reviewer, [
      workspaceGone404,
      workGone404,
    ]);
    // Deterministic idempotency keys: the five command keys are minted at
    // mount (renew, release, submit, reveal, correction); each reveal
    // acceptance rotates a fresh key.  uuid(n) = nth crypto.randomUUID call.
    const uuidSequence = [];
    await reviewer.addInitScript(
      (log) => {
        let index = 0;
        crypto.randomUUID = () => {
          const value = `00000000-0000-4000-8000-${String(index)
            .padStart(12, "0")}`;
          log.push(value);
          index += 1;
          return value;
        };
      },
      uuidSequence,
    );
    const uuid = (index) =>
      `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`;

    const posts = [];
    reviewer.on("request", (request) => {
      const url = new URL(request.url());
      if (
        request.method() === "POST" &&
        url.pathname.includes("/commands/review-work-items/")
      ) {
        posts.push({ url: url.pathname, body: request.postDataJSON() });
      }
    });

    const shellResponse = await reviewer.goto(`${server.baseURL}${REACT_URL}`, {
      waitUntil: "networkidle",
    });
    expect(shellResponse.status()).toBe(200);
    await expect(reviewer.getByTestId("queue-panel")).toBeVisible();

    const item = await installManualWork(server.baseURL, reviewer);
    const workId = item.work_item_id;
    const applicationId = item.application_id;
    workspaceGone404.url = `${server.baseURL}/controlled/s01/api/queries/applications/${encodeURIComponent(applicationId)}/workspace`;
    workGone404.url = `${server.baseURL}/controlled/s01/api/queries/review-work-items/${encodeURIComponent(workId)}`;
    await reviewer.reload({ waitUntil: "networkidle" });
    await reviewer.getByRole("link", { name: new RegExp(workId) }).click();
    await expect(reviewer.getByTestId("review-panel")).toBeVisible();

    // 1. Masked before any action: the sentinels must not exist in visible
    // text, DOM, URL, or web storage.
    await assertSentinelAbsentEverywhere(reviewer, SOURCE_SENTINEL);
    await assertSentinelAbsentEverywhere(reviewer, MISREAD_SENTINEL);
    await expect(reviewer.getByTestId("review-evidence-masked")).toHaveCount(4);
    for (const masked of await reviewer
      .getByTestId("review-evidence-masked")
      .all()) {
      await expect(masked).toHaveText("[REDACTED]");
    }
    // The source-bearing invoice observation is the last link of the
    // selected finding (reg, pol, lease, inv).
    await expect(reviewer.getByTestId("review-evidence-link")).toHaveCount(4);

    // Claim, then the reveal controls become available.
    await reviewer.getByRole("button", { name: "认领" }).click();
    await expect(reviewer.getByTestId("review-command-status")).toContainText(
      "认领已接受",
    );
    await expect(reviewer.getByTestId("review-status")).toHaveText("claimed");

    // 2. Explicit reveal of exactly the requested field.
    await reviewer.getByTestId("review-reveal-button").nth(3).click();
    await expect(reviewer.getByTestId("review-command-status")).toContainText(
      "揭示已接受",
    );
    await expect(reviewer.getByTestId("review-reveal-source")).toHaveText(
      SOURCE_SENTINEL,
    );
    await assertOnlyOneSentinelElement(reviewer, SOURCE_SENTINEL);
    // The other three observations stay masked.
    const revealPosts = posts.filter((entry) =>
      entry.url.endsWith("/reveal-field-observation"),
    );
    expect(revealPosts).toHaveLength(1);
    expect(revealPosts[0].body.idempotency_key).toBe(uuid(3));

    // Authoritative reload scrubs the reveal and returns to masked.
    await reviewer.getByRole("button", { name: "重新加载" }).click();
    await expect(reviewer.getByTestId("review-evidence-masked")).toHaveCount(4);
    await assertSentinelAbsentEverywhere(reviewer, SOURCE_SENTINEL);

    // 3. Reveal once more and submit one valid correction with the fixed
    // idempotency key; the restricted reveal disappears as submission starts.
    await reviewer.getByTestId("review-reveal-button").nth(3).click();
    await expect(reviewer.getByTestId("review-reveal-source")).toHaveText(
      SOURCE_SENTINEL,
    );
    expect(posts.filter((entry) => entry.url.endsWith("/reveal-field-observation"))).toHaveLength(2);
    expect(
      posts.filter((entry) => entry.url.endsWith("/reveal-field-observation"))[1]
        .body.idempotency_key,
    ).toBe(uuid(5));
    await reviewer.getByTestId("review-correct-button").nth(3).click();
    await expect(reviewer.getByTestId("review-correction-form")).toBeVisible();
    await reviewer
      .getByTestId("review-correction-raw")
      .fill(SOURCE_SENTINEL);
    await expect(reviewer.getByTestId("review-correction-reason")).toHaveValue(
      "SOURCE_VALUE_MISREAD",
    );
    await reviewer.getByTestId("review-correction-submit").click();
    await assertSentinelAbsentEverywhere(reviewer, SOURCE_SENTINEL);

    const correctionPosts = posts.filter((entry) =>
      entry.url.endsWith("/correct-field-observation"),
    );
    expect(correctionPosts).toHaveLength(1);
    const correctionBody = correctionPosts[0].body;
    expect(correctionBody.idempotency_key).toBe(uuid(4));
    expect(correctionBody.correction).toEqual({
      schema_version: "field-observation-correction/1",
      finding_id: expect.any(String),
      observation_id: expect.any(String),
      document_id: "inv",
      document_role: "发票",
      field: "vin",
      raw: SOURCE_SENTINEL,
      source_location: {
        source_sha256: expect.stringMatching(/^[0-9a-f]{64}$/),
        source_page: 4,
        source_region: "region:1",
      },
      reason_code: "SOURCE_VALUE_MISREAD",
    });

    // 4. Command acceptance and the pending/reconciling surface; the
    // invalidated old workspace may become unavailable without losing the
    // shell, current-route, or history.  The UI is waiting immediately after
    // acceptance and converges to the server-owned successor shortly after.
    await expect(
      reviewer
        .getByTestId("review-correction-pending")
        .or(reviewer.getByTestId("review-correction-converged")),
    ).toBeVisible();
    await expect(reviewer.getByTestId("gate-panel")).toBeVisible();
    await expect(reviewer.getByTestId("history-panel")).toBeVisible();

    // 5. Wait for history/current-route convergence on the accepted
    // evidence revision: predecessor -> correction -> successor, exactly one
    // server-current run, and the displayed route equals the server response.
    await expect
      .poll(
        async () =>
          (await reviewer.getByTestId("review-history-runs").innerText()).split(
            "\n",
          ).length,
        { timeout: 30_000 },
      )
      .toBeGreaterThanOrEqual(2);
    const historyResponse = await reviewer.request.get(
      `${server.baseURL}/controlled/s01/api/queries/applications/${encodeURIComponent(applicationId)}/history`,
    );
    expect(historyResponse.ok()).toBeTruthy();
    const history = await historyResponse.json();
    expect(history.runs).toHaveLength(2);
    expect(history.runs.map((run) => run.current)).toEqual([false, true]);
    const predecessor = history.runs[0];
    const successor = history.runs[1];
    expect(predecessor.currentness_reason).toContain("EVIDENCE_CORRECTION_ACCEPTED");
    expect(history.corrections).toHaveLength(1);
    const correction = history.corrections[0];
    expect(correction.superseded_observation_id).not.toBe(
      correction.successor_observation_id,
    );
    const runsText = await reviewer.getByTestId("review-history-runs").innerText();
    expect(runsText).toContain(predecessor.run_id);
    expect(runsText).toContain(successor.run_id);
    await expect(
      reviewer.getByTestId("review-history-correction"),
    ).toHaveCount(1);
    const correctionText = await reviewer
      .getByTestId("review-history-correction")
      .innerText();
    expect(correctionText).toContain(correction.correction_id);
    expect(correctionText).toContain(correction.superseded_observation_id);
    expect(correctionText).toContain(correction.successor_observation_id);
    await expect(
      reviewer.getByTestId("review-history-correction"),
    ).toContainText("SOURCE_VALUE_MISREAD");
    await expect(reviewer.getByTestId("review-history-runs").first()).toContainText(
      "非当前",
    );

    const routeResponse = await reviewer.request.get(
      `${server.baseURL}/controlled/s01/api/queries/applications/${encodeURIComponent(applicationId)}/current-route`,
    );
    expect(routeResponse.ok()).toBeTruthy();
    const route = await routeResponse.json();
    expect(route.current_run_id).toBe(successor.run_id);
    await expect(reviewer.getByTestId("gate-route")).toHaveText(route.route);
    await expect(reviewer.getByTestId("gate-currentness")).toHaveText(
      route.currentness_reason,
    );
    await expect(reviewer.getByTestId("gate-phase")).toHaveText(route.phase);
    expect(route.evidence_revision).toBe(correction.evidence_revision);
    // The UI must leave the waiting state and explicitly converge to the
    // server-owned successor run and current route.
    await expect(reviewer.getByTestId("review-correction-converged")).toBeVisible();
    await expect(reviewer.getByTestId("review-correction-converged")).toContainText(
      String(route.evidence_revision),
    );
    await expect(reviewer.getByTestId("review-correction-converged")).toContainText(
      successor.run_id,
    );
    await expect(reviewer.getByTestId("review-correction-converged")).toContainText(
      route.route,
    );
    await expect(reviewer.getByTestId("review-correction-timeout")).toHaveCount(0);

    // 6. No restricted sentinel survives in any surface after convergence.
    await assertSentinelAbsentEverywhere(reviewer, SOURCE_SENTINEL);
    await assertSentinelAbsentEverywhere(reviewer, MISREAD_SENTINEL);
    await expect(reviewer.getByTestId("review-reveal-source")).toHaveCount(0);
    // The server-owned history read never echoes raw evidence values.
    expect(JSON.stringify(history)).not.toContain(SOURCE_SENTINEL);
    expect(JSON.stringify(history)).not.toContain(MISREAD_SENTINEL);
    expect(JSON.stringify(route)).not.toContain(SOURCE_SENTINEL);
    expect(JSON.stringify(route)).not.toContain(MISREAD_SENTINEL);
    expect(JSON.stringify(correctionPosts[0].body)).toContain(SOURCE_SENTINEL);
    expect(JSON.stringify(correctionBody)).toContain(SOURCE_SENTINEL);
    // The UI status text is sanitized: it names the command and revision but
    // never echoes corrected or revealed values.
    const statusText = await reviewer
      .getByTestId("review-command-status")
      .innerText();
    expect(statusText).not.toContain(SOURCE_SENTINEL);
    expect(statusText).not.toContain(MISREAD_SENTINEL);

    // Layout stays usable on both viewports.
    expect(await assertNoOverflow(reviewer)).toBe(true);
    expect(diagnostics.browserErrors).toEqual([]);
    expect(diagnostics.consoleErrors).toEqual([]);
    expect(diagnostics.networkErrors).toEqual([]);
    expect(diagnostics.counts.workspaceGone404).toBeGreaterThan(0);
  } catch (error) {
    failure = error;
    throw error;
  } finally {
    try {
      await settleCleanup([
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
}

const VIEWPORTS = [
  { width: 1280, height: 800, label: "desktop 1280x800" },
  { width: 390, height: 844, label: "mobile 390x844" },
];

/** Opens the production shell, installs the manual work item, claims it, and
 * registers the expected existence-hiding 404 diagnostics for the post-
 * correction invalidation. */
async function openClaimedReviewPanel(reviewer, server) {
  const shellResponse = await reviewer.goto(`${server.baseURL}${REACT_URL}`, {
    waitUntil: "networkidle",
  });
  expect(shellResponse.status()).toBe(200);
  await expect(reviewer.getByTestId("queue-panel")).toBeVisible();
  const item = await installManualWork(server.baseURL, reviewer);
  const workId = item.work_item_id;
  const applicationId = item.application_id;
  await reviewer.reload({ waitUntil: "networkidle" });
  await reviewer.getByRole("link", { name: new RegExp(workId) }).click();
  await expect(reviewer.getByTestId("review-panel")).toBeVisible();
  const diagnostics = trackPageDiagnostics(reviewer, [
    {
      name: "workspaceGone404",
      url: `${server.baseURL}/controlled/s01/api/queries/applications/${encodeURIComponent(applicationId)}/workspace`,
      statusText: "404",
    },
    {
      name: "workGone404",
      url: `${server.baseURL}/controlled/s01/api/queries/review-work-items/${encodeURIComponent(workId)}`,
      statusText: "404",
    },
  ]);
  await reviewer.getByRole("button", { name: "认领" }).click();
  await expect(reviewer.getByTestId("review-command-status")).toContainText(
    "认领已接受",
  );
  await expect(reviewer.getByTestId("review-status")).toHaveText("claimed");
  return { workId, applicationId, diagnostics };
}

/** Waits for the server-owned successor convergence and asserts the UI has
 * explicitly converged to it (never a pending or timed-out display). */
async function awaitConvergence(reviewer, server, applicationId, expectCurrentRun) {
  await expect
    .poll(
      async () =>
        (await reviewer.getByTestId("review-history-runs").innerText()).split(
          "\n",
        ).length,
      { timeout: 30_000 },
    )
    .toBeGreaterThanOrEqual(2);
  await expect(reviewer.getByTestId("review-correction-converged")).toBeVisible();
  const routeResponse = await reviewer.request.get(
    `${server.baseURL}/controlled/s01/api/queries/applications/${encodeURIComponent(applicationId)}/current-route`,
  );
  expect(routeResponse.ok()).toBeTruthy();
  const route = await routeResponse.json();
  if (expectCurrentRun !== undefined) {
    expect(route.current_run_id).toBe(expectCurrentRun);
  }
  await expect(reviewer.getByTestId("review-correction-converged")).toContainText(
    route.current_run_id,
  );
  await expect(reviewer.getByTestId("review-correction-converged")).toContainText(
    route.route,
  );
  return route;
}

/** Asserts clean diagnostics except for the deliberately fault-injected
 * correction POST (abort/conflict produce browser console/network noise that
 * is the point of the scenario, not an unexpected failure). */
async function assertCleanDiagnostics(diagnostics, faultedUrlPart) {
  const unexpectedConsole = diagnostics.consoleErrors.filter(
    (message) => !message.includes(faultedUrlPart),
  );
  const unexpectedNetwork = diagnostics.networkErrors.filter(
    (entry) => !entry.url.includes(faultedUrlPart),
  );
  expect(diagnostics.browserErrors).toEqual([]);
  expect(unexpectedConsole).toEqual([]);
  expect(unexpectedNetwork).toEqual([]);
}

/** Tabs until the active element satisfies the predicate; returns the active
 * element's data-testid.  Keyboard-only navigation proof helper. */
async function tabUntil(reviewer, predicate, maxTabs = 60) {
  for (let index = 0; index < maxTabs; index += 1) {
    await reviewer.keyboard.press("Tab");
    const testid = await reviewer.evaluate(
      () => document.activeElement?.dataset?.testid ?? null,
    );
    if (await predicate(reviewer)) return testid;
  }
  throw new Error("Tab navigation never reached the target control");
}

const isInvoiceRevealFocused = (reviewer) =>
  reviewer.evaluate(() => {
    const element = document.activeElement;
    if (
      !(element instanceof HTMLElement) ||
      element.dataset.testid !== "review-reveal-button"
    ) {
      return false;
    }
    const listItem = element.closest("li");
    return listItem !== null && listItem.textContent.includes("inv");
  });

const isInvoiceCorrectFocused = (reviewer) =>
  reviewer.evaluate(() => {
    const element = document.activeElement;
    if (
      !(element instanceof HTMLElement) ||
      element.dataset.testid !== "review-correct-button"
    ) {
      return false;
    }
    const listItem = element.closest("li");
    return listItem !== null && listItem.textContent.includes("inv");
  });

async function runLostResponseReplayTracer(browser) {
  const resources = {};
  let failure;
  try {
    resources.server = await startServer();
    const server = resources.server;
    resources.reviewerContext = await browser.newContext({
      viewport: { width: 1280, height: 800 },
      extraHTTPHeaders: { Authorization: `Bearer ${DEMO_CREDENTIAL}` },
    });
    const reviewer = await resources.reviewerContext.newPage();
    const posts = [];
    reviewer.on("request", (request) => {
      const url = new URL(request.url());
      if (
        request.method() === "POST" &&
        url.pathname.includes("/commands/review-work-items/")
      ) {
        posts.push({ url: url.pathname, body: request.postDataJSON() });
      }
    });
    const { applicationId, diagnostics } = await openClaimedReviewPanel(
      reviewer,
      server,
    );
    // The raw evidence crosses the boundary byte-for-byte: the whitespace is
    // part of the entered value.
    const paddedRaw = `  ${SOURCE_SENTINEL}  `;
    let correctionAttempts = 0;
    await reviewer.route("**/correct-field-observation", (route) => {
      correctionAttempts += 1;
      if (correctionAttempts === 1) {
        // The response is lost on the wire: transport outcome unknown.
        return route.abort();
      }
      return route.continue();
    });
    await reviewer.getByTestId("review-correct-button").nth(3).click();
    await expect(reviewer.getByTestId("review-correction-form")).toBeVisible();
    await reviewer.getByTestId("review-correction-raw").fill(paddedRaw);
    await reviewer.getByTestId("review-correction-submit").click();
    await expect(reviewer.getByTestId("review-command-status")).toContainText(
      "结果未知：网络未确认，重试将使用同一幂等键",
    );
    await expect(reviewer.getByTestId("retry-button")).toBeVisible();
    await reviewer.getByTestId("retry-button").click();
    await expect(
      reviewer
        .getByTestId("review-correction-pending")
        .or(reviewer.getByTestId("review-correction-converged")),
    ).toBeVisible();
    await routeContinueOnly(reviewer);
    const correctionPosts = posts.filter((entry) =>
      entry.url.endsWith("/correct-field-observation"),
    );
    expect(correctionPosts).toHaveLength(2);
    // The replay is the exact original command: same idempotency key and a
    // byte-identical body, never a reconstruction from current UI state.
    expect(correctionPosts[0].body.idempotency_key).toBe(
      correctionPosts[1].body.idempotency_key,
    );
    expect(JSON.stringify(correctionPosts[0].body)).toBe(
      JSON.stringify(correctionPosts[1].body),
    );
    expect(correctionPosts[1].body.correction.raw).toBe(paddedRaw);
    await awaitConvergence(reviewer, server, applicationId);
    const historyResponse = await reviewer.request.get(
      `${server.baseURL}/controlled/s01/api/queries/applications/${encodeURIComponent(applicationId)}/history`,
    );
    const history = await historyResponse.json();
    expect(history.runs).toHaveLength(2);
    expect(history.runs.map((run) => run.current)).toEqual([false, true]);
    expect(history.corrections).toHaveLength(1);
    await assertSentinelAbsentEverywhere(reviewer, SOURCE_SENTINEL);
    await assertSentinelAbsentEverywhere(reviewer, MISREAD_SENTINEL);
    await assertCleanDiagnostics(
      diagnostics,
      "/correct-field-observation",
    );
  } catch (error) {
    failure = error;
    throw error;
  } finally {
    try {
      await settleCleanup([
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
}

async function routeContinueOnly(reviewer) {
  await reviewer.unroute("**/correct-field-observation");
}

async function runStaleCorrectionReloadTracer(browser) {
  const resources = {};
  let failure;
  try {
    resources.server = await startServer();
    const server = resources.server;
    resources.reviewerContext = await browser.newContext({
      viewport: { width: 1280, height: 800 },
      extraHTTPHeaders: { Authorization: `Bearer ${DEMO_CREDENTIAL}` },
    });
    const reviewer = await resources.reviewerContext.newPage();
    const posts = [];
    reviewer.on("request", (request) => {
      const url = new URL(request.url());
      if (
        request.method() === "POST" &&
        url.pathname.includes("/commands/review-work-items/")
      ) {
        posts.push({ url: url.pathname, body: request.postDataJSON() });
      }
    });
    const { applicationId, diagnostics } = await openClaimedReviewPanel(
      reviewer,
      server,
    );
    let correctionAttempts = 0;
    await reviewer.route("**/correct-field-observation", (route) => {
      correctionAttempts += 1;
      if (correctionAttempts === 1) {
        return route.fulfill({
          status: 409,
          contentType: "application/json",
          body: JSON.stringify({
            detail: {
              error: "S03_STALE",
              reason_code: "STALE_WORK_ITEM_CLAIM",
            },
          }),
        });
      }
      return route.continue();
    });
    await reviewer.getByTestId("review-correct-button").nth(3).click();
    await expect(reviewer.getByTestId("review-correction-form")).toBeVisible();
    await reviewer.getByTestId("review-correction-raw").fill(SOURCE_SENTINEL);
    await reviewer.getByTestId("review-correction-submit").click();
    await expect(reviewer.getByTestId("review-command-status")).toContainText(
      "更正未接受（STALE_WORK_ITEM_CLAIM）：请重新加载权威上下文后再试",
    );
    // The definitive rejection scrubbed the reveal, the form, and the
    // restricted mutations; no restricted value may survive anywhere.
    await expect(reviewer.getByTestId("review-correction-form")).toHaveCount(0);
    await assertSentinelAbsentEverywhere(reviewer, SOURCE_SENTINEL);
    for (const button of await reviewer
      .getByTestId("review-reveal-button")
      .all()) {
      await expect(button).toBeDisabled();
    }
    // An authoritative reload recovers the fenced actions; the stale
    // correction never committed, so no invalidation shell may appear.
    await reviewer.getByRole("button", { name: "重新加载" }).click();
    await expect(reviewer.getByTestId("review-command-status")).toContainText(
      "等待操作",
    );
    await expect(reviewer.getByTestId("review-reveal-button").first()).toBeEnabled();
    await reviewer.getByTestId("review-correct-button").nth(3).click();
    await expect(reviewer.getByTestId("review-correction-form")).toBeVisible();
    await reviewer.getByTestId("review-correction-raw").fill(SOURCE_SENTINEL);
    await reviewer.getByTestId("review-correction-submit").click();
    await expect(
      reviewer
        .getByTestId("review-correction-pending")
        .or(reviewer.getByTestId("review-correction-converged")),
    ).toBeVisible();
    await routeContinueOnly(reviewer);
    await awaitConvergence(reviewer, server, applicationId);
    const correctionPosts = posts.filter((entry) =>
      entry.url.endsWith("/correct-field-observation"),
    );
    expect(correctionPosts).toHaveLength(2);
    // The successful command carries the exact entered raw byte-for-byte.
    expect(correctionPosts[1].body.correction.raw).toBe(SOURCE_SENTINEL);
    await assertSentinelAbsentEverywhere(reviewer, SOURCE_SENTINEL);
    await assertSentinelAbsentEverywhere(reviewer, MISREAD_SENTINEL);
    await assertCleanDiagnostics(
      diagnostics,
      "/correct-field-observation",
    );
  } catch (error) {
    failure = error;
    throw error;
  } finally {
    try {
      await settleCleanup([
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
}

async function runKeyboardTracer(browser, viewport) {
  const resources = {};
  let failure;
  try {
    resources.server = await startServer();
    const server = resources.server;
    resources.reviewerContext = await browser.newContext({
      viewport,
      extraHTTPHeaders: { Authorization: `Bearer ${DEMO_CREDENTIAL}` },
    });
    const reviewer = await resources.reviewerContext.newPage();
    const posts = [];
    reviewer.on("request", (request) => {
      const url = new URL(request.url());
      if (
        request.method() === "POST" &&
        url.pathname.includes("/commands/review-work-items/")
      ) {
        posts.push({ url: url.pathname, body: request.postDataJSON() });
      }
    });
    const { applicationId, diagnostics } = await openClaimedReviewPanel(
      reviewer,
      server,
    );
    // Claim was issued by mouse in the shared helper; the reveal, correction,
    // and rerun below are keyboard-only (Tab/Enter) with visible focus on
    // every restricted control.
    await tabUntil(
      reviewer,
      (page) =>
        page.evaluate(
          () => document.activeElement?.dataset?.testid === "review-reveal-button",
        ),
    );
    await tabUntil(reviewer, (page) => isInvoiceRevealFocused(page));
    await reviewer.keyboard.press("Enter");
    await expect(reviewer.getByTestId("review-reveal-source")).toHaveText(
      SOURCE_SENTINEL,
    );
    // The reveal button remounts when its pending state clears, so focus
    // returns to the document root; keyboard navigation continues with Tab
    // until the invoice correction button is reached again.
    await tabUntil(reviewer, (page) => isInvoiceCorrectFocused(page));
    await reviewer.keyboard.press("Enter");
    await expect(reviewer.getByTestId("review-correction-form")).toBeVisible();
    // Tab into the raw input, type the value, then Tab through the reason
    // select to the submit button and activate it with Enter.
    await reviewer.keyboard.press("Tab");
    expect(
      await reviewer.evaluate(
        () => document.activeElement?.dataset?.testid ?? null,
      ),
    ).toBe("review-correction-raw");
    await reviewer.keyboard.type(SOURCE_SENTINEL);
    await reviewer.keyboard.press("Tab");
    expect(
      await reviewer.evaluate(
        () => document.activeElement?.dataset?.testid ?? null,
      ),
    ).toBe("review-correction-reason");
    await reviewer.keyboard.press("Tab");
    expect(
      await reviewer.evaluate(
        () => document.activeElement?.dataset?.testid ?? null,
      ),
    ).toBe("review-correction-submit");
    await reviewer.keyboard.press("Enter");
    await expect(
      reviewer
        .getByTestId("review-correction-pending")
        .or(reviewer.getByTestId("review-correction-converged")),
    ).toBeVisible();
    await awaitConvergence(reviewer, server, applicationId);
    const correctionPosts = posts.filter((entry) =>
      entry.url.endsWith("/correct-field-observation"),
    );
    expect(correctionPosts).toHaveLength(1);
    expect(correctionPosts[0].body.correction.raw).toBe(SOURCE_SENTINEL);
    await assertSentinelAbsentEverywhere(reviewer, SOURCE_SENTINEL);
    await assertSentinelAbsentEverywhere(reviewer, MISREAD_SENTINEL);
    expect(await assertNoOverflow(reviewer)).toBe(true);
    expect(diagnostics.browserErrors).toEqual([]);
    expect(diagnostics.consoleErrors).toEqual([]);
    expect(diagnostics.networkErrors).toEqual([]);
  } catch (error) {
    failure = error;
    throw error;
  } finally {
    try {
      await settleCleanup([
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
}

if (process.env.T03_DEBUG_EXPORTS === "1") {
  module.exports.__startServerForDebug = startServer;
  module.exports.__installManualWork = installManualWork;
} else {
  // The restricted T03 flow renders revealed source text and submits
  // correction raw values; failure screenshots/traces would be a durable
  // copy of restricted evidence, so this owning spec never captures them.
  // The rest of the suite keeps its failure diagnostics.
  test.use({ screenshot: "off", trace: "off" });

  for (const viewport of VIEWPORTS) {
    test(`T03 production tracer (${viewport.label}): controlled reveal and correction rerun`, async ({
      browser,
    }) => {
      test.setTimeout(180_000);
      await runRevealCorrectionTracer(browser, viewport, viewport.label);
    });
  }

  test("T03 lost-response replay keeps the exact idempotency key and byte-identical body", async ({
    browser,
  }) => {
    test.setTimeout(180_000);
    await runLostResponseReplayTracer(browser);
  });

  test("T03 stale correction recovers through an authoritative reload", async ({
    browser,
  }) => {
    test.setTimeout(180_000);
    await runStaleCorrectionReloadTracer(browser);
  });

  for (const viewport of VIEWPORTS) {
    test(`T03 keyboard-only reveal/correct/rerun (${viewport.label})`, async ({
      browser,
    }) => {
      test.setTimeout(180_000);
      await runKeyboardTracer(browser, viewport);
    });
  }
}
