const { test, expect } = require("@playwright/test");
const { spawn } = require("child_process");
const { once } = require("events");
const fs = require("fs");
const net = require("net");
const os = require("os");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const PYTHON = process.env.PYTHON || path.join(ROOT, ".venv", "bin", "python");
const REVIEWER_CREDENTIAL = "s01-registered-demo-test-credential";
const APPROVER_CREDENTIAL = "s05-browser-approver-credential";
const OPERATOR_CREDENTIAL = "s01-registered-operator-test-credential";

async function reservePort() {
  const server = net.createServer();
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const port = server.address().port;
  await new Promise((resolve, reject) => server.close((error) => (error ? reject(error) : resolve())));
  return port;
}

async function startServer() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "xiaopeng-s05-browser-"));
  const statePath = path.join(root, "target.sqlite3");
  const clockPath = path.join(root, "clock.txt");
  fs.writeFileSync(clockPath, "1000", "ascii");
  const port = await reservePort();
  const output = [];
  const child = spawn(
    PYTHON,
    [
      "-m",
      "uvicorn",
      "tests.test_s05_http:create_s05_clock_test_app",
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
        PYTHONPYCACHEPREFIX: "/tmp/xiaopeng-task4-s05-browser-pycache",
        TASK4_S01_STATE_PATH: statePath,
        TASK4_S01_TEST_STATE_PATH: statePath,
        TASK4_S01_TEST_SCENARIO_ID: "app_bad_brand.json",
        TASK4_S01_DEMO_CREDENTIAL: REVIEWER_CREDENTIAL,
        TASK4_S01_DEMO_SUBJECT: "s05-browser-reviewer",
        TASK4_S01_OPERATOR_CREDENTIAL: OPERATOR_CREDENTIAL,
        TASK4_S01_OPERATOR_SUBJECT: "s05-browser-router",
        TASK4_S05_EXCEPTION_APPROVER_CREDENTIAL: APPROVER_CREDENTIAL,
        TASK4_S05_EXCEPTION_APPROVER_SUBJECT: "s05-browser-approver",
        TASK4_S05_TEST_CLOCK_PATH: clockPath,
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  child.stdout.on("data", (chunk) => output.push(chunk.toString()));
  child.stderr.on("data", (chunk) => output.push(chunk.toString()));
  const baseURL = `http://127.0.0.1:${port}`;
  const deadline = Date.now() + 8_000;
  while (Date.now() < deadline && child.exitCode === null) {
    try {
      if ((await fetch(`${baseURL}/api/health`)).ok) {
        return { baseURL, child, clockPath, output };
      }
    } catch (_) {}
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  child.kill("SIGKILL");
  throw new Error(`S05 browser server did not start: ${output.join("")}`);
}

async function stopServer(server) {
  if (server.child.exitCode !== null) return;
  server.child.kill("SIGTERM");
  const exited = once(server.child, "exit");
  const timeout = new Promise((resolve) => setTimeout(resolve, 5_000, "timeout"));
  if ((await Promise.race([exited, timeout])) === "timeout") {
    server.child.kill("SIGKILL");
    await once(server.child, "exit");
  }
}

async function browserApi(page, method, url, body) {
  return page.evaluate(
    async ({ requestMethod, requestUrl, requestBody }) => {
      const response = await fetch(requestUrl, {
        method: requestMethod,
        credentials: "same-origin",
        cache: "no-store",
        headers: requestBody
          ? { "Content-Type": "application/json", Accept: "application/json" }
          : { Accept: "application/json" },
        body: requestBody ? JSON.stringify(requestBody) : undefined,
      });
      return { status: response.status, body: await response.json() };
    },
    { requestMethod: method, requestUrl: url, requestBody: body },
  );
}

async function operatorApi(server, method, pathName, body) {
  const response = await fetch(`${server.baseURL}${pathName}`, {
    method,
    headers: {
      Authorization: `Bearer ${OPERATOR_CREDENTIAL}`,
      "Content-Type": "application/json",
      Accept: "application/json",
    },
    body: JSON.stringify(body),
  });
  return { status: response.status, body: await response.json() };
}

async function requestException(reviewer, key) {
  const admitted = await browserApi(
    reviewer,
    "POST",
    "/controlled/s01/api/commands/submit",
    { scenario_id: "app_bad_brand.json", idempotency_key: `${key}-intake` },
  );
  expect(admitted.status).toBe(200);
  expect(admitted.body.disposition).toBe("accepted");
  let item;
  await expect
    .poll(async () => {
      const queue = await browserApi(reviewer, "GET", "/controlled/s01/api/queries/queue");
      item = queue.body.items.find(
        (candidate) => candidate.application_id === admitted.body.application_id,
      );
      return Boolean(item);
    })
    .toBe(true);
  const work = await browserApi(
    reviewer,
    "GET",
    `/controlled/s01/api/queries/review-work-items/${item.work_item_id}`,
  );
  const finding = work.body.automatic_findings.find(
    (candidate) => candidate.rule_id === "R_BRAND_CROSS",
  );
  const claim = await browserApi(
    reviewer,
    "POST",
    `/controlled/s01/api/commands/review-work-items/${item.work_item_id}/claim`,
    { expected_context: work.body.command_context },
  );
  const requested = await browserApi(
    reviewer,
    "POST",
    `/controlled/s01/api/commands/review-work-items/${item.work_item_id}/business-exceptions`,
    {
      finding_id: finding.finding_id,
      reason_code: "DOCUMENTED_BRAND_VARIANCE",
      expected_fence: claim.body.claim_fence,
      expected_context: work.body.command_context,
      idempotency_key: `${key}-request`,
    },
  );
  expect(requested.status).toBe(200);
  return { applicationId: admitted.body.application_id, finding, request: requested.body };
}

async function claimException(approver, request) {
  const view = await browserApi(
    approver,
    "GET",
    `/controlled/s01/api/queries/business-exceptions/${request.request_id}`,
  );
  const claim = await browserApi(
    approver,
    "POST",
    `/controlled/s01/api/commands/exception-work-items/${request.work_item_id}/claim`,
    { expected_context: view.body.command_context },
  );
  expect(view.status).toBe(200);
  expect(claim.status).toBe(200);
  return { view: view.body, claim: claim.body };
}

test("independent reviewer and approver browser identities approve, reject, and expire", async ({
  browser,
}) => {
  const server = await startServer();
  const reviewerContext = await browser.newContext({
    extraHTTPHeaders: { Authorization: `Bearer ${REVIEWER_CREDENTIAL}` },
  });
  const rejectReviewerContext = await browser.newContext({
    extraHTTPHeaders: { Authorization: `Bearer ${REVIEWER_CREDENTIAL}` },
  });
  const expiryReviewerContext = await browser.newContext({
    extraHTTPHeaders: { Authorization: `Bearer ${REVIEWER_CREDENTIAL}` },
  });
  const approverContext = await browser.newContext({
    extraHTTPHeaders: { Authorization: `Bearer ${APPROVER_CREDENTIAL}` },
  });
  const reviewer = await reviewerContext.newPage();
  const rejectReviewer = await rejectReviewerContext.newPage();
  const expiryReviewer = await expiryReviewerContext.newPage();
  const approver = await approverContext.newPage();
  try {
    expect((await reviewer.goto(`${server.baseURL}/controlled/s01`)).status()).toBe(200);
    expect((await rejectReviewer.goto(`${server.baseURL}/controlled/s01`)).status()).toBe(200);
    expect((await expiryReviewer.goto(`${server.baseURL}/controlled/s01`)).status()).toBe(200);
    expect((await approver.goto(`${server.baseURL}/api/health`)).status()).toBe(200);
    expect((await reviewerContext.cookies()).some((cookie) => cookie.name === "s01_session")).toBe(
      true,
    );
    expect((await approverContext.cookies()).some((cookie) => cookie.name === "s01_session")).toBe(
      false,
    );

    const approved = await requestException(reviewer, "s05-browser-approve");
    const approval = await claimException(approver, approved.request);
    expect(approval.view.actions).toEqual(["claim"]);
    expect(approval.view.finding.verdict).toBe("inconsistent");
    expect(approval.view.scope).toBe("one_application_cycle_run_finding");
    for (const secret of ["LSVAA4182N3000004", "ENG555555", "330106199203034560", "丰田"]) {
      expect(JSON.stringify(approval.view)).not.toContain(secret);
    }
    const decision = await browserApi(
      approver,
      "POST",
      `/controlled/s01/api/commands/business-exceptions/${approved.request.request_id}/decide`,
      {
        work_item_id: approved.request.work_item_id,
        decision: "approved",
        reason_code: "DOCUMENTED_VARIANCE_ACCEPTED",
        expected_fence: approval.claim.claim_fence,
        expected_context: approval.view.command_context,
        idempotency_key: "s05-browser-approve-decision",
      },
    );
    expect(decision.status).toBe(200);
    const routed = await operatorApi(
      server,
      "POST",
      `/controlled/s01/api/commands/business-exceptions/${approved.request.request_id}/route`,
      {
        expected_context: decision.body.routing_context,
        idempotency_key: "s05-browser-route",
      },
    );
    expect(routed.status).toBe(200);
    expect(routed.body.completion_basis).toBe("business_exception");

    const rejected = await requestException(rejectReviewer, "s05-browser-reject");
    const rejection = await claimException(approver, rejected.request);
    const rejectedDecision = await browserApi(
      approver,
      "POST",
      `/controlled/s01/api/commands/business-exceptions/${rejected.request.request_id}/decide`,
      {
        work_item_id: rejected.request.work_item_id,
        decision: "rejected",
        reason_code: "DOCUMENTED_VARIANCE_REJECTED",
        expected_fence: rejection.claim.claim_fence,
        expected_context: rejection.view.command_context,
        idempotency_key: "s05-browser-reject-decision",
      },
    );
    expect(rejectedDecision.status).toBe(200);
    expect(rejectedDecision.body.phase).toBe("Manual Review");

    const expiring = await requestException(expiryReviewer, "s05-browser-expire");
    const expiry = await claimException(approver, expiring.request);
    fs.writeFileSync(server.clockPath, String(expiring.request.expires_at), "ascii");
    const expired = await operatorApi(
      server,
      "POST",
      `/controlled/s01/api/commands/business-exceptions/${expiring.request.request_id}/expire`,
      {
        expected_context: expiry.view.command_context,
        idempotency_key: "s05-browser-expire-command",
      },
    );
    expect(expired.status).toBe(200);
    expect(expired.body.phase).toBe("Manual Review");

    await reviewer.reload({ waitUntil: "networkidle" });
    const route = await browserApi(
      reviewer,
      "GET",
      `/controlled/s01/api/queries/applications/${approved.applicationId}/current-route`,
    );
    const history = await browserApi(
      reviewer,
      "GET",
      `/controlled/s01/api/queries/applications/${approved.applicationId}/history`,
    );
    expect(route.body.route).toBe("human_complete");
    expect(history.body.runs[0].exception_ids).toContain(approved.request.request_id);
  } finally {
    await reviewerContext.close();
    await rejectReviewerContext.close();
    await expiryReviewerContext.close();
    await approverContext.close();
    await stopServer(server);
  }
});
