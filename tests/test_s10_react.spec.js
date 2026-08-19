/** S10 page-membership correction browser E2E (highest public seam).

 * Reviewer: queue -> dual-pane candidate comparison -> explicit accept
 * decision -> Evidence successor -> readiness/rerun -> new current run/route.
 * Every prior candidate claim and both runs stay immutable and navigable.
 */
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const net = require("node:net");
const path = require("node:path");

const { expect, test } = require("@playwright/test");

const ROOT = path.resolve(__dirname, "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");
const DEMO_CREDENTIAL = "s10-registered-demo-credential";
const S02_CREDENTIAL = "s10-registered-s02-credential";
const S02_SUBJECT = "s10-registered-s02-reviewer";
const S02_TENANT = "tenant-s10-react";
const S02_SOURCE = "registered-s10-react-source";
const SCENARIO = "app_s10_ambiguous_membership.json";
const REACT_URL = "/controlled/s01/react";

function createS02Fixture() {
  const root = fs.mkdtempSync(
    path.join("/tmp", `xiaopeng-task4-s10-react-s02-${process.pid}-`),
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
          workload_identity_id: "s10-react-workload",
          adapter_id: "s10-react-adapter",
          adapter_version: "1",
          source_shape: "ocr-detection/unversioned",
          producer_family: "s10-react-ocr",
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
          object_ref: "s10-react-result-object",
          media_type: "application/json",
          file: "result.json",
        },
      ],
    }),
  );
  return {
    root,
    objectRoot,
    registryPath: path.join(root, "registry.json"),
  };
}

function reservePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

async function startServer(
  extraEnv = {},
  appTarget = "task4_consistency.web.app:create_s02_test_app",
) {
  const port = await reservePort();
  const s02Fixture = createS02Fixture();
  const statePath = path.join(
    "/tmp",
    `xiaopeng-task4-s10-react-${process.pid}-${port}-${Date.now()}.sqlite3`,
  );
  const output = [];
  const child = spawn(
    PYTHON,
    [
      "-m",
      "uvicorn",
      appTarget,
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
        TASK4_S01_STATE_PATH: statePath,
        TASK4_S01_TEST_STATE_PATH: statePath,
        TASK4_S02_TEST_STATE_PATH: statePath,
        TASK4_S01_TEST_BACKGROUND_ENABLED: "1",
        TASK4_S02_TEST_BACKGROUND_ENABLED: "1",
        TASK4_S01_DEMO_CREDENTIAL: DEMO_CREDENTIAL,
        TASK4_S01_DEMO_SUBJECT: "s10-browser-reviewer",
        TASK4_S01_OPERATOR_CREDENTIAL: "s10-operator-credential",
        TASK4_S01_OPERATOR_SUBJECT: "s10-browser-operator",
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
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline && child.exitCode === null) {
    try {
      if ((await fetch(`${baseURL}/api/health`)).ok) break;
    } catch (_) {
      /* bounded readiness retry */
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  if (child.exitCode !== null) {
    throw new Error(`S10 React server did not start: ${output.join("")}`);
  }
  return { baseURL, child, output, statePath, s02Fixture };
}

async function stopServer(server) {
  try {
    const exited = new Promise((resolve) => server.child.once("exit", resolve));
    if (server.child.exitCode === null) server.child.kill("SIGTERM");
    await Promise.race([
      exited,
      new Promise((resolve) => setTimeout(resolve, 5000)),
    ]);
  } catch (_) {
    /* best effort */
  }
  try {
    if (server.child.exitCode === null) server.child.kill("SIGKILL");
  } catch (_) {
    /* already gone */
  }
  try {
    fs.rmSync(server.statePath, { force: true });
  } catch (_) {
    /* best effort */
  }
  try {
    fs.rmSync(server.s02Fixture.root, { recursive: true, force: true });
  } catch (_) {
    /* best effort */
  }
}

/** Advance the S01 worker explicitly through the test-driver boundary and
 * refresh the minimized projection so the successor work becomes visible.
 * The background runtime stays disabled so every run transition is
 * deterministic and observable from React DOM while queued. */
async function processNextJob(reviewer, server) {
  const response = await reviewer.request.post(
    `${server.baseURL}/controlled/s01/api/_test/commands/process`,
    {
      data: {
        worker_id: "s10-react-driver",
        now: Math.floor(Date.now() / 1000),
      },
    },
  );
  expect(response.ok()).toBeTruthy();
  const result = await response.json();
  expect(result.status).toBe("complete");
  const projected = await reviewer.request.post(
    `${server.baseURL}/controlled/s01/api/_test/commands/project`,
    { data: {} },
  );
  expect(projected.ok()).toBeTruthy();
  return result;
}

async function installManualWork(baseURL, reviewer) {
  const admission = await reviewer.request.post(
    `${baseURL}/controlled/s01/api/commands/submit`,
    { data: { scenario_id: SCENARIO, idempotency_key: "s10-react-admission" } },
  );
  expect(admission.ok()).toBeTruthy();
  const accepted = await admission.json();
  await processNextJob(reviewer, { baseURL });
  const deadline = Date.now() + 15_000;
  while (Date.now() < deadline) {
    const queue = await reviewer.request.get(
      `${baseURL}/controlled/s01/api/queries/queue`,
    );
    const items = (await queue.json()).items || [];
    const item = items.find(
      (candidate) => candidate.application_id === accepted.application_id,
    );
    if (item !== undefined) return item;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("S10 manual review work never appeared");
}

/** Press Tab until the focused element carries the given data-testid. */
async function tabToTestId(page, testId, maxTabs = 80) {
  for (let attempt = 0; attempt < maxTabs; attempt += 1) {
    await page.keyboard.press("Tab");
    const reached = await page.evaluate((id) => {
      const active = document.activeElement;
      return active !== null && active.dataset?.testid === id;
    }, testId);
    if (reached) return;
  }
  throw new Error(`keyboard focus never reached ${testId}`);
}

/** Press Tab until the focused element carries the given aria-label. */
async function tabToName(page, name, maxTabs = 80) {
  for (let attempt = 0; attempt < maxTabs; attempt += 1) {
    await page.keyboard.press("Tab");
    const reached = await page.evaluate((label) => {
      const active = document.activeElement;
      return active !== null && active.getAttribute("aria-label") === label;
    }, name);
    if (reached) return;
  }
  throw new Error(`keyboard focus never reached ${name}`);
}

/** Press Tab until the focused anchor links to the given work id. */
async function tabToWorkLink(page, workId, maxTabs = 80) {
  const fragment = encodeURIComponent(workId);
  for (let attempt = 0; attempt < maxTabs; attempt += 1) {
    await page.keyboard.press("Tab");
    const reached = await page.evaluate((hrefFragment) => {
      const active = document.activeElement;
      return (
        active !== null &&
        active.tagName === "A" &&
        active.href.includes(hrefFragment)
      );
    }, fragment);
    if (reached) return;
  }
  throw new Error(`keyboard focus never reached ${workId}`);
}

/** Pick a native select option with the keyboard: focus is already on the
 * select, ArrowDown moves the highlight one option at a time (the first
 * option occupies index 0), and Tab commits the choice. */
async function selectOptionByKeyboard(page, locator, value) {
  const count = await locator.locator("option").count();
  let index = -1;
  for (let i = 0; i < count; i += 1) {
    const optionValue = await locator
      .locator("option")
      .nth(i)
      .getAttribute("value");
    if (optionValue === value) {
      index = i;
      break;
    }
  }
  expect(index).toBeGreaterThanOrEqual(0);
  for (let step = 0; step < index; step += 1) {
    await page.keyboard.press("ArrowDown");
  }
  await page.keyboard.press("Tab");
  await expect(locator).toHaveValue(value);
}

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
  await tabToWorkLink(reviewer, workId);
  await reviewer.keyboard.press("Enter");
  await expect(reviewer.getByTestId("review-panel")).toBeVisible();
  await tabToTestId(reviewer, "claim-button");
  await reviewer.keyboard.press("Enter");
  await expect(reviewer.getByTestId("review-command-status")).toContainText(
    "认领已接受",
  );
  await expect(reviewer.getByTestId("review-status")).toHaveText("claimed");
  return { workId, applicationId };
}

async function waitForApplicationWork(
  reviewer,
  server,
  applicationId,
  predecessorWorkId,
) {
  const deadline = Date.now() + 15_000;
  while (Date.now() < deadline) {
    const queue = await reviewer.request.get(
      `${server.baseURL}/controlled/s01/api/queries/queue`,
    );
    const item = (await queue.json()).items?.find(
      (candidate) =>
        candidate.application_id === applicationId &&
        candidate.work_item_id !== predecessorWorkId,
    );
    if (item !== undefined) return item;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("S10 successor manual work never appeared");
}

async function openAndClaimWork(reviewer, server, item) {
  await reviewer.goto(`${server.baseURL}${REACT_URL}`, {
    waitUntil: "networkidle",
  });
  await expect(reviewer.getByTestId("queue-panel")).toBeVisible();
  await tabToWorkLink(reviewer, item.work_item_id);
  await reviewer.keyboard.press("Enter");
  await expect(reviewer.getByTestId("review-panel")).toBeVisible();
  await tabToTestId(reviewer, "claim-button");
  await reviewer.keyboard.press("Enter");
  await expect(reviewer.getByTestId("review-command-status")).toContainText(
    "认领已接受",
  );
  await expect(reviewer.getByTestId("review-status")).toHaveText("claimed");
}

async function settleCleanup(cleanups) {
  const failures = [];
  for (const cleanup of cleanups) {
    try {
      await cleanup();
    } catch (error) {
      failures.push(error);
    }
  }
  if (failures.length > 0) throw failures[0];
}

/** Every pane, the form controls, and the history remain inside the active
 * viewport with no horizontal document overflow. */
async function expectContained(reviewer, testIds) {
  const result = { document: false };
  result.document = await reviewer.evaluate(
    () => document.documentElement.scrollWidth <= window.innerWidth,
  );
  for (const id of testIds) {
    result[id] = await reviewer.evaluate((testId) => {
      const element = document.querySelector(`[data-testid="${testId}"]`);
      if (element === null) return false;
      const box = element.getBoundingClientRect();
      return (
        element.scrollWidth <= element.clientWidth &&
        box.left >= 0 &&
        box.right <= window.innerWidth
      );
    }, id);
  }
  expect(result).toEqual({
    document: true,
    ...Object.fromEntries(testIds.map((id) => [id, true])),
  });
}

async function runS10Flow(browser, viewport) {
  const resources = {};
  let failure;
  try {
    resources.server = await startServer({
      TASK4_S01_TEST_BACKGROUND_ENABLED: "0",
    }, "tests.test_s10_http:create_s10_react_test_app");
    const server = resources.server;
    resources.reviewerContext = await browser.newContext({
      viewport: { width: viewport.width, height: viewport.height },
      extraHTTPHeaders: { Authorization: `Bearer ${DEMO_CREDENTIAL}` },
    });
    const reviewer = await resources.reviewerContext.newPage();
    const { workId, applicationId } = await openClaimedReviewPanel(
      reviewer,
      server,
    );

    // The visible ledger keeps every page, candidate claim and provenance.
    const ledger = reviewer.getByTestId("review-membership-ledger");
    await expect(ledger).toBeVisible();
    await expect(ledger).toContainText("/pages/0");
    await expect(ledger).toContainText("/pages/1");
    await expect(reviewer.getByTestId("review-membership-ledger-page")).toHaveCount(2);
    await expect(reviewer.getByTestId("review-membership-ledger-candidate")).toHaveCount(3);

    // The dual pane shows the coexisting candidate claims and ambiguous state.
    await expect(reviewer.getByTestId("review-membership")).toBeVisible();
    await expect(reviewer.getByTestId("review-membership-candidate").first()).toBeVisible();
    await expect(reviewer.getByTestId("review-membership-candidate-instance").first()).toBeVisible();
    await expect(reviewer.getByTestId("review-membership-candidate-provenance").first()).toContainText(
      "source_pointer=/pages/0",
    );
    await expect(reviewer.getByTestId("review-membership-candidate-provenance").first()).toContainText(
      "inferred=false",
    );
    expect(reviewer.viewportSize()).toEqual({
      width: viewport.width,
      height: viewport.height,
    });
    await expectContained(reviewer, [
      "review-membership-ledger",
      "review-membership",
    ]);

    // Read the authoritative route before the correction.
    const routeBefore = await (
      await reviewer.request.get(
        `${server.baseURL}/controlled/s01/api/queries/applications/${encodeURIComponent(applicationId)}/current-route`,
      )
    ).json();

    // Accept a claim whose public identifier contains the delimiter used by
    // the legacy UI.  The claim id remains byte-for-byte intact.
    await tabToTestId(reviewer, "review-membership-start");
    await reviewer.keyboard.press("Enter");
    await expect(reviewer.getByTestId("review-membership-form")).toBeVisible();
    expect(
      await reviewer.evaluate(() => {
        const form = document.querySelector('[data-testid="review-membership-form"]');
        const fits = (element) => {
          const box = element.getBoundingClientRect();
          return (
            element.scrollWidth <= element.clientWidth &&
            box.left >= 0 &&
            box.right <= window.innerWidth
          );
        };
        return {
          form: fits(form),
          controls: [...form.querySelectorAll("select, button")].every(fits),
        };
      }),
    ).toEqual({ form: true, controls: true });
    const candidateSelect = reviewer.getByRole("combobox", { name: "候选实例" });
    const reasonSelect = reviewer.getByRole("combobox", { name: "原因" });
    // The opened draft never preselects a candidate: the native select shows
    // an explicit disabled placeholder and submit stays disabled until the
    // Reviewer picks a claim with the keyboard.
    await expect(candidateSelect).toHaveValue("");
    await expect(candidateSelect.locator('option[value=""]')).toBeDisabled();
    await expect(reviewer.getByTestId("review-membership-submit")).toBeDisabled();
    await expect(reasonSelect.locator("option")).toHaveCount(3);
    await expect(
      reasonSelect.locator('option[value="MEMBERSHIP_PAGE_UNASSIGNED"]'),
    ).toHaveCount(0);
    await tabToTestId(reviewer, "review-membership-candidate-select");
    await selectOptionByKeyboard(reviewer, candidateSelect, "s10::claim_page1_b");
    await selectOptionByKeyboard(
      reviewer,
      reasonSelect,
      "MEMBERSHIP_SOURCE_VERIFIED",
    );
    await reviewer.keyboard.press("Enter");
    await expect(reviewer.getByTestId("review-command-status")).toContainText(
      "页归属已接受",
    );

    // Deterministic readiness: the accepted mutation invalidated the S01
    // reads and the replacement job is still queued.  React DOM carries the
    // pending Evidence revision and the invalidated route facts.
    await expect(reviewer.getByTestId("review-correction-pending")).toContainText(
      "证据修订 2",
    );
    await expect(reviewer.getByTestId("gate-phase")).toHaveText("Assembly");
    await expect(reviewer.getByTestId("gate-route")).toHaveText("pending_check");
    await expect(reviewer.getByTestId("gate-currentness")).toHaveText(
      "NO_CURRENT_RUN",
    );
    const firstWorker = await processNextJob(reviewer, server);
    // The explicit worker call creates the successor run; React DOM then
    // converges on the server-created run id and the fresh current route.
    await expect(reviewer.getByTestId("review-correction-converged")).toContainText(
      "证据修订 2",
    );
    await expect(
      reviewer.getByTestId("review-correction-converged"),
    ).toContainText(firstWorker.run_id);
    await expect(
      reviewer.getByTestId("review-correction-converged"),
    ).toContainText("manual_review");
    await expect(
      reviewer.getByTestId("review-history-run").filter({
        hasText: firstWorker.run_id,
      }),
    ).toContainText("当前");
    await expect(reviewer.getByTestId("gate-route")).toHaveText("manual_review");

    const firstHistory = await (
      await reviewer.request.get(
        `${server.baseURL}/controlled/s01/api/queries/applications/${encodeURIComponent(applicationId)}/history`,
      )
    ).json();
    const firstRouteAfter = await (
      await reviewer.request.get(
        `${server.baseURL}/controlled/s01/api/queries/applications/${encodeURIComponent(applicationId)}/current-route`,
      )
    ).json();

    // The old run stays immutable; the first correction becomes current only
    // through its fresh complete run.
    const firstRunIds = firstHistory.runs.map((run) => run.run_id);
    expect(firstRunIds).toContain(routeBefore.current_run_id);
    expect(firstRouteAfter.current_run_id).not.toBe(routeBefore.current_run_id);
    expect(firstRouteAfter.evidence_revision).toBe(2);
    expect(firstHistory.membership_history).toHaveLength(1);
    expect(firstHistory.memberships).toHaveLength(4);

    // The next work item exposes page 1 as selected history.  Append a later
    // page 1 decision from that ledger entry so the predecessor remains visible
    // as superseded.
    const successorWork = await waitForApplicationWork(
      reviewer,
      server,
      applicationId,
      workId,
    );
    await openAndClaimWork(reviewer, server, successorWork);
    const successorLedger = reviewer.getByTestId("review-membership-ledger");
    await expect(successorLedger).toContainText("selected");
    await expect(successorLedger).toContainText("active");
    await tabToName(reviewer, "选择附件 s10-attachment-1 第 1 页");
    await reviewer.keyboard.press("Enter");
    await expect(reviewer.getByTestId("review-membership")).toContainText("页 1");
    const successorCandidate = reviewer.getByRole("combobox", {
      name: "候选实例",
    });
    await tabToTestId(reviewer, "review-membership-candidate-select");
    await selectOptionByKeyboard(reviewer, successorCandidate, "s10_claim_page1_a");
    await selectOptionByKeyboard(
      reviewer,
      reviewer.getByRole("combobox", { name: "原因" }),
      "MEMBERSHIP_SOURCE_MISASSIGNED",
    );
    await reviewer.keyboard.press("Enter");
    await expect(reviewer.getByTestId("review-command-status")).toContainText(
      "页归属已接受",
    );

    // Deterministic readiness for the superseding correction: the accepted
    // mutation invalidated the reads and the successor job is still queued.
    await expect(reviewer.getByTestId("review-correction-pending")).toContainText(
      "证据修订 3",
    );
    await expect(reviewer.getByTestId("gate-phase")).toHaveText("Assembly");
    await expect(reviewer.getByTestId("gate-route")).toHaveText("pending_check");
    await expect(reviewer.getByTestId("gate-currentness")).toHaveText(
      "NO_CURRENT_RUN",
    );
    const supersedingWorker = await processNextJob(reviewer, server);
    await expect(reviewer.getByTestId("review-correction-converged")).toContainText(
      "证据修订 3",
    );
    await expect(
      reviewer.getByTestId("review-correction-converged"),
    ).toContainText(supersedingWorker.run_id);
    await expect(
      reviewer.getByTestId("review-correction-converged"),
    ).toContainText("manual_review");
    await expect(
      reviewer.getByTestId("review-history-run").filter({
        hasText: supersedingWorker.run_id,
      }),
    ).toContainText("当前");
    await expect(reviewer.getByTestId("gate-route")).toHaveText("manual_review");

    const supersededHistory = await (
      await reviewer.request.get(
        `${server.baseURL}/controlled/s01/api/queries/applications/${encodeURIComponent(applicationId)}/history`,
      )
    ).json();
    const page1Decisions = supersededHistory.memberships.filter(
      (record) =>
        record.record_kind === "accepted" &&
        record.page.attachment_id === "s10-attachment-1",
    );
    expect(page1Decisions).toHaveLength(2);
    const superseded = page1Decisions.find((record) => record.status === "superseded");
    const active = page1Decisions.find((record) => record.status === "active");
    expect(superseded).toBeDefined();
    expect(active.supersedes).toEqual([superseded.decision_id]);
    await expect(reviewer.getByTestId("review-history-memberships")).toContainText(
      "superseded",
    );

    // Page 2 remains unresolved in the next cycle.  Resolve it through an
    // explicit unassign, preserving both page 1 decisions.
    const finalWork = await waitForApplicationWork(
      reviewer,
      server,
      applicationId,
      successorWork.work_item_id,
    );
    await openAndClaimWork(reviewer, server, finalWork);
    await tabToName(reviewer, "选择附件 s10-attachment-2 第 2 页");
    await reviewer.keyboard.press("Enter");
    await expect(reviewer.getByTestId("review-membership")).toContainText("页 2");
    // Only the checked radio is tabbable; the Arrow key moves the selection
    // to the unassign radio within the group.
    await tabToTestId(reviewer, "review-membership-accept-radio");
    await reviewer.keyboard.press("ArrowDown");
    await expect(
      reviewer.getByTestId("review-membership-unassign-radio"),
    ).toBeChecked();
    const unassignReason = reviewer.getByRole("combobox", { name: "原因" });
    await expect(unassignReason.locator("option")).toHaveCount(3);
    await expect(
      unassignReason.locator('option[value="MEMBERSHIP_INSTANCE_WRONG"]'),
    ).toHaveCount(0);
    // The unassign variant also binds an explicit source claim: the Reviewer
    // picks the page-2 candidate with the keyboard before submit enables.
    const page2CandidateValue = await candidateSelect
      .locator('option:not([value=""])')
      .first()
      .getAttribute("value");
    expect(page2CandidateValue).toBeTruthy();
    await tabToTestId(reviewer, "review-membership-candidate-select");
    await selectOptionByKeyboard(
      reviewer,
      candidateSelect,
      page2CandidateValue,
    );
    await selectOptionByKeyboard(
      reviewer,
      unassignReason,
      "MEMBERSHIP_PAGE_UNASSIGNED",
    );
    await reviewer.keyboard.press("Enter");
    await expect(reviewer.getByTestId("review-command-status")).toContainText(
      "页归属已接受",
    );

    // Final deterministic readiness: the page-2 unassign advanced Evidence
    // to revision 4 while the successor job stays queued.
    await expect(reviewer.getByTestId("review-correction-pending")).toContainText(
      "证据修订 4",
    );
    await expect(reviewer.getByTestId("gate-phase")).toHaveText("Assembly");
    await expect(reviewer.getByTestId("gate-route")).toHaveText("pending_check");
    await expect(reviewer.getByTestId("gate-currentness")).toHaveText(
      "NO_CURRENT_RUN",
    );
    const finalWorker = await processNextJob(reviewer, server);
    await expect(reviewer.getByTestId("review-correction-converged")).toContainText(
      "证据修订 4",
    );
    await expect(
      reviewer.getByTestId("review-correction-converged"),
    ).toContainText(finalWorker.run_id);
    await expect(
      reviewer.getByTestId("review-correction-converged"),
    ).toContainText("auto_complete");
    await expect(
      reviewer.getByTestId("review-history-run").filter({
        hasText: finalWorker.run_id,
      }),
    ).toContainText("当前");
    await expect(reviewer.getByTestId("gate-route")).toHaveText("auto_complete");

    const history = await (
      await reviewer.request.get(
        `${server.baseURL}/controlled/s01/api/queries/applications/${encodeURIComponent(applicationId)}/history`,
      )
    ).json();
    const routeAfter = await (
      await reviewer.request.get(
        `${server.baseURL}/controlled/s01/api/queries/applications/${encodeURIComponent(applicationId)}/current-route`,
      )
    ).json();
    const runIds = history.runs.map((run) => run.run_id);
    expect(runIds).toContain(routeBefore.current_run_id);
    expect(runIds).toContain(firstRouteAfter.current_run_id);
    expect(routeAfter.current_run_id).not.toBe(firstRouteAfter.current_run_id);
    expect(routeAfter.evidence_revision).toBe(4);
    expect(routeAfter.route).toBe("auto_complete");
    expect(history.membership_history).toHaveLength(3);
    expect(history.memberships).toHaveLength(6);
    const decisions = history.memberships.filter(
      (record) =>
        record.record_kind === "accepted" || record.record_kind === "unassigned",
    );
    expect(decisions).toHaveLength(3);
    expect(decisions.filter((record) => record.status === "superseded")).toHaveLength(1);
    expect(decisions.filter((record) => record.status === "active")).toHaveLength(2);
    expect(decisions.some((record) => record.record_kind === "unassigned")).toBe(true);
    await expect(reviewer.getByTestId("review-history-memberships")).toContainText(
      "unassigned",
    );
    await expect(
      reviewer.getByTestId("review-history-membership-corrections"),
    ).toContainText("MEMBERSHIP_PAGE_UNASSIGNED");
    expect(reviewer.viewportSize()).toEqual({
      width: viewport.width,
      height: viewport.height,
    });
    await expectContained(reviewer, [
      "review-history-memberships",
      "review-history-membership-corrections",
    ]);
  } catch (error) {
    failure = error;
    throw error;
  } finally {
    try {
      await settleCleanup([
        resources.reviewerContext
          ? () => resources.reviewerContext.close()
          : () => Promise.resolve(),
        resources.server ? () => stopServer(resources.server) : () => Promise.resolve(),
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

for (const viewport of VIEWPORTS) {
  test(
    `S10 membership dual-pane correction reruns via keyboard at ${viewport.label}`,
    async ({ browser }) => {
      await runS10Flow(browser, viewport);
    },
  );
}

module.exports.__startServerForDebug = startServer;
