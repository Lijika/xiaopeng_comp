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

async function startServer(extraEnv = {}) {
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
      "task4_consistency.web.app:create_s02_test_app",
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

async function installManualWork(baseURL, reviewer) {
  const admission = await reviewer.request.post(
    `${baseURL}/controlled/s01/api/commands/submit`,
    { data: { scenario_id: SCENARIO, idempotency_key: "s10-react-admission" } },
  );
  expect(admission.ok()).toBeTruthy();
  const accepted = await admission.json();
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
  await reviewer.getByRole("button", { name: "认领" }).click();
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
  await reviewer.getByRole("link", { name: new RegExp(item.work_item_id) }).click();
  await expect(reviewer.getByTestId("review-panel")).toBeVisible();
  await reviewer.getByRole("button", { name: "认领" }).click();
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

test("S10 membership dual-pane correction reruns and changes only via a fresh current run", async ({
  browser,
}) => {
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

    // Read the authoritative route before the correction.
    const routeBefore = await (
      await reviewer.request.get(
        `${server.baseURL}/controlled/s01/api/queries/applications/${encodeURIComponent(applicationId)}/current-route`,
      )
    ).json();

    // Accept a claim whose public identifier contains the delimiter used by
    // the legacy UI.  The claim id remains byte-for-byte intact.
    await reviewer.getByTestId("review-membership-start").click();
    await expect(reviewer.getByTestId("review-membership-form")).toBeVisible();
    const candidateSelect = reviewer.getByRole("combobox", { name: "候选实例" });
    const reasonSelect = reviewer.getByRole("combobox", { name: "原因" });
    await expect(reasonSelect.locator("option")).toHaveCount(3);
    await expect(
      reasonSelect.locator('option[value="MEMBERSHIP_PAGE_UNASSIGNED"]'),
    ).toHaveCount(0);
    await candidateSelect.selectOption("s10::claim_page1_b");
    await expect(candidateSelect).toHaveValue("s10::claim_page1_b");
    await reasonSelect.selectOption(
      "MEMBERSHIP_SOURCE_VERIFIED",
    );
    await reviewer.getByTestId("review-membership-submit").click();
    await expect(reviewer.getByTestId("review-command-status")).toContainText(
      "页归属已接受",
    );

    // Acceptance advances Evidence; the successor run converges through
    // current-route/history and only then replaces the current run.
    await expect
      .poll(
        async () => {
          const history = await (
            await reviewer.request.get(
              `${server.baseURL}/controlled/s01/api/queries/applications/${encodeURIComponent(applicationId)}/history`,
            )
          ).json();
          return history.runs ? history.runs.length : 0;
        },
        { timeout: 15_000, message: "successor run did not complete" },
      )
      .toBeGreaterThanOrEqual(2);

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
    await reviewer
      .getByRole("button", {
        name: "选择附件 s10-attachment-1 第 1 页",
      })
      .click();
    await expect(reviewer.getByTestId("review-membership")).toContainText("页 1");
    const successorCandidate = reviewer.getByRole("combobox", {
      name: "候选实例",
    });
    await successorCandidate.selectOption("s10_claim_page1_a");
    await reviewer
      .getByRole("combobox", { name: "原因" })
      .selectOption("MEMBERSHIP_SOURCE_MISASSIGNED");
    await reviewer.getByTestId("review-membership-submit").click();
    await expect(reviewer.getByTestId("review-command-status")).toContainText(
      "页归属已接受",
    );

    await expect
      .poll(
        async () => {
          const history = await (
            await reviewer.request.get(
              `${server.baseURL}/controlled/s01/api/queries/applications/${encodeURIComponent(applicationId)}/history`,
            )
          ).json();
          return history.runs ? history.runs.length : 0;
        },
        { timeout: 15_000, message: "superseding run did not complete" },
      )
      .toBeGreaterThanOrEqual(3);

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
    await reviewer
      .getByRole("button", {
        name: "选择附件 s10-attachment-2 第 2 页",
      })
      .click();
    await expect(reviewer.getByTestId("review-membership")).toContainText("页 2");
    await reviewer.getByTestId("review-membership-unassign-radio").click();
    const unassignReason = reviewer.getByRole("combobox", { name: "原因" });
    await expect(unassignReason.locator("option")).toHaveCount(3);
    await expect(
      unassignReason.locator('option[value="MEMBERSHIP_INSTANCE_WRONG"]'),
    ).toHaveCount(0);
    await unassignReason.selectOption("MEMBERSHIP_PAGE_UNASSIGNED");
    await reviewer.getByTestId("review-membership-submit").click();
    await expect(reviewer.getByTestId("review-command-status")).toContainText(
      "页归属已接受",
    );

    await expect
      .poll(
        async () => {
          const history = await (
            await reviewer.request.get(
              `${server.baseURL}/controlled/s01/api/queries/applications/${encodeURIComponent(applicationId)}/history`,
            )
          ).json();
          return history.runs ? history.runs.length : 0;
        },
        { timeout: 15_000, message: "final successor run did not complete" },
      )
      .toBeGreaterThanOrEqual(4);

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
});

module.exports.__startServerForDebug = startServer;
