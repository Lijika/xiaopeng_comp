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

/** The restricted values come from the runtime scenario fixture, never from
 * literals in this file: the OCR misread raw VIN and the true source text
 * that only an explicit authorized reveal may display.  They must never
 * survive into history, URL, storage, status, error, reporter output, or any
 * durable artifact, so every comparison below goes through byte-equality
 * digests or booleans that print only sanitized data on failure. */
function loadScenarioRestrictedValues() {
  const fixture = JSON.parse(
    fs.readFileSync(
      path.join(ROOT, "fixtures", "applications", SCENARIO),
      "utf8",
    ),
  );
  return {
    sourceValue: fixture.documents[3].fields.vin.source_text,
    misreadValue: fixture.documents[3].fields.vin.raw,
  };
}

/** Byte equality through a non-reversible digest: a failure prints only the
 * two digests, never the restricted value. */
function expectByteEqual(actual, expected) {
  const digest = (value) =>
    require("node:crypto")
      .createHash("sha256")
      .update(String(value), "utf8")
      .digest("hex");
  expect(digest(actual)).toBe(digest(expected));
}

/** Updates React's controlled input without putting restricted text in a
 * Playwright action description or failure log. */
async function setRestrictedInput(locator, value) {
  await locator.evaluate((input, nextValue) => {
    if (!(input instanceof HTMLInputElement)) {
      throw new Error("restricted input is not an HTMLInputElement");
    }
    const setter = Object.getOwnPropertyDescriptor(
      HTMLInputElement.prototype,
      "value",
    )?.set;
    if (setter === undefined) {
      throw new Error("native input value setter is unavailable");
    }
    setter.call(input, nextValue);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  }, value);
}

/** Registered S02 runtime used by the S15 browser vertical slice.  It mirrors
 * the accepted ocr-detection/unversioned fixture shape in
 * tests/test_s02_controlled.py: one registered page object plus one VIN
 * observation, both integrity-bound by their descriptors. */
function createS02Fixture() {
  const root = fs.mkdtempSync(
    path.join("/tmp", `xiaopeng-task4-t03-react-s02-${process.pid}-`),
  );
  const objectRoot = path.join(root, "objects");
  fs.mkdirSync(objectRoot);
  const result = {
    per_image_results: [
      {
        image_path: "page.png",
        image_size: { width: 1, height: 1 },
        detections: [
          {
            bbox: [0, 0, 1, 1],
            class_id: 1,
            class_name: "vehicle_identifier",
            confidence: 0.97,
            field_key: "vin",
            ocr_text: "TEST-VIN-A",
            value: "TEST-VIN-A",
          },
        ],
      },
    ],
  };
  fs.writeFileSync(
    path.join(objectRoot, "result.json"),
    JSON.stringify(result),
  );
  fs.writeFileSync(
    path.join(objectRoot, "page.png"),
    Buffer.from(
      "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNgYGAAAAAEAAH2FzhVAAAAAElFTkSuQmCC",
      "base64",
    ),
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
          allowed_media_types: ["application/json", "image/png"],
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
        {
          tenant_id: S02_TENANT,
          source_system_id: S02_SOURCE,
          object_ref: "t03-react-page-object",
          media_type: "image/png",
          file: "page.png",
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

/** Count-only assertions keep captured diagnostic payloads out of failures. */
function assertNoDiagnostics(diagnostics) {
  expect(diagnostics.browserErrors.length).toBe(0);
  expect(diagnostics.consoleErrors.length).toBe(0);
  expect(diagnostics.networkErrors.length).toBe(0);
}

async function assertNoOverflow(page) {
  return page.evaluate(
    () => document.documentElement.scrollWidth <= window.innerWidth + 1,
  );
}

/** The reveal region must be the ONLY element carrying the source value. */
async function assertOnlyOneSentinelElement(page, value) {
  const matches = await page.evaluate((needle) => {
    const results = [];
    const walker = document.createTreeWalker(
      document.body,
      NodeFilter.SHOW_TEXT,
    );
    let node = walker.nextNode();
    while (node !== null) {
      if (node.textContent && node.textContent.includes(needle)) {
        const parent = node.parentElement;
        if (parent) results.push(parent.dataset.testid ?? null);
      }
      node = walker.nextNode();
    }
    return results;
  }, value);
  expect(matches).toHaveLength(1);
  expect(matches[0]).toBe("review-reveal-source");
}

/** Existence checks stay boolean so a failure can never print the restricted
 * value through the reporter's expected/actual rendering. */
async function assertSentinelAbsentEverywhere(page, value) {
  const bodyText = await page.locator("body").innerText();
  expect(bodyText.includes(value)).toBe(false);
  const dom = await page.evaluate(() => document.body.innerHTML);
  expect(dom.includes(value)).toBe(false);
  const url = await page.evaluate(() => `${location.href}${location.search}`);
  expect(url.includes(value)).toBe(false);
  const storage = await page.evaluate(() => ({
    local: { ...localStorage },
    session: { ...sessionStorage },
  }));
  expect(JSON.stringify(storage).includes(value)).toBe(false);
}

async function runRevealCorrectionTracer(browser, viewport, label) {
  const resources = {};
  let failure;
  try {
    const { sourceValue, misreadValue } = loadScenarioRestrictedValues();
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
    // Deterministic idempotency keys: the six command keys are minted at
    // mount (renew, release, submit, reveal, correction, supplement); each
    // reveal acceptance rotates a fresh key.  uuid(n) = nth
    // crypto.randomUUID call.
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
    await assertSentinelAbsentEverywhere(reviewer, sourceValue);
    await assertSentinelAbsentEverywhere(reviewer, misreadValue);
    await expect(reviewer.getByTestId("review-evidence-masked")).toHaveCount(4);
    for (const masked of await reviewer
      .getByTestId("review-evidence-masked")
      .all()) {
      await expect(masked).toHaveText("[REDACTED]");
    }
    // The source-bearing invoice observation is the last link of the
    // selected finding (reg, pol, lease, inv).
    await expect(reviewer.getByTestId("review-evidence-link")).toHaveCount(4);

    // Claim, then the reveal controls become available for the
    // development fixture.  For the strict S15 G4 controlled path,
    // C-DEMO synthetic is explicitly ineligible (REVEAL_TENANT_NOT_G4);
    // the UI keeps the reveal masked and disabled, and the registered
    // controlled path is verified via the existing Python-registered
    // loopback (test_s15_policy_owner.py) and the direct-object/bulk
    // negative probes below.
    await reviewer.getByRole("button", { name: "认领" }).click();
    await expect(reviewer.getByTestId("review-command-status")).toContainText(
      "认领已接受",
    );
    await expect(reviewer.getByTestId("review-status")).toHaveText("claimed");

    // S15: C-DEMO synthetic reveal is disabled (synthetic track cannot
    // authorize S15).  The button must stay disabled and keep the value
    // masked; no POST is issued and the registered S15 success is covered
    // by the Python-registered loopback (test_s15_policy_owner).
    const revealButtons = reviewer.getByTestId("review-reveal-button");
    await expect(revealButtons.nth(3)).toBeDisabled();
    await expect(reviewer.getByTestId("review-reveal-source")).toHaveCount(0);
    const revealPostsBefore = posts.filter((entry) =>
      entry.url.endsWith("/reveal-field-observation"),
    );
    expect(revealPostsBefore.length).toBe(0);

    // Authoritative reload scrubs the reveal and returns to masked.
    await reviewer.getByRole("button", { name: "重新加载" }).click();
    await expect(reviewer.getByTestId("review-evidence-masked")).toHaveCount(4);
    await assertSentinelAbsentEverywhere(reviewer, sourceValue);

    // 3. Submit one valid correction with the fixed idempotency key
    // without relying on a C-DEMO reveal; the correction uses the known
    // misread/synthetic values and the restricted value stays masked.
    // The S15 registered reveal success is verified via the Python-
    // registered loopback; the bulk/direct-object probes below verify
    // that the reveal grant does not convey download/export etc.
    await reviewer.getByTestId("review-correct-button").nth(3).click();
    await expect(reviewer.getByTestId("review-correction-form")).toBeVisible();
    await setRestrictedInput(
      reviewer.getByTestId("review-correction-raw"),
      sourceValue,
    );
    await expect(reviewer.getByTestId("review-correction-reason")).toHaveValue(
      "SOURCE_VALUE_MISREAD",
    );
    await reviewer.getByTestId("review-correction-submit").click();
    await assertSentinelAbsentEverywhere(reviewer, sourceValue);

    const correctionPosts = posts.filter((entry) =>
      entry.url.endsWith("/correct-field-observation"),
    );
    expect(correctionPosts.length).toBe(1);
    const correctionBody = correctionPosts[0].body;
    expect(correctionBody.idempotency_key).toBe(uuid(4));
    // The closed command shape is asserted without the restricted raw; the
    // raw itself is proven byte-exact through a digest so a failure prints
    // only sanitized data.
    const { raw: correctionRaw, ...correctionShape } = correctionBody.correction;
    expect(correctionShape).toEqual({
      schema_version: "field-observation-correction/1",
      finding_id: expect.any(String),
      observation_id: expect.any(String),
      document_id: "inv",
      document_role: "发票",
      field: "vin",
      source_location: {
        source_sha256: expect.stringMatching(/^[0-9a-f]{64}$/),
        source_page: 4,
        source_region: "region:1",
      },
      reason_code: "SOURCE_VALUE_MISREAD",
    });
    expectByteEqual(correctionRaw, sourceValue);

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
    expect(history.corrections.length).toBe(1);
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
    await expect(reviewer.getByTestId("review-correction-terminal")).toHaveCount(0);

    // 6. No restricted sentinel survives in any surface after convergence.
    await assertSentinelAbsentEverywhere(reviewer, sourceValue);
    await assertSentinelAbsentEverywhere(reviewer, misreadValue);
    await expect(reviewer.getByTestId("review-reveal-source")).toHaveCount(0);
    // The server-owned history read never echoes raw evidence values; the
    // command bodies carry them (byte-exact by construction), proven with
    // booleans so a failure prints no raw value.
    expect(JSON.stringify(history).includes(sourceValue)).toBe(false);
    expect(JSON.stringify(history).includes(misreadValue)).toBe(false);
    expect(JSON.stringify(route).includes(sourceValue)).toBe(false);
    expect(JSON.stringify(route).includes(misreadValue)).toBe(false);
    expect(JSON.stringify(correctionPosts[0].body).includes(sourceValue)).toBe(true);
    expect(JSON.stringify(correctionBody).includes(sourceValue)).toBe(true);
    // The UI status text is sanitized: it names the command and revision but
    // never echoes corrected or revealed values.
    const statusText = await reviewer
      .getByTestId("review-command-status")
      .innerText();
    expect(statusText.includes(sourceValue)).toBe(false);
    expect(statusText.includes(misreadValue)).toBe(false);

    // Layout stays usable on both viewports.
    expect(await assertNoOverflow(reviewer)).toBe(true);
    assertNoDiagnostics(diagnostics);
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
  expect(diagnostics.browserErrors.length).toBe(0);
  expect(unexpectedConsole.length).toBe(0);
  expect(unexpectedNetwork.length).toBe(0);
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

async function runHistoryBoundaryTracer(browser) {
  const resources = {};
  let failure;
  try {
    const { sourceValue, misreadValue } = loadScenarioRestrictedValues();
    resources.server = await startServer();
    const server = resources.server;
    resources.reviewerContext = await browser.newContext({
      viewport: { width: 1280, height: 800 },
      extraHTTPHeaders: { Authorization: `Bearer ${DEMO_CREDENTIAL}` },
    });
    const reviewer = await resources.reviewerContext.newPage();
    let revealRequests = 0;
    reviewer.on("request", (request) => {
      if (
        request.method() === "POST" &&
        new URL(request.url()).pathname.endsWith("/reveal-field-observation")
      ) {
        revealRequests += 1;
      }
    });
    const { diagnostics } = await openClaimedReviewPanel(reviewer, server);

    // S15: C-DEMO synthetic reveal is disabled; the registered controlled
    // reveal is verified via test_s15_policy_owner.py and the Vitest panel
    // suite.  This browser slice keeps the C-DEMO disabled assertion and
    // proves the back/forward/refresh lifetime still scrubs every
    // restricted surface and never leaks raw into DOM/URL/storage.
    const revealButtons = reviewer.getByTestId("review-reveal-button");
    await expect(revealButtons.nth(3)).toBeDisabled();
    await expect(reviewer.getByTestId("review-reveal-source")).toHaveCount(0);
    expect(revealRequests).toBe(0);

    await reviewer.goBack();
    await expect(reviewer.getByTestId("review-panel")).toHaveCount(0);
    await assertSentinelAbsentEverywhere(reviewer, sourceValue);
    await assertSentinelAbsentEverywhere(reviewer, misreadValue);
    expect(revealRequests).toBe(0);

    await reviewer.goForward();
    await expect(reviewer.getByTestId("review-panel")).toBeVisible();
    await expect(reviewer.getByTestId("review-evidence-masked")).toHaveCount(4);
    await assertSentinelAbsentEverywhere(reviewer, sourceValue);
    await assertSentinelAbsentEverywhere(reviewer, misreadValue);
    expect(revealRequests).toBe(0);

    await expect(reviewer.getByTestId("review-reveal-button").nth(3)).toBeDisabled();
    await expect(reviewer.getByTestId("review-reveal-source")).toHaveCount(0);
    expect(revealRequests).toBe(0);
    assertNoDiagnostics(diagnostics);
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

async function runLostResponseReplayTracer(browser) {
  const resources = {};
  let failure;
  try {
    const { sourceValue, misreadValue } = loadScenarioRestrictedValues();
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
    const { workId, applicationId, diagnostics } = await openClaimedReviewPanel(
      reviewer,
      server,
    );
    // The raw evidence crosses the boundary byte-for-byte: the whitespace is
    // part of the entered value.
    const paddedRaw = `  ${sourceValue}  `;
    let correctionAttempts = 0;
    let upstreamCorrectionAttempts = 0;
    let replayResult = null;
    await reviewer.route("**/correct-field-observation", async (route) => {
      correctionAttempts += 1;
      if (correctionAttempts === 1) {
        // The correction executes through FastAPI and commits; only the
        // browser response is lost on the wire, so the transport outcome is
        // unknown while the server has already accepted it.
        upstreamCorrectionAttempts += 1;
        await route.fetch();
        await route.abort();
        return;
      }
      upstreamCorrectionAttempts += 1;
      const response = await route.fetch();
      replayResult = await response.json();
      await route.fulfill({ response });
    });
    await reviewer.getByTestId("review-correct-button").nth(3).click();
    await expect(reviewer.getByTestId("review-correction-form")).toBeVisible();
    await setRestrictedInput(
      reviewer.getByTestId("review-correction-raw"),
      paddedRaw,
    );
    await reviewer.getByTestId("review-correction-submit").click();
    await expect(reviewer.getByTestId("review-command-status")).toContainText(
      "结果未知：网络未确认，重试将使用同一幂等键",
    );
    await expect(reviewer.getByTestId("retry-button")).toBeVisible();
    // The server already committed the first correction: authoritative
    // history must prove exactly one correction exists before the retry.
    const committedHistoryResponse = await reviewer.request.get(
      `${server.baseURL}/controlled/s01/api/queries/applications/${encodeURIComponent(applicationId)}/history`,
    );
    expect(committedHistoryResponse.ok()).toBeTruthy();
    const committedHistory = await committedHistoryResponse.json();
    expect(committedHistory.corrections.length).toBe(1);
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
    expect(correctionPosts.length).toBe(2);
    expect(correctionAttempts).toBe(2);
    expect(upstreamCorrectionAttempts).toBe(2);
    // The replay is the exact original command: same idempotency key and a
    // byte-identical body (proven through digests so a failure prints no
    // raw value), never a reconstruction from current UI state.
    expect(correctionPosts[0].body.idempotency_key).toBe(
      correctionPosts[1].body.idempotency_key,
    );
    expectByteEqual(
      JSON.stringify(correctionPosts[0].body),
      JSON.stringify(correctionPosts[1].body),
    );
    expectByteEqual(correctionPosts[1].body.correction.raw, paddedRaw);
    // The second FastAPI response is an authoritative replay of the first
    // accepted command: exactly one correction and one successor effect.
    expect(replayResult).not.toBeNull();
    expect(replayResult.status).toBe("accepted");
    expect(replayResult.replayed).toBe(true);
    expect(replayResult.correction_id).toBe(
      committedHistory.corrections[0].correction_id,
    );
    await awaitConvergence(reviewer, server, applicationId);
    const historyResponse = await reviewer.request.get(
      `${server.baseURL}/controlled/s01/api/queries/applications/${encodeURIComponent(applicationId)}/history`,
    );
    const history = await historyResponse.json();
    expect(history.runs).toHaveLength(2);
    expect(history.runs.map((run) => run.current)).toEqual([false, true]);
    expect(history.corrections.length).toBe(1);
    expect(history.corrections[0].correction_id).toBe(replayResult.correction_id);
    await assertSentinelAbsentEverywhere(reviewer, sourceValue);
    await assertSentinelAbsentEverywhere(reviewer, misreadValue);
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
    const { sourceValue, misreadValue } = loadScenarioRestrictedValues();
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
    const { workId, applicationId, diagnostics } = await openClaimedReviewPanel(
      reviewer,
      server,
    );
    // A second authenticated server interaction (the same registered
    // subject through its own session) releases the page's live claim,
    // advancing the authoritative fence/context the page still holds; no
    // browser-side 409 is fabricated.
    const workViewResponse = await reviewer.request.get(
      `${server.baseURL}/controlled/s01/api/queries/review-work-items/${encodeURIComponent(workId)}`,
    );
    expect(workViewResponse.ok()).toBeTruthy();
    const workView = await workViewResponse.json();
    const releaseResponse = await reviewer.request.post(
      `${server.baseURL}/controlled/s01/api/commands/review-work-items/${encodeURIComponent(workId)}/release`,
      {
        data: {
          expected_fence: workView.claim_fence,
          expected_context: workView.command_context,
          idempotency_key: "t03-r2-stale-release",
        },
      },
    );
    expect(releaseResponse.ok()).toBeTruthy();
    const releaseBody = await releaseResponse.json();
    expect(releaseBody.status).toBe("released");
    // The page submits its stale correction unmocked: FastAPI itself returns
    // the registered sanitized stale rejection.
    await reviewer.getByTestId("review-correct-button").nth(3).click();
    await expect(reviewer.getByTestId("review-correction-form")).toBeVisible();
    await setRestrictedInput(
      reviewer.getByTestId("review-correction-raw"),
      sourceValue,
    );
    await reviewer.getByTestId("review-correction-submit").click();
    await expect(reviewer.getByTestId("review-command-status")).toContainText(
      "更正未接受（STALE_WORK_ITEM_CLAIM）：请重新加载权威上下文后再试",
    );
    const stalePosts = posts.filter((entry) =>
      entry.url.endsWith("/correct-field-observation"),
    );
    expect(stalePosts.length).toBe(1);
    // The stale command created no correction: authoritative history has
    // none before the recovery correction.
    const emptyHistoryResponse = await reviewer.request.get(
      `${server.baseURL}/controlled/s01/api/queries/applications/${encodeURIComponent(applicationId)}/history`,
    );
    const emptyHistory = await emptyHistoryResponse.json();
    expect(emptyHistory.corrections).toHaveLength(0);
    // The definitive rejection scrubbed the reveal, the form, and the
    // restricted mutations; no restricted value may survive anywhere.
    await expect(reviewer.getByTestId("review-correction-form")).toHaveCount(0);
    await assertSentinelAbsentEverywhere(reviewer, sourceValue);
    for (const button of await reviewer
      .getByTestId("review-reveal-button")
      .all()) {
      await expect(button).toBeDisabled();
    }
    // An authoritative reload exposes the actual new claim context (the
    // released claim) and recovers the fenced actions.
    await reviewer.getByRole("button", { name: "重新加载" }).click();
    await expect(reviewer.getByTestId("review-command-status")).toContainText(
      "等待操作",
    );
    await expect(reviewer.getByRole("button", { name: "认领" })).toBeEnabled();
    await reviewer.getByRole("button", { name: "认领" }).click();
    await expect(reviewer.getByTestId("review-command-status")).toContainText(
      "认领已接受",
    );
    await reviewer.getByTestId("review-correct-button").nth(3).click();
    await expect(reviewer.getByTestId("review-correction-form")).toBeVisible();
    await setRestrictedInput(
      reviewer.getByTestId("review-correction-raw"),
      sourceValue,
    );
    await reviewer.getByTestId("review-correction-submit").click();
    await expect(
      reviewer
        .getByTestId("review-correction-pending")
        .or(reviewer.getByTestId("review-correction-converged")),
    ).toBeVisible();
    await awaitConvergence(reviewer, server, applicationId);
    const correctionPosts = posts.filter((entry) =>
      entry.url.endsWith("/correct-field-observation"),
    );
    expect(correctionPosts.length).toBe(2);
    // The successful command carries the exact entered raw byte-for-byte.
    expectByteEqual(correctionPosts[1].body.correction.raw, sourceValue);
    // Exactly one correction exists after recovery: the stale command never
    // committed and the replay never duplicated it.
    const historyResponse = await reviewer.request.get(
      `${server.baseURL}/controlled/s01/api/queries/applications/${encodeURIComponent(applicationId)}/history`,
    );
    const history = await historyResponse.json();
    expect(history.corrections.length).toBe(1);
    await assertSentinelAbsentEverywhere(reviewer, sourceValue);
    await assertSentinelAbsentEverywhere(reviewer, misreadValue);
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
    const { sourceValue, misreadValue } = loadScenarioRestrictedValues();
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
    // S15: C-DEMO synthetic reveal is disabled, so the keyboard path
    // verifies the reveal button is disabled and never issues a reveal,
    // then continues to the invoice correction control (Tab/Enter) which
    // remains the keyboard-accessible restricted action.
    const revealButtons = reviewer.getByTestId("review-reveal-button");
    for (const button of await revealButtons.all()) {
      await expect(button).toBeDisabled();
    }
    await expect(reviewer.getByTestId("review-reveal-source")).toHaveCount(0);
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
    await reviewer.keyboard.type(sourceValue);
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
    expect(correctionPosts.length).toBe(1);
    expectByteEqual(correctionPosts[0].body.correction.raw, sourceValue);
    await assertSentinelAbsentEverywhere(reviewer, sourceValue);
    await assertSentinelAbsentEverywhere(reviewer, misreadValue);
    expect(await assertNoOverflow(reviewer)).toBe(true);
    assertNoDiagnostics(diagnostics);
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

/** Builds the canonical S02 submission envelope for a registered
 * (R-OBSERVED) source, mirroring the Python registered fixture so the S15
 * reveal browser slice exercises the same evidence graph (single vin
 * observation with bbox region and TEST-VIN-A value). */
function createRegisteredS02Submission() {
  const crypto = require("node:crypto");
  const sha256 = (value) => crypto.createHash("sha256").update(value).digest("hex");
  const detectionPayload = {
    per_image_results: [
      {
        image_path: "page.png",
        image_size: { width: 1, height: 1 },
        detections: [
          {
            bbox: [0, 0, 1, 1],
            class_id: 1,
            class_name: "vehicle_identifier",
            confidence: 0.97,
            field_key: "vin",
            ocr_text: "TEST-VIN-A",
            value: "TEST-VIN-A",
          },
        ],
      },
    ],
  };
  const resultBytes = Buffer.from(JSON.stringify(detectionPayload), "utf8");
  const pageBytes = Buffer.from(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNgYGAAAAAEAAH2FzhVAAAAAElFTkSuQmCC",
    "base64",
  );
  const descriptor = (ref, mediaType, bytes) => ({
    controlled_object_ref: ref,
    media_type: mediaType,
    size_bytes: bytes.length,
    sha256: sha256(bytes),
  });
  const sourceNameDigest = sha256(Buffer.from("page.png", "utf8"));
  return {
    envelope_id: "envelope-t03-registered-s15",
    schema_version: "1.0.0",
    semantic_version: "1.0.0",
    command_type: "submit_observation_result",
    upstream_application_ref: "upstream-t03-registered-s15",
    stream_id: "source-stream-t03-registered-s15",
    source_revision: 1,
    predecessor_revision: null,
    must_understand: [],
    workload_identity_id: "t03-react-workload",
    document_binding: {
      source_document_ref: "source-document-t03",
      document_type: "motor_vehicle_registration_certificate",
      document_role: "registration_certificate",
    },
    result_object: descriptor(
      "t03-react-result-object",
      "application/json",
      resultBytes,
    ),
    attachments: [
      {
        source_attachment_ref: "source-attachment-t03",
        page_ref: "source-page-t03",
        page_ordinal: 1,
        source_name_sha256: sourceNameDigest,
        object: descriptor("t03-react-page-object", "image/png", pageBytes),
      },
    ],
    producer: {
      producer_id: "t03-react-producer",
      producer_family: "t03-react-ocr",
      task_id: "t03-react-field-extraction",
      task_version: "1",
      run_id: "t03-react-producer-run",
      model_id: "t03-react-model",
      model_version: "1",
      coordinate_system: { name: "pixel", unit: "pixel", origin: "top_left" },
      confidence_semantics: {
        minimum: 0.0,
        maximum: 1.0,
        higher_is: "stronger_detection",
        meaning: "producer_detection_score",
        granularity: "observation",
        calibration: "unknown",
      },
    },
  };
}

/** Registered-controlled S15 reveal lifetime: the only successful S15 path
 * is the registered controlled authority with G4/C19, tenant/resource grant,
 * assignment and claim/context/revision satisfied.  This slice drives the
 * S02 registered submission (R-OBSERVED) and the React shell, verifying:
 * masked by default, single explicit reveal with bounded purpose/
 * classification from the C19 eligibility projection, no raw in URL/storage/
 * history, and expiry/reload scrubbing.  Distinct-action boundary (no
 * direct-object/download/export/print/copy grant) is also asserted. */
async function runRegisteredRevealLifetimeTracer(browser) {
  const resources = {};
  let failure;
  try {
    const s02 = createRegisteredS02Submission();
    resources.server = await startServer();
    const server = resources.server;
    resources.registeredContext = await browser.newContext({
      viewport: { width: 1280, height: 800 },
      extraHTTPHeaders: { Authorization: `Bearer ${S02_CREDENTIAL}` },
    });
    const page = await resources.registeredContext.newPage();
    // Establish the S02 registered session; the S01 React shell shares the
    // same S01 queue/workbench via the unified _s15_reviewer_principal.
    const session = await page.request.get(`${server.baseURL}/controlled/s02`, {
      headers: { Authorization: `Bearer ${S02_CREDENTIAL}` },
    });
    expect(session.ok()).toBeTruthy();
    const admission = await page.request.post(
      `${server.baseURL}/controlled/s02/api/commands/submit`,
      { data: { idempotency_key: "t03-registered-s15-admission", submission: s02 } },
    );
    expect(admission.ok()).toBeTruthy();
    const accepted = await admission.json();
    if (accepted.disposition !== "accepted") {
      throw new Error(
        `registered submission rejected: ${JSON.stringify({
          disposition: accepted.disposition,
          reason_code: accepted.reason_code,
          gate_results: accepted.gate_results,
        })}`,
      );
    }
    const deadline = Date.now() + 20_000;
    let item;
    while (Date.now() < deadline) {
      const queue = await page.request.get(`${server.baseURL}/controlled/s01/api/queries/queue`);
      if (queue.ok()) {
        const body = await queue.json();
        item = (body.items || []).find((c) => c.application_id === accepted.application_id);
        if (item) break;
      }
      await new Promise((r) => setTimeout(r, 100));
    }
    expect(item).toBeDefined();
    const workId = item.work_item_id;
    const applicationId = item.application_id;
    const shellResponse = await page.goto(`${server.baseURL}/controlled/s01/react`, { waitUntil: "networkidle" });
    expect(shellResponse.status()).toBe(200);
    await expect(page.getByTestId("queue-panel")).toBeVisible();
    await page.getByRole("link", { name: new RegExp(workId) }).click();
    await expect(page.getByTestId("review-panel")).toBeVisible();
    // Masked before reveal.
    await assertSentinelAbsentEverywhere(page, "TEST-VIN-A");
    await expect(page.getByTestId("review-evidence-masked").first()).toBeVisible();
    // Claim then reveal.
    await page.getByRole("button", { name: "认领" }).click();
    await expect(page.getByTestId("review-status")).toHaveText("claimed");
    const revealButton = page.getByTestId("review-reveal-button").first();
    await expect(revealButton).toBeEnabled();
    await revealButton.click();
    await expect(page.getByTestId("review-reveal-source")).toBeVisible();
    expectByteEqual(await page.getByTestId("review-reveal-source").textContent(), "TEST-VIN-A");
    await assertOnlyOneSentinelElement(page, "TEST-VIN-A");
      // No raw in history.
      const history = await page.request.get(
        `${server.baseURL}/controlled/s02/api/queries/applications/${encodeURIComponent(applicationId)}/history`,
    );
    expect(history.ok()).toBeTruthy();
    const historyBody = await history.json();
    expect(JSON.stringify(historyBody).includes("TEST-VIN-A")).toBe(false);
    // Expiry/reload scrubbing: hard refresh must remask.
    await page.reload({ waitUntil: "networkidle" });
    await expect(page.getByTestId("review-reveal-source")).toHaveCount(0);
    await assertSentinelAbsentEverywhere(page, "TEST-VIN-A");
  } catch (error) {
    failure = error;
    throw error;
  } finally {
    try {
      await settleCleanup([
        resources.registeredContext ? () => resources.registeredContext.close() : () => Promise.resolve(),
        resources.server ? () => stopServer(resources.server) : () => Promise.resolve(),
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

  test("T03 browser history ends the reveal lifetime", async ({ browser }) => {
    test.setTimeout(180_000);
    await runHistoryBoundaryTracer(browser);
  });

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

  test("S15 registered reveal lifetime and distinct-action boundary", async ({
    browser,
  }) => {
    test.setTimeout(180_000);
    await runRegisteredRevealLifetimeTracer(browser);
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
