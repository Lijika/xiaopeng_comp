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
  // Start at a real epoch so the React expiry gating (browser Date.now())
  // agrees with the server clock; the existing expiry assertions only rely
  // on the +900s waiver TTL relative to this start.
  fs.writeFileSync(clockPath, String(Math.floor(Date.now() / 1000)), "ascii");
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

async function uiPrepareApplication(reviewer, key) {
  const admitted = await browserApi(reviewer, "POST", "/controlled/s01/api/commands/submit", {
    scenario_id: "app_bad_brand.json",
    idempotency_key: `${key}-intake`,
  });
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
  return { applicationId: admitted.body.application_id, workItemId: item.work_item_id };
}

/** The Reviewer drives the request through the production React build: open
 * the review shell, claim, and post only when the server eligibility DTO
 * says eligible.  Returns the server-issued request id. */
async function uiRequestException(reviewer, server, applicationId, workItemId) {
  await reviewer.goto(`${server.baseURL}/controlled/s01/react?review=${encodeURIComponent(workItemId)}`);
  await expect(reviewer.getByTestId("review-panel")).toBeVisible({ timeout: 10_000 });
  await expect(reviewer.getByTestId("claim-button")).toBeEnabled({ timeout: 10_000 });
  await reviewer.getByTestId("claim-button").click();
  await expect(reviewer.getByTestId("exception-request-button")).toBeEnabled({ timeout: 10_000 });
  await expect(reviewer.getByTestId("review-workspace-verdict")).toHaveText("inconsistent");
  await reviewer.getByTestId("exception-request-button").click();
  await expect(reviewer.getByTestId("exception-request-accepted")).toBeVisible({ timeout: 10_000 });
  const requestId = (await reviewer.getByTestId("exception-request-id").textContent()).trim();
  expect(requestId.length).toBeGreaterThan(0);
  await expect(reviewer.getByTestId("exception-route")).toHaveText("pending_exception_approval");
  return requestId;
}

test("UI-driven request to independent approve with operator routing and reviewer refetch", async ({
  browser,
}) => {
  test.setTimeout(120_000);
  const server = await startServer();
  const reviewerContext = await browser.newContext({
    extraHTTPHeaders: { Authorization: `Bearer ${REVIEWER_CREDENTIAL}` },
  });
  const approverContext = await browser.newContext({
    extraHTTPHeaders: { Authorization: `Bearer ${APPROVER_CREDENTIAL}` },
  });
  const reviewer = await reviewerContext.newPage();
  const approver = await approverContext.newPage();
  try {
    await reviewer.goto(`${server.baseURL}/controlled/s01`);
    const { applicationId, workItemId } = await uiPrepareApplication(reviewer, "s05-ui-approve");
    const requestId = await uiRequestException(reviewer, server, applicationId, workItemId);

    await approver.goto(`${server.baseURL}/controlled/s05/react?request=${encodeURIComponent(requestId)}`);
    await expect(approver.getByTestId("approver-view")).toBeVisible({ timeout: 10_000 });
    await expect(approver.getByTestId("approver-verdict")).toHaveText("inconsistent");
    await expect(approver.getByTestId("approver-finding-rule")).toHaveText("R_BRAND_CROSS");
    const approverBody = await approver.evaluate(() => document.body.textContent ?? "");
    for (const secret of ["LSVAA4182N3000004", "ENG555555", "330106199203034560", "丰田"]) {
      expect(approverBody).not.toContain(secret);
    }
    let routingContext = null;
    approver.on("response", (response) => {
      if (response.url().includes("/decide") && response.status() === 200) {
        response
          .json()
          .then((body) => {
            if (body && body.routing_context) routingContext = body.routing_context;
          })
          .catch(() => {});
      }
    });
    await approver.getByTestId("approver-claim-button").click();
    await expect(approver.getByTestId("approver-approve-button")).toBeEnabled({ timeout: 10_000 });
    await approver.getByTestId("approver-approve-button").click();
    await expect(approver.getByTestId("approver-status")).toHaveText("approved", { timeout: 10_000 });
    await expect.poll(() => routingContext).not.toBeNull();

    const routed = await operatorApi(
      server,
      "POST",
      `/controlled/s01/api/commands/business-exceptions/${requestId}/route`,
      { expected_context: routingContext, idempotency_key: "s05-ui-route" },
    );
    expect(routed.status).toBe(200);
    expect(routed.body.completion_basis).toBe("business_exception");

    // The Reviewer refetches inside the same mounted page through the
    // authoritative reload action of the accepted-exception block; the
    // current-route and history reads reconverge on the server-owned
    // completion while the machine verdict stays inconsistent.
    await expect(reviewer.getByTestId("exception-reload-button")).toBeEnabled({
      timeout: 10_000,
    });
    await reviewer.getByTestId("exception-reload-button").click();
    await expect(reviewer.getByTestId("exception-route")).toHaveText("human_complete", {
      timeout: 10_000,
    });
    await expect(reviewer.getByTestId("review-history-exceptions")).toContainText(
      requestId,
      { timeout: 10_000 },
    );
    await expect(reviewer.getByTestId("review-history-exceptions")).toContainText(
      "inconsistent",
    );
    await expect(reviewer.getByTestId("review-history-exceptions")).toContainText(
      "human_complete",
    );
    const reviewerBody = await reviewer.evaluate(() => document.body.textContent ?? "");
    for (const secret of ["LSVAA4182N3000004", "ENG555555", "330106199203034560", "丰田"]) {
      expect(reviewerBody).not.toContain(secret);
    }
    const decides = await approver.evaluate(
      (needle) =>
        performance
          .getEntriesByType("resource")
          .filter((entry) => entry.name.includes(needle)).length,
      "/decide",
    );
    expect(decides).toBe(1);
  } finally {
    await reviewerContext.close();
    await approverContext.close();
    await stopServer(server);
  }
});

test("UI-driven reject, expiry, claim race, operations recovery, accessibility and viewports", async ({
  browser,
}) => {
  test.setTimeout(180_000);
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
  const recoveryReviewerContext = await browser.newContext({
    extraHTTPHeaders: { Authorization: `Bearer ${REVIEWER_CREDENTIAL}` },
  });
  const reopenedReviewerContext = await browser.newContext({
    extraHTTPHeaders: { Authorization: `Bearer ${REVIEWER_CREDENTIAL}` },
  });
  const approverContext = await browser.newContext({
    extraHTTPHeaders: { Authorization: `Bearer ${APPROVER_CREDENTIAL}` },
  });
  const racerContext = await browser.newContext({
    extraHTTPHeaders: { Authorization: `Bearer ${APPROVER_CREDENTIAL}` },
  });
  const reviewer = await reviewerContext.newPage();
  const rejectReviewer = await rejectReviewerContext.newPage();
  const expiryReviewer = await expiryReviewerContext.newPage();
  const recoveryReviewer = await recoveryReviewerContext.newPage();
  const reopenedReviewer = await reopenedReviewerContext.newPage();
  const approver = await approverContext.newPage();
  const racer = await racerContext.newPage();
  try {
    await reviewer.goto(`${server.baseURL}/controlled/s01`);
    await rejectReviewer.goto(`${server.baseURL}/controlled/s01`);
    await expiryReviewer.goto(`${server.baseURL}/controlled/s01`);
    await recoveryReviewer.goto(`${server.baseURL}/controlled/s01`);
    await reopenedReviewer.goto(`${server.baseURL}/controlled/s01`);

    // --- reject leg with a live-claim race observed by a second approver ---
    const rejectedPrep = await uiPrepareApplication(rejectReviewer, "s05-ui-reject");
    const rejectedRequestId = await uiRequestException(
      rejectReviewer,
      server,
      rejectedPrep.applicationId,
      rejectedPrep.workItemId,
    );
    await approver.goto(`${server.baseURL}/controlled/s05/react?request=${encodeURIComponent(rejectedRequestId)}`);
    await expect(approver.getByTestId("approver-claim-button")).toBeEnabled({ timeout: 10_000 });
    await approver.getByTestId("approver-claim-button").focus();
    await approver.keyboard.press("Enter");
    await expect(approver.getByTestId("approver-reject-button")).toBeEnabled({ timeout: 10_000 });
    // The racer sees the live claim: server claim facts and no claim control.
    await racer.goto(`${server.baseURL}/controlled/s05/react?request=${encodeURIComponent(rejectedRequestId)}`);
    await expect(racer.getByTestId("approver-view")).toBeVisible({ timeout: 10_000 });
    await expect(racer.getByTestId("approver-claim-status")).toHaveText("claimed");
    await expect(racer.getByTestId("approver-claim-fence")).toHaveText("1");
    await expect(racer.locator('[data-testid="approver-claim-button"]')).toHaveCount(0);
    await approver.getByTestId("approver-reject-button").click();
    await expect(approver.getByTestId("approver-status")).toHaveText("rejected", { timeout: 10_000 });
    // The successor returns to Manual Review; the fresh work item projects the
    // same-run re-request as not material.
    let successor;
    await expect
      .poll(async () => {
        const queue = await browserApi(rejectReviewer, "GET", "/controlled/s01/api/queries/queue");
        successor = queue.body.items.find(
          (candidate) => candidate.application_id === rejectedPrep.applicationId,
        );
        return Boolean(successor);
      })
      .toBe(true);
    await rejectReviewer.goto(`${server.baseURL}/controlled/s01/react?review=${encodeURIComponent(successor.work_item_id)}`);
    await expect(rejectReviewer.getByTestId("claim-button")).toBeEnabled({ timeout: 10_000 });
    await rejectReviewer.getByTestId("claim-button").click();
    await expect(rejectReviewer.getByTestId("exception-ineligible")).toContainText(
      "EXCEPTION_REREQUEST_NOT_MATERIAL",
      { timeout: 10_000 },
    );

    // --- expiry leg: a claimed request expires at the trusted server clock ---
    const expiryPrep = await uiPrepareApplication(expiryReviewer, "s05-ui-expire");
    const expiringRequestId = await uiRequestException(
      expiryReviewer,
      server,
      expiryPrep.applicationId,
      expiryPrep.workItemId,
    );
    await approver.goto(`${server.baseURL}/controlled/s05/react?request=${encodeURIComponent(expiringRequestId)}`);
    await expect(approver.getByTestId("approver-view")).toBeVisible({ timeout: 10_000 });
    await approver.getByTestId("approver-claim-button").click();
    await expect(approver.getByTestId("approver-approve-button")).toBeEnabled({ timeout: 10_000 });
    const expiresAt = Number(
      (await approver.getByTestId("approver-expiry").textContent()).trim(),
    );
    const viewBody = await browserApi(
      approver,
      "GET",
      `/controlled/s01/api/queries/business-exceptions/${expiringRequestId}`,
    );
    expect(viewBody.status).toBe(200);
    fs.writeFileSync(server.clockPath, String(expiresAt), "ascii");
    const expired = await operatorApi(
      server,
      "POST",
      `/controlled/s01/api/commands/business-exceptions/${expiringRequestId}/expire`,
      { expected_context: viewBody.body.command_context, idempotency_key: "s05-ui-expire-command" },
    );
    expect(expired.status).toBe(200);
    expect(expired.body.phase).toBe("Manual Review");
    // The Reviewer refetches in-page through the authoritative reload action
    // and observes the expiry on the server-owned route/history reads.
    await expect(expiryReviewer.getByTestId("exception-reload-button")).toBeEnabled({
      timeout: 10_000,
    });
    await expiryReviewer.getByTestId("exception-reload-button").click();
    await expect(expiryReviewer.getByTestId("exception-route")).toHaveText("manual_review", {
      timeout: 10_000,
    });
    await expect(expiryReviewer.getByTestId("review-history-exceptions")).toContainText(
      expiringRequestId,
      { timeout: 10_000 },
    );
    await expect(expiryReviewer.getByTestId("review-history-exceptions")).toContainText(
      "expired",
    );
    // The stale request view is server data: expired, non-current, no actions.
    await approver.reload();
    await expect(approver.getByTestId("approver-status")).toHaveText("expired", { timeout: 10_000 });
    await expect(approver.getByTestId("approver-currentness")).toHaveText("EXPIRED");
    await expect(approver.locator('[data-testid="approver-claim-button"]')).toHaveCount(0);

    // --- operations recovery: close blocks the claim, resume reopens it ---
    const recoveryPrep = await uiPrepareApplication(recoveryReviewer, "s05-ui-recovery");
    const recoveryRequestId = await uiRequestException(
      recoveryReviewer,
      server,
      recoveryPrep.applicationId,
      recoveryPrep.workItemId,
    );
    const closed = await operatorApi(
      server,
      "POST",
      "/controlled/s01/api/commands/business-exception-operations/close",
      { idempotency_key: "s05-ui-close" },
    );
    expect(closed.status).toBe(200);
    expect(closed.body.operations).toBe("closed");
    await approver.goto(`${server.baseURL}/controlled/s05/react?request=${encodeURIComponent(recoveryRequestId)}`);
    await expect(approver.getByTestId("approver-view")).toBeVisible({ timeout: 10_000 });
    // The closed operations invalidated the request: the server view is the
    // authoritative unavailable state and offers no claim control.
    await expect(approver.getByTestId("approver-status")).toHaveText("invalidated", {
      timeout: 10_000,
    });
    await expect(approver.getByTestId("approver-currentness")).toHaveText("INVALIDATED");
    await expect(approver.locator('[data-testid="approver-claim-button"]')).toHaveCount(0);
    // The Reviewer refetches in-page and observes the recovery invalidation
    // on the authoritative route/history reads.
    await expect(recoveryReviewer.getByTestId("exception-reload-button")).toBeEnabled({
      timeout: 10_000,
    });
    await recoveryReviewer.getByTestId("exception-reload-button").click();
    await expect(recoveryReviewer.getByTestId("exception-route")).toHaveText("manual_review", {
      timeout: 10_000,
    });
    await expect(recoveryReviewer.getByTestId("review-history-exceptions")).toContainText(
      recoveryRequestId,
      { timeout: 10_000 },
    );
    await expect(recoveryReviewer.getByTestId("review-history-exceptions")).toContainText(
      "invalidated",
    );
    const resumed = await operatorApi(
      server,
      "POST",
      "/controlled/s01/api/commands/business-exception-operations/resume",
      { idempotency_key: "s05-ui-resume" },
    );
    expect(resumed.status).toBe(200);
    expect(resumed.body.operations).toBe("open");
    // A request created after the resume works end to end again.
    const reopenedPrep = await uiPrepareApplication(reopenedReviewer, "s05-ui-reopened");
    const reopenedRequestId = await uiRequestException(
      reopenedReviewer,
      server,
      reopenedPrep.applicationId,
      reopenedPrep.workItemId,
    );
    await approver.goto(`${server.baseURL}/controlled/s05/react?request=${encodeURIComponent(reopenedRequestId)}`);
    await expect(approver.getByTestId("approver-claim-button")).toBeEnabled({ timeout: 10_000 });
    await approver.getByTestId("approver-claim-button").click();
    await expect(approver.getByTestId("approver-approve-button")).toBeEnabled({ timeout: 10_000 });
    await approver.getByTestId("approver-approve-button").click();
    await expect(approver.getByTestId("approver-status")).toHaveText("approved", { timeout: 10_000 });

    // --- accessibility and viewports on both shells ---
    for (const page of [reviewer, approver]) {
      const noOverflow = await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      );
      expect(noOverflow).toBe(true);
    }
    const mobileContext = await browser.newContext({
      extraHTTPHeaders: { Authorization: `Bearer ${APPROVER_CREDENTIAL}` },
      viewport: { width: 390, height: 844 },
    });
    const mobile = await mobileContext.newPage();
    try {
      await mobile.goto(`${server.baseURL}/controlled/s05/react?request=${encodeURIComponent(reopenedRequestId)}`);
      await expect(mobile.getByTestId("approver-view")).toBeVisible({ timeout: 10_000 });
      const noOverflow = await mobile.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      );
      expect(noOverflow).toBe(true);
    } finally {
      await mobileContext.close();
    }
  } finally {
    await reviewerContext.close();
    await rejectReviewerContext.close();
    await expiryReviewerContext.close();
    await recoveryReviewerContext.close();
    await reopenedReviewerContext.close();
    await approverContext.close();
    await racerContext.close();
    await stopServer(server);
  }
});
