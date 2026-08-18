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
    const { applicationId } = await openClaimedReviewPanel(
      reviewer,
      server,
    );

    // The dual pane shows the coexisting candidate claims, provenance and the
    // ambiguous state without any silent selection.
    await expect(reviewer.getByTestId("review-membership")).toBeVisible();
    await expect(reviewer.getByTestId("review-membership-candidate").first()).toBeVisible();
    await expect(reviewer.getByTestId("review-membership-candidate-instance").first()).toBeVisible();

    // Read the authoritative route before the correction.
    const routeBefore = await (
      await reviewer.request.get(
        `${server.baseURL}/controlled/s01/api/queries/applications/${encodeURIComponent(applicationId)}/current-route`,
      )
    ).json();

    // Accept the first candidate of the ambiguous page with a registered reason.
    await reviewer.getByTestId("review-membership-start").click();
    await expect(reviewer.getByTestId("review-membership-form")).toBeVisible();
    await reviewer.getByTestId("review-membership-candidate-select").selectOption(
      "reg_cert_instance_a::机动车登记证书",
    );
    await reviewer.getByTestId("review-membership-reason").selectOption(
      "MEMBERSHIP_SOURCE_VERIFIED",
    );
    await reviewer.getByTestId("review-membership-submit").click();

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

    // The old run stays immutable in history; route changes only via a fresh
    // complete run that won current-run CAS.
    const runIds = history.runs.map((run) => run.run_id);
    expect(runIds).toContain(routeBefore.current_run_id);
    expect(routeAfter.current_run_id).not.toBe(routeBefore.current_run_id);
    expect(routeAfter.evidence_revision).toBe(2);
    expect(history.membership_history.length).toBe(1);
    expect(history.memberships.length).toBeGreaterThanOrEqual(8);
    // The accepted decision and its superseded predecessors are preserved.
    const decisions = history.memberships.filter(
      (record) =>
        record.record_kind === "accepted" || record.record_kind === "unassigned",
    );
    expect(decisions.some((record) => record.status === "active")).toBe(true);
    expect(decisions.some((record) => record.status === "superseded")).toBe(true);
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
