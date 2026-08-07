const { test, expect } = require("@playwright/test");
const { spawn } = require("child_process");
const crypto = require("crypto");
const { once } = require("events");
const fs = require("fs");
const net = require("net");
const os = require("os");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const PYTHON = process.env.PYTHON || path.join(ROOT, ".venv", "bin", "python");
const REVIEWER_CREDENTIAL = "s06-browser-reviewer-credential";
const INTEGRATOR_CREDENTIAL = "s06-browser-integrator-credential";
const SOURCE = "s06-material-source";
const WORKLOAD = "s06-material-workload";

function sha256(value) {
  return crypto.createHash("sha256").update(value).digest("hex");
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

async function startServer() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "xiaopeng-s06-browser-"));
  const objectRoot = path.join(root, "objects");
  fs.mkdirSync(objectRoot);
  const page = Buffer.from(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
    "base64",
  );
  const result = Buffer.from(
    JSON.stringify({
      per_image_results: [
        {
          image_path: "lease-page.png",
          image_size: { width: 1, height: 1 },
          detections: [
            {
              bbox: [0, 0, 1, 1],
              class_id: 1,
              class_name: "vehicle_identifier",
              confidence: 0.99,
              field_key: "vin",
              ocr_text: "LSVAA4182N2444555",
              value: "LSVAA4182N2444555",
            },
          ],
        },
      ],
    }),
  );
  fs.writeFileSync(path.join(objectRoot, "result.json"), result);
  fs.writeFileSync(path.join(objectRoot, "page.png"), page);
  const registryPath = path.join(root, "registry.json");
  fs.writeFileSync(
    registryPath,
    JSON.stringify({
      schema_version: "s02-runtime-registry/1",
      sources: [
        {
          tenant_id: "c-demo",
          source_system_id: SOURCE,
          workload_identity_id: WORKLOAD,
          adapter_id: "s06-browser-detection-adapter",
          adapter_version: "1",
          source_shape: "ocr-detection/unversioned",
          producer_family: "s06-ocr",
          enabled: true,
        },
      ],
      objects: [
        {
          tenant_id: "c-demo",
          source_system_id: SOURCE,
          object_ref: "s06-result-object",
          media_type: "application/json",
          file: "result.json",
        },
        {
          tenant_id: "c-demo",
          source_system_id: SOURCE,
          object_ref: "s06-page-object",
          media_type: "image/png",
          file: "page.png",
        },
      ],
    }),
  );
  const statePath = path.join(root, "target.sqlite3");
  const port = await reservePort();
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
        PYTHONPYCACHEPREFIX: "/tmp/xiaopeng-task4-s06-browser-pycache",
        TASK4_S01_STATE_PATH: statePath,
        TASK4_S01_TEST_STATE_PATH: statePath,
        TASK4_S01_DEMO_CREDENTIAL: REVIEWER_CREDENTIAL,
        TASK4_S01_DEMO_SUBJECT: "s06-browser-reviewer",
        TASK4_S02_TEST_STATE_PATH: statePath,
        TASK4_S02_TEST_REGISTRY_PATH: registryPath,
        TASK4_S02_TEST_OBJECT_ROOT: objectRoot,
        TASK4_S02_CREDENTIAL: INTEGRATOR_CREDENTIAL,
        TASK4_S02_SUBJECT: "s06-browser-integrator",
        TASK4_S02_TENANT_ID: "c-demo",
        TASK4_S02_SOURCE_SYSTEM_ID: SOURCE,
        TASK4_S02_TEST_SCENARIO_ID: "app_missing_vin_docs.json",
        TASK4_S02_TEST_BACKGROUND_ENABLED: "1",
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
        return { baseURL, child, output, page, result };
      }
    } catch (_) {}
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  child.kill("SIGKILL");
  throw new Error(`S06 browser server did not start: ${output.join("")}`);
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

async function api(page, method, url, body) {
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

function attachmentSubmission(request, server, closed) {
  const itemSequence = closed ? 2 : 1;
  const manifest = {
    batch_id: "s06-batch-1",
    final_sequence: 2,
    item_count: 2,
    scope_mode: "full",
    stream_id: "s06-supplement-stream",
    supplement_request_id: request.request_id,
  };
  return {
    envelope_id: `s06-attachment-envelope-${itemSequence}`,
    schema_version: "1.0.0",
    semantic_version: "1.0.0",
    command_type: "submit_attachment_version",
    upstream_application_ref: "APP-MISS-VINDOC",
    stream_id: "s06-supplement-stream",
    source_revision: itemSequence,
    predecessor_revision: closed ? 1 : null,
    must_understand: [],
    workload_identity_id: WORKLOAD,
    request_binding: {
      supplement_request_id: request.request_id,
      request_context_digest: request.context_digest,
      material_requirement_id: "c-demo-financing-lease-vin/1",
      request_progress_revision: itemSequence,
    },
    document_binding: {
      source_document_ref: "s06-lease-replacement",
      document_type: "financing_lease_contract",
      document_role: "financing_lease_contract",
    },
    attachment_lineage: {
      operation: "replacement",
      predecessor_attachment_id: request.expected_predecessor_attachment_id,
      predecessor_attachment_version: request.expected_predecessor_attachment_version,
      attachment_version: 2,
    },
    batch: {
      batch_id: "s06-batch-1",
      item_sequence: itemSequence,
      item_count: 2,
      final_sequence: 2,
      scope_mode: "full",
      closed,
      manifest_digest: sha256(JSON.stringify(manifest)),
    },
    result_object: {
      controlled_object_ref: "s06-result-object",
      media_type: "application/json",
      size_bytes: server.result.length,
      sha256: sha256(server.result),
    },
    attachments: [
      {
        source_attachment_ref: "s06-source-attachment-2",
        page_ref: "s06-source-page-2",
        page_ordinal: 1,
        source_name_sha256: sha256(Buffer.from("lease-page.png")),
        object: {
          controlled_object_ref: "s06-page-object",
          media_type: "image/png",
          size_bytes: server.page.length,
          sha256: sha256(server.page),
        },
      },
    ],
    producer: {
      producer_id: "s06-producer",
      producer_family: "s06-ocr",
      task_id: "s06-lease-field-extraction",
      task_version: "1",
      run_id: "s06-producer-run-1",
      model_id: "s06-model",
      model_version: "1",
      coordinate_system: { name: "pixel", unit: "pixel", origin: "top_left" },
      confidence_semantics: {
        minimum: 0,
        maximum: 1,
        higher_is: "stronger_detection",
        meaning: "producer_detection_score",
        granularity: "observation",
        calibration: "unknown",
      },
    },
  };
}

test("independent Reviewer and Integrator browsers fulfill one supplement request", async ({
  browser,
}) => {
  const server = await startServer();
  const reviewerContext = await browser.newContext({
    extraHTTPHeaders: { Authorization: `Bearer ${REVIEWER_CREDENTIAL}` },
  });
  const integratorContext = await browser.newContext({
    extraHTTPHeaders: { Authorization: `Bearer ${INTEGRATOR_CREDENTIAL}` },
  });
  const reviewer = await reviewerContext.newPage();
  const integrator = await integratorContext.newPage();
  try {
    expect((await reviewer.goto(`${server.baseURL}/controlled/s01`)).status()).toBe(200);
    expect((await integrator.goto(`${server.baseURL}/controlled/s02`)).status()).toBe(200);
    expect((await reviewerContext.cookies()).some((cookie) => cookie.name === "s01_session")).toBe(
      true,
    );
    expect((await integratorContext.cookies()).some((cookie) => cookie.name === "s02_session")).toBe(
      true,
    );

    const admitted = await api(reviewer, "POST", "/controlled/s01/api/commands/submit", {
      scenario_id: "app_missing_vin_docs.json",
      idempotency_key: "s06-browser-admission",
    });
    expect(admitted.status).toBe(200);
    let item;
    await expect
      .poll(async () => {
        const queue = await api(reviewer, "GET", "/controlled/s01/api/queries/queue");
        item = queue.body.items.find(
          (candidate) => candidate.application_id === admitted.body.application_id,
        );
        return Boolean(item);
      })
      .toBe(true);
    const work = await api(
      reviewer,
      "GET",
      `/controlled/s01/api/queries/review-work-items/${item.work_item_id}`,
    );
    const finding = work.body.automatic_findings.find(
      (candidate) => candidate.rule_id === "R_VIN_CROSS",
    );
    const claim = await api(
      reviewer,
      "POST",
      `/controlled/s01/api/commands/review-work-items/${item.work_item_id}/claim`,
      { expected_context: work.body.command_context },
    );
    const requested = await api(
      reviewer,
      "POST",
      `/controlled/s01/api/commands/review-work-items/${item.work_item_id}/supplement`,
      {
        finding_id: finding.finding_id,
        reason_code: "MISSING_REQUIRED_MATERIAL",
        expected_fence: claim.body.claim_fence,
        expected_context: work.body.command_context,
        idempotency_key: "s06-browser-request",
      },
    );
    expect(requested.status).toBe(200);
    await reviewer.reload();
    const request = await api(
      reviewer,
      "GET",
      `/controlled/s01/api/queries/supplement-requests/${requested.body.request_id}`,
    );
    expect(request.body.material_requirement.responsible_party).toBe(
      "application_material_provider",
    );
    expect(request.body.due_at).toBeGreaterThan(request.body.requested_at);

    const openSubmission = attachmentSubmission(request.body, server, false);
    const wrongRole = await api(
      reviewer,
      "POST",
      "/controlled/s02/api/commands/submit-attachment-version",
      { idempotency_key: "s06-browser-wrong-role", submission: openSubmission },
    );
    const badHashSubmission = structuredClone(openSubmission);
    badHashSubmission.attachments[0].object.sha256 = "0".repeat(64);
    const badHash = await api(
      integrator,
      "POST",
      "/controlled/s02/api/commands/submit-attachment-version",
      { idempotency_key: "s06-browser-bad-hash", submission: badHashSubmission },
    );
    const progress = await api(
      integrator,
      "POST",
      "/controlled/s02/api/commands/submit-attachment-version",
      { idempotency_key: "s06-browser-progress", submission: openSubmission },
    );
    expect(wrongRole.status).toBe(403);
    expect(badHash.body.disposition).toBe("quarantined");
    expect(progress.body.request_status).toBe("open");
    expect(progress.body.phase).toBe("Awaiting Evidence");

    const fulfilled = await api(
      integrator,
      "POST",
      "/controlled/s02/api/commands/submit-attachment-version",
      {
        idempotency_key: "s06-browser-closure",
        submission: attachmentSubmission(request.body, server, true),
      },
    );
    expect(fulfilled.body.request_status).toBe("fulfilled");
    let route;
    let history;
    await expect
      .poll(async () => {
        route = await api(
          reviewer,
          "GET",
          `/controlled/s01/api/queries/applications/${admitted.body.application_id}/current-route`,
        );
        history = await api(
          reviewer,
          "GET",
          `/controlled/s01/api/queries/applications/${admitted.body.application_id}/history`,
        );
        return history.body.runs.length;
      })
      .toBe(2);
    expect(route.body.phase).toBe("Manual Review");
    expect(history.body.attachment_versions.map((item) => item.version)).toEqual([1, 2]);
    expect(history.body.attachment_versions.map((item) => item.current)).toEqual([false, true]);
    const publicSurface = JSON.stringify([
      request.body,
      badHash.body,
      progress.body,
      fulfilled.body,
      route.body,
      history.body,
    ]);
    expect(publicSurface).not.toContain("LSVAA4182N2444555");
  } finally {
    await reviewerContext.close();
    await integratorContext.close();
    await stopServer(server);
  }
});

function canonicalJson(value) {
  if (Array.isArray(value)) {
    return `[${value.map(canonicalJson).join(",")}]`;
  }
  if (value !== null && typeof value === "object") {
    return `{${Object.keys(value)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

function t04SubmissionFromProjection(projection, server, { closed, batchId, streamId }) {
  const material = projection.material_requirement;
  const itemSequence = projection.next_batch_item_sequence;
  const manifest = {
    batch_id: batchId,
    final_sequence: material.batch_item_count,
    item_count: material.batch_item_count,
    scope_mode: "full",
    stream_id: streamId,
    supplement_request_id: projection.request_id,
  };
  return {
    envelope_id: `t04-browser-envelope-${itemSequence}`,
    schema_version: "1.0.0",
    semantic_version: "1.0.0",
    command_type: "submit_attachment_version",
    upstream_application_ref: projection.upstream_application_ref,
    stream_id: streamId,
    source_revision: projection.next_source_revision,
    predecessor_revision: projection.expected_predecessor_revision,
    must_understand: [],
    workload_identity_id: material.allowed_workload_identity_ids[0],
    request_binding: {
      supplement_request_id: projection.request_id,
      request_context_digest: projection.context_digest,
      material_requirement_id: material.material_requirement_id,
      request_progress_revision: projection.next_request_progress_revision,
    },
    document_binding: {
      source_document_ref: "t04-lease-replacement",
      document_type: material.material_kind,
      document_role: material.document_role,
    },
    attachment_lineage: {
      operation: material.operation,
      predecessor_attachment_id: projection.expected_predecessor_attachment_id,
      predecessor_attachment_version:
        projection.expected_predecessor_attachment_version,
      attachment_version: projection.next_attachment_version,
    },
    batch: {
      batch_id: batchId,
      item_sequence: itemSequence,
      item_count: material.batch_item_count,
      final_sequence: material.batch_item_count,
      scope_mode: "full",
      closed,
      manifest_digest: sha256(canonicalJson(manifest)),
    },
    result_object: {
      controlled_object_ref: "s06-result-object",
      media_type: "application/json",
      size_bytes: server.result.length,
      sha256: sha256(server.result),
    },
    attachments: [
      {
        source_attachment_ref: "t04-source-attachment-2",
        page_ref: "t04-source-page-2",
        page_ordinal: 1,
        source_name_sha256: sha256(Buffer.from("lease-page.png")),
        object: {
          controlled_object_ref: "s06-page-object",
          media_type: "image/png",
          size_bytes: server.page.length,
          sha256: sha256(server.page),
        },
      },
    ],
    producer: {
      producer_id: "t04-producer",
      producer_family: "s06-ocr",
      task_id: "t04-lease-field-extraction",
      task_version: "1",
      run_id: "t04-producer-run-1",
      model_id: "t04-model",
      model_version: "1",
      coordinate_system: { name: "pixel", unit: "pixel", origin: "top_left" },
      confidence_semantics: {
        minimum: 0,
        maximum: 1,
        higher_is: "stronger_detection",
        meaning: "producer_detection_score",
        granularity: "observation",
        calibration: "unknown",
      },
    },
  };
}


const GENERIC_404_CONSOLE =
  "Failed to load resource: the server responded with a status of 404 (Not Found)";

/** Exact browser diagnostics for the T04 flows (fix round 1): every >=400
 * response, console error, page error, and failed request is captured with
 * sanitized method/path/status only — never bodies, cookies, keys, hashes,
 * or raw envelopes. */
function trackT04Diagnostics(pages) {
  const records = {
    responses: [],
    consoleErrors: [],
    pageErrors: [],
    failedRequests: [],
  };
  for (const page of pages) {
    page.on("response", (response) => {
      if (response.status() >= 400) {
        records.responses.push({
          method: response.request().method(),
          path: new URL(response.url()).pathname,
          status: response.status(),
        });
      }
    });
    page.on("console", (message) => {
      if (message.type() === "error") {
        records.consoleErrors.push(message.text());
      }
    });
    page.on("pageerror", (error) => records.pageErrors.push(String(error)));
    page.on("requestfailed", (request) => {
      records.failedRequests.push({
        method: request.method(),
        path: new URL(request.url()).pathname,
        failure: request.failure()?.errorText ?? "",
      });
    });
  }
  return records;
}

/** Every allowed diagnostic is matched by exact URL/status/count; all other
 * console/network/page diagnostics fail.  The only permitted console errors
 * are the generic Chromium 404 messages correlated one-to-one with the
 * expected API 404s, plus one net:: failure message per aborted POST. */
function expectExactDiagnostics(records, { api404s, abortedPosts = [] }) {
  const observed404s = records.responses.filter((entry) => entry.status === 404);
  expect(observed404s).toEqual(api404s);
  expect(records.responses.filter((entry) => entry.status !== 404)).toEqual([]);
  expect(
    records.failedRequests.map((entry) => `${entry.method} ${entry.path}`),
  ).toEqual(abortedPosts.map((entry) => `${entry.method} ${entry.path}`));
  for (const failure of records.failedRequests) {
    expect(failure.failure).not.toBe("");
  }
  expect(records.pageErrors).toEqual([]);
  const generic404Count = records.consoleErrors.filter(
    (message) => message === GENERIC_404_CONSOLE,
  ).length;
  expect(generic404Count).toBe(api404s.length);
  const remaining = records.consoleErrors.filter(
    (message) => message !== GENERIC_404_CONSOLE,
  );
  expect(remaining.map((message) => message.split(":")[0])).toEqual(
    abortedPosts.map(() => "Failed to load resource"),
  );
  for (const message of remaining) {
    expect(message).toMatch(/^Failed to load resource: net::/);
  }
}

/** Starts one T04 server plus the separate Reviewer/Integrator contexts and
 * returns the running flow handles. */
async function startT04Flow(browser, { viewport }) {
  const server = await startServer();
  const reviewerContext = await browser.newContext({
    viewport,
    extraHTTPHeaders: { Authorization: `Bearer ${REVIEWER_CREDENTIAL}` },
  });
  const integratorContext = await browser.newContext({
    viewport,
    extraHTTPHeaders: { Authorization: `Bearer ${INTEGRATOR_CREDENTIAL}` },
  });
  const reviewer = await reviewerContext.newPage();
  const integrator = await integratorContext.newPage();
  const diagnostics = trackT04Diagnostics([reviewer, integrator]);
  return {
    server,
    reviewerContext,
    integratorContext,
    reviewer,
    integrator,
    diagnostics,
  };
}

async function stopT04Flow(flow) {
  await flow.reviewerContext.close();
  await flow.integratorContext.close();
  await stopServer(flow.server);
}

/** Admits the demo scenario through the API and drives the Reviewer UI to
 * the accepted supplement request; returns the server-issued request id. */
async function reviewerCreatesSupplementRequest(reviewer, server) {
  const admitted = await api(
    reviewer,
    "POST",
    "/controlled/s01/api/commands/submit",
    {
      scenario_id: "app_missing_vin_docs.json",
      idempotency_key: `t04-admission-${crypto.randomUUID()}`,
    },
  );
  expect(admitted.status).toBe(200);
  await reviewer.reload();
  await expect(reviewer.getByTestId("queue-item")).toBeVisible({ timeout: 15000 });
  await reviewer.getByTestId("queue-manual-link").click();
  await expect(reviewer.getByTestId("claim-button")).toBeEnabled({ timeout: 10000 });
  await reviewer.getByTestId("claim-button").click();
  await expect(reviewer.getByTestId("supplement-button")).toBeEnabled({
    timeout: 10000,
  });
  await reviewer.getByTestId("supplement-button").click();
  await expect(reviewer.getByTestId("review-supplement-request")).toBeVisible({
    timeout: 10000,
  });
  await expect(reviewer.getByTestId("review-supplement-status")).toHaveText("open");
  const requestId = (
    await reviewer.getByTestId("review-supplement-request-id").textContent()
  ).trim();
  expect(requestId).toMatch(/^supplement_request_/);
  return requestId;
}

async function integratorOpensProjection(integrator, server, requestId) {
  await integrator.goto(
    `${server.baseURL}/controlled/s02/react?request=${encodeURIComponent(requestId)}`,
  );
  await expect(integrator.getByTestId("integrator-projection-status")).toHaveText(
    "open",
    { timeout: 10000 },
  );
  expect(
    await integrator
      .getByTestId("integrator-projection-request-id")
      .textContent(),
  ).toContain(requestId);
}

/** Fills the envelope textarea and submits through the panel; returns the
 * rendered receipt announcement text. */
async function integratorSubmitsEnvelope(integrator, submission, { timeout = 10000 } = {}) {
  await integrator
    .getByTestId("integrator-envelope-input")
    .fill(JSON.stringify(submission));
  await integrator.getByTestId("integrator-submit-button").click();
  await expect(integrator.getByTestId("integrator-receipt")).toBeVisible({
    timeout,
  });
  return (
    await integrator.getByTestId("integrator-disposition-announcement").textContent()
  ).trim();
}

async function integratorProjection(integrator, requestId) {
  const response = await api(
    integrator,
    "GET",
    `/controlled/s02/api/queries/supplement-requests/${encodeURIComponent(requestId)}`,
  );
  expect(response.status).toBe(200);
  return response.body;
}

/** Reviewer authoritative refetch and the exact post-fulfillment assertions:
 * exactly one server-current successor run matching current-route, v1
 * non-current and v2 current. */
async function reviewerConverges(flow) {
  const { reviewer, server } = flow;
  await reviewer.getByTestId("supplement-reload-button").click();
  await expect(reviewer.getByTestId("review-supplement-converged")).toBeVisible({
    timeout: 20000,
  });
  const converged = await reviewer
    .getByTestId("review-supplement-converged")
    .textContent();
  expect(converged).toContain("证据修订 2");
  const attachments = await reviewer
    .getByTestId("review-history-attachments")
    .textContent();
  expect(attachments).toContain("v1");
  expect(attachments).toContain("v2");
  expect(attachments.match(/· 当前/g) ?? []).toHaveLength(1);
  expect(attachments.match(/· 非当前/g) ?? []).toHaveLength(1);
  const runs = await reviewer.getByTestId("review-history-runs").textContent();
  expect(runs.match(/· 当前/g) ?? []).toHaveLength(1);
  expect(runs.match(/· 非当前/g) ?? []).toHaveLength(1);
  const route = await api(
    reviewer,
    "GET",
    `/controlled/s01/api/queries/applications/${flow.applicationId}/current-route`,
  );
  const history = await api(
    reviewer,
    "GET",
    `/controlled/s01/api/queries/applications/${flow.applicationId}/history`,
  );
  expect(route.status).toBe(200);
  expect(history.status).toBe(200);
  expect(route.body.phase).toBe("Manual Review");
  expect(history.body.current_run_id).toBe(route.body.current_run_id);
  expect(history.body.runs.map((run) => run.current)).toEqual([false, true]);
  expect(history.body.attachment_versions.map((item) => item.version)).toEqual([
    1,
    2,
  ]);
  expect(history.body.attachment_versions.map((item) => item.current)).toEqual([
    false,
    true,
  ]);
}

/** The exact expected Reviewer 404 set: the invalidated work item's
 * workspace existence-hides once at acceptance and once at the reviewer's
 * authoritative reload. */
function expectedReviewerWorkspace404s(flow) {
  return [
    {
      method: "GET",
      path: `/controlled/s01/api/queries/applications/${flow.applicationId}/workspace`,
      status: 404,
    },
  ];
}

for (const viewport of [
  { name: "desktop 1280x800", width: 1280, height: 800 },
  { name: "mobile 390x844", width: 390, height: 844 },
]) {
  test(`T04 React tracer (${viewport.name}): Reviewer request -> Integrator current projection -> valid progress/closure -> Reviewer current route/history`, async ({
    browser,
  }) => {
    test.setTimeout(120_000);
    const flow = await startT04Flow(browser, { viewport });
    const { reviewer, integrator, server, diagnostics } = flow;
    try {
      expect(
        (await reviewer.goto(`${server.baseURL}/controlled/s01/react`)).status(),
      ).toBe(200);
      expect(
        (await integrator.goto(`${server.baseURL}/controlled/s02/react`)).status(),
      ).toBe(200);
      expect(
        (await flow.reviewerContext.cookies()).some(
          (cookie) => cookie.name === "s01_session",
        ),
      ).toBe(true);
      expect(
        (await flow.integratorContext.cookies()).some(
          (cookie) => cookie.name === "s02_session",
        ),
      ).toBe(true);

      const requestId = await reviewerCreatesSupplementRequest(reviewer, server);
      flow.applicationId = (
        await api(
          reviewer,
          "GET",
          `/controlled/s01/api/queries/supplement-requests/${encodeURIComponent(requestId)}`,
        )
      ).body.application_id;
      await integratorOpensProjection(integrator, server, requestId);

      const openProjection = await integratorProjection(integrator, requestId);
      expect(openProjection.next_request_progress_revision).toBe(1);
      const openSubmission = t04SubmissionFromProjection(openProjection, server, {
        closed: false,
        batchId: "t04-browser-batch",
        streamId: "t04-browser-stream",
      });
      await integratorSubmitsEnvelope(integrator, openSubmission);
      await expect(
        integrator.getByTestId("integrator-receipt-request-status"),
      ).toHaveText("open");
      // A 200 command receipt is not recheck completion: the reviewer shell
      // must not announce any convergence from the integrator's receipt.
      expect(
        await reviewer.getByTestId("review-supplement-converged").isVisible(),
      ).toBe(false);

      const afterProgress = await integratorProjection(integrator, requestId);
      expect(afterProgress.next_request_progress_revision).toBe(2);
      expect(afterProgress.expected_predecessor_revision).toBe(1);
      expect(afterProgress.batch.batch_id).toBe("t04-browser-batch");
      const closedSubmission = t04SubmissionFromProjection(afterProgress, server, {
        closed: true,
        batchId: "t04-browser-batch",
        streamId: "t04-browser-stream",
      });
      await integratorSubmitsEnvelope(integrator, closedSubmission);
      await expect(
        integrator.getByTestId("integrator-receipt-request-status"),
      ).toHaveText("fulfilled", { timeout: 10000 });

      await reviewerConverges(flow);
      await expectViewportIntegrity(reviewer, [
        "review-panel",
        "review-supplement-request",
        "supplement-reload-button",
        "review-history-attachments",
      ]);
      await expectViewportIntegrity(integrator, [
        "integrator-projection",
        "integrator-envelope-input",
        "integrator-reload-button",
        "integrator-submit-button",
        "integrator-receipt",
      ]);
      expectExactDiagnostics(diagnostics, {
        api404s: expectedReviewerWorkspace404s(flow),
      });
      expect(await reviewer.evaluate(() => sessionStorage.length)).toBe(0);
      expect(await reviewer.evaluate(() => localStorage.length)).toBe(0);
      expect(await integrator.evaluate(() => sessionStorage.length)).toBe(0);
      expect(await integrator.evaluate(() => localStorage.length)).toBe(0);
      const integratorCookies = await flow.integratorContext.cookies();
      expect(integratorCookies.map((cookie) => cookie.name)).toEqual([
        "s02_session",
      ]);
      // URLs/query/hash carry no command, key, envelope, hash, or
      // unauthorized identifier.
      const integratorUrl = integrator.url();
      expect(integratorUrl).toMatch(/^[^?#]*\?request=[A-Za-z0-9_:-]+$/);
      expect(integratorUrl).not.toMatch(/[0-9a-f]{64}/);
      expect(integratorUrl).not.toContain("envelope");
      expect(integratorUrl).not.toContain("idempotency");
      expect(integratorUrl).not.toContain(flow.applicationId);
      // No foreign facts in either panel surface.
      const publicSurface = JSON.stringify([
        openProjection,
        afterProgress,
        await reviewer.getByTestId("review-panel").textContent(),
        await integrator.getByTestId("integrator-panel").textContent(),
        integratorUrl,
      ]);
      expect(publicSurface).not.toContain("LSVAA4182N2444555");
    } finally {
      await stopT04Flow(flow);
    }
  });
}

/** Viewport integrity: no horizontal overflow, no clipped control, and no
 * pairwise-overlapping text boxes among the primary panel controls. */
async function expectViewportIntegrity(page, testIds) {
  const layout = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
    innerWidth: window.innerWidth,
    innerHeight: window.innerHeight,
  }));
  expect(layout.scrollWidth).toBeLessThanOrEqual(layout.clientWidth);
  const boxes = await page.evaluate(
    (ids) =>
      ids.map((id) => {
        const element = document.querySelector(`[data-testid="${id}"]`);
        if (element === null) return null;
        const rect = element.getBoundingClientRect();
        const ancestor = ids.find(
          (candidate) =>
            candidate !== id &&
            element.closest(`[data-testid="${candidate}"]`) !== null,
        );
        return {
          id,
          x: rect.x,
          y: rect.y,
          w: rect.width,
          h: rect.height,
          ancestor,
        };
      }),
    testIds,
  );
  const present = boxes.filter((box) => box !== null);
  expect(present.length).toBeGreaterThan(0);
  for (const box of present) {
    // No horizontal clipping: every control spans only the viewport width.
    expect(box.x).toBeGreaterThanOrEqual(-1);
    expect(box.x + box.w).toBeLessThanOrEqual(layout.innerWidth + 1);
    expect(box.w).toBeGreaterThan(0);
    expect(box.h).toBeGreaterThan(0);
  }
  // No overlapping text among sibling/peer controls (ancestor containment
  // is normal block nesting, not an overlap).
  for (let i = 0; i < present.length; i += 1) {
    for (let j = i + 1; j < present.length; j += 1) {
      const a = present[i];
      const b = present[j];
      if (a.ancestor === b.id || b.ancestor === a.id) {
        continue;
      }
      const intersects =
        a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
      expect(
        intersects,
        `${a.id} overlaps ${b.id} at ${JSON.stringify([
          { id: a.id, x: a.x, y: a.y, w: a.w, h: a.h },
          { id: b.id, x: b.x, y: b.y, w: b.w, h: b.h },
        ])}`,
      ).toBe(false);
    }
  }
}

test("T04 React: lost-response exact replay returns the original receipt and a same-key conflict has no second effect", async ({
  browser,
}) => {
  test.setTimeout(120_000);
  const flow = await startT04Flow(browser, {
    viewport: { width: 1280, height: 800 },
  });
  const { reviewer, integrator, server, diagnostics } = flow;
  try {
    expect(
      (await reviewer.goto(`${server.baseURL}/controlled/s01/react`)).status(),
    ).toBe(200);
    await integrator.goto(`${server.baseURL}/controlled/s02/react`);
    const requestId = await reviewerCreatesSupplementRequest(reviewer, server);
    flow.applicationId = (
      await api(
        reviewer,
        "GET",
        `/controlled/s01/api/queries/supplement-requests/${encodeURIComponent(requestId)}`,
      )
    ).body.application_id;
    await integratorOpensProjection(integrator, server, requestId);
    const openProjection = await integratorProjection(integrator, requestId);

    let submitCount = 0;
    let firstKey = null;
    let firstBody = null;
    let conflictBody = null;
    await integrator.route(
      "**/api/commands/submit-attachment-version",
      async (route) => {
        submitCount += 1;
        const post = route.request().postDataJSON();
        if (submitCount === 1) {
          // The server commits the progress receipt, then the response is
          // lost at the transport: the panel must hold the exact command
          // and key for replay.
          firstKey = post.idempotency_key;
          firstBody = post.submission;
          await route.fetch();
          await route.abort("connectionrefused");
        } else if (submitCount === 2) {
          await route.continue();
        } else if (submitCount === 3) {
          // Same semantic key with a different fingerprint: the domain
          // answers a definitive conflict receipt with no second effect.
          conflictBody = post.submission;
          const rewritten = { ...post, idempotency_key: firstKey };
          const response = await route.fetch({
            postData: JSON.stringify(rewritten),
          });
          await route.fulfill({ response });
        } else {
          await route.continue();
        }
      },
    );

    const openSubmission = t04SubmissionFromProjection(openProjection, server, {
      closed: false,
      batchId: "t04-replay-batch",
      streamId: "t04-replay-stream",
    });
    await integrator
      .getByTestId("integrator-envelope-input")
      .fill(JSON.stringify(openSubmission));
    await integrator.getByTestId("integrator-submit-button").click();
    await expect(integrator.getByTestId("integrator-unknown")).toContainText(
      "结果未知",
      { timeout: 10000 },
    );
    expect(integrator.getByRole("button", { name: "重试" })).toBeVisible();
    expect(
      (await integrator.getByTestId("integrator-envelope-input").isDisabled()),
    ).toBe(true);

    // Exact replay: same command bytes and the same key, no third POST.
    await integrator.getByRole("button", { name: "重试" }).click();
    await expect(
      integrator.getByTestId("integrator-disposition-announcement"),
    ).toHaveText("附件版本已接受（重放原回执）", { timeout: 10000 });
    await expect(integrator.getByTestId("integrator-receipt-replayed")).toHaveText(
      "true",
    );
    expect(submitCount).toBe(2);
    expect(firstKey).toMatch(/^[0-9a-f-]{36}$/);
    // One committed progress effect only: the next revision advances once
    // and no evidence/job/run/route changed.
    const afterReplay = await integratorProjection(integrator, requestId);
    expect(afterReplay.next_request_progress_revision).toBe(2);
    expect(afterReplay.status).toBe("open");
    const historyAfterReplay = await api(
      reviewer,
      "GET",
      `/controlled/s01/api/queries/applications/${flow.applicationId}/history`,
    );
    expect(historyAfterReplay.body.evidence_revision ?? historyAfterReplay.body.runs[0].evidence_revision).toBe(1);

    // Same key, different fingerprint: definitive conflict, no second effect.
    const conflictingSubmission = structuredClone(openSubmission);
    conflictingSubmission.envelope_id = "t04-conflicting-envelope";
    conflictingSubmission.source_attachment_ref = "t04-conflicting-attachment";
    await integrator
      .getByTestId("integrator-envelope-input")
      .fill(JSON.stringify(conflictingSubmission));
    await integrator.getByTestId("integrator-submit-button").click();
    await expect(
      integrator.getByTestId("integrator-disposition-announcement"),
    ).toHaveText("附件版本被拒绝（intake.idempotency_conflict）", {
      timeout: 10000,
    });
    expect(submitCount).toBe(3);
    expect(JSON.stringify(conflictBody)).not.toBe(JSON.stringify(firstBody));
    const afterConflict = await integratorProjection(integrator, requestId);
    expect(afterConflict.next_request_progress_revision).toBe(2);
    expect(afterConflict.status).toBe("open");

    // The valid closure still completes and the reviewer converges.
    const closedSubmission = t04SubmissionFromProjection(afterConflict, server, {
      closed: true,
      batchId: "t04-replay-batch",
      streamId: "t04-replay-stream",
    });
    await integrator
      .getByTestId("integrator-envelope-input")
      .fill(JSON.stringify(closedSubmission));
    await integrator.getByTestId("integrator-submit-button").click();
    await expect(
      integrator.getByTestId("integrator-receipt-request-status"),
    ).toHaveText("fulfilled", { timeout: 10000 });
    await reviewerConverges(flow);
    expectExactDiagnostics(diagnostics, {
      api404s: expectedReviewerWorkspace404s(flow),
      abortedPosts: [
        {
          method: "POST",
          path: "/controlled/s02/api/commands/submit-attachment-version",
        },
      ],
    });
  } finally {
    await stopT04Flow(flow);
  }
});

test("T04 React: awaiting_predecessor, rejected and quarantined receipts create no evidence/job/run/route/fulfillment effect", async ({
  browser,
}) => {
  test.setTimeout(120_000);
  const flow = await startT04Flow(browser, {
    viewport: { width: 1280, height: 800 },
  });
  const { reviewer, integrator, server, diagnostics } = flow;
  try {
    expect(
      (await reviewer.goto(`${server.baseURL}/controlled/s01/react`)).status(),
    ).toBe(200);
    await integrator.goto(`${server.baseURL}/controlled/s02/react`);
    const requestId = await reviewerCreatesSupplementRequest(reviewer, server);
    flow.applicationId = (
      await api(
        reviewer,
        "GET",
        `/controlled/s01/api/queries/supplement-requests/${encodeURIComponent(requestId)}`,
      )
    ).body.application_id;
    await integratorOpensProjection(integrator, server, requestId);
    const openProjection = await integratorProjection(integrator, requestId);

    const baseSubmission = t04SubmissionFromProjection(openProjection, server, {
      closed: false,
      batchId: "t04-disposition-batch",
      streamId: "t04-disposition-stream",
    });

    // Predecessor gap: the declared source revision skips the next one.
    const gapSubmission = structuredClone(baseSubmission);
    gapSubmission.source_revision = openProjection.next_source_revision + 1;
    const gapAnnouncement = await integratorSubmitsEnvelope(integrator, gapSubmission);
    expect(gapAnnouncement).toBe(
      `附件版本等待前驱（intake.sequence_gap）`,
    );
    await expect(
      integrator.getByTestId("integrator-receipt-disposition"),
    ).toHaveText("awaiting_predecessor");

    // Invalid request context: rejected.
    const contextSubmission = structuredClone(baseSubmission);
    contextSubmission.request_binding.request_context_digest = "0".repeat(64);
    const contextAnnouncement = await integratorSubmitsEnvelope(
      integrator,
      contextSubmission,
    );
    expect(contextAnnouncement).toBe(
      "附件版本被拒绝（intake.request_context_mismatch）",
    );
    await expect(
      integrator.getByTestId("integrator-receipt-disposition"),
    ).toHaveText("rejected");

    // Invalid attachment version breaks the canonical lineage contract:
    // quarantined at the envelope boundary with the stable reason.
    const versionSubmission = structuredClone(baseSubmission);
    versionSubmission.attachment_lineage.attachment_version =
      openProjection.next_attachment_version + 1;
    const versionAnnouncement = await integratorSubmitsEnvelope(
      integrator,
      versionSubmission,
    );
    expect(versionAnnouncement).toBe(
      "附件版本被隔离（evidence.provenance_invalid）",
    );
    await expect(
      integrator.getByTestId("integrator-receipt-disposition"),
    ).toHaveText("quarantined");

    // Bad hash / integrity failure: quarantined.
    const hashSubmission = structuredClone(baseSubmission);
    hashSubmission.attachments[0].object.sha256 = "0".repeat(64);
    const hashAnnouncement = await integratorSubmitsEnvelope(integrator, hashSubmission);
    expect(hashAnnouncement).toMatch(/^附件版本被隔离（/);
    await expect(
      integrator.getByTestId("integrator-receipt-disposition"),
    ).toHaveText("quarantined");

    // None of the three produced progress, evidence, jobs, runs, routes or
    // fulfillment.
    const afterDispositions = await integratorProjection(integrator, requestId);
    expect(afterDispositions.next_request_progress_revision).toBe(1);
    expect(afterDispositions.status).toBe("open");
    expect(afterDispositions.current).toBe(true);
    const history = await api(
      reviewer,
      "GET",
      `/controlled/s01/api/queries/applications/${flow.applicationId}/history`,
    );
    expect(history.body.runs).toHaveLength(1);
    expect(history.body.runs[0].evidence_revision).toBe(1);
    expect(history.body.attachment_versions).toHaveLength(1);
    expect(history.body.attachment_versions[0].version).toBe(1);
    const route = await api(
      reviewer,
      "GET",
      `/controlled/s01/api/queries/applications/${flow.applicationId}/current-route`,
    );
    expect(route.body.phase).toBe("Supplement");
    expect(route.body.route).toBe("supplement_pending");

    // The request stays viable: the valid path still fulfills.
    const progressAnnouncement = await integratorSubmitsEnvelope(
      integrator,
      baseSubmission,
    );
    expect(progressAnnouncement).toBe("附件版本已接受");
    const afterProgress = await integratorProjection(integrator, requestId);
    const closedSubmission = t04SubmissionFromProjection(afterProgress, server, {
      closed: true,
      batchId: "t04-disposition-batch",
      streamId: "t04-disposition-stream",
    });
    await integratorSubmitsEnvelope(integrator, closedSubmission);
    await expect(
      integrator.getByTestId("integrator-receipt-request-status"),
    ).toHaveText("fulfilled", { timeout: 10000 });
    await reviewerConverges(flow);
    expectExactDiagnostics(diagnostics, {
      api404s: expectedReviewerWorkspace404s(flow),
    });
  } finally {
    await stopT04Flow(flow);
  }
});

test("T04 React: wrong role/request scope and lost sessions are existence-hiding with no foreign facts", async ({
  browser,
}) => {
  test.setTimeout(120_000);
  const flow = await startT04Flow(browser, {
    viewport: { width: 1280, height: 800 },
  });
  const { reviewer, integrator, server, diagnostics } = flow;
  try {
    expect(
      (await reviewer.goto(`${server.baseURL}/controlled/s01/react`)).status(),
    ).toBe(200);
    await integrator.goto(`${server.baseURL}/controlled/s02/react`);
    const requestId = await reviewerCreatesSupplementRequest(reviewer, server);
    flow.applicationId = (
      await api(
        reviewer,
        "GET",
        `/controlled/s01/api/queries/supplement-requests/${encodeURIComponent(requestId)}`,
      )
    ).body.application_id;

    // Cross-role read of the Integrator projection: same sanitized 404.
    const crossRole = await api(
      reviewer,
      "GET",
      `/controlled/s02/api/queries/supplement-requests/${encodeURIComponent(requestId)}`,
    );
    expect(crossRole.status).toBe(404);
    expect(crossRole.body).toEqual({ detail: { error: "S02_NOT_FOUND" } });

    // Unknown request scope in the Integrator shell: sanitized panel error.
    await integrator.goto(
      `${server.baseURL}/controlled/s02/react?request=${encodeURIComponent("supplement_request_missing00000000000000000000000")}`,
    );
    await expect(integrator.getByTestId("integrator-projection-error")).toHaveText(
      "请求未找到或无权访问",
      { timeout: 10000 },
    );
    expect(
      await integrator.getByTestId("integrator-projection-error").textContent(),
    ).not.toContain("S02_NOT_FOUND");

    // A valid projection loads, then the session disappears: the panel
    // suppresses every protected fact and submission.
    await integratorOpensProjection(integrator, server, requestId);
    const protectedFacts = [
      flow.applicationId,
      "finding_",
      "run_",
      "LSVAA4182N2444555",
      "requester_subject",
      "snapshot_",
    ];
    const loadedText = await integrator
      .getByTestId("integrator-panel")
      .textContent();
    for (const fact of protectedFacts) {
      expect(loadedText).not.toContain(fact);
    }
    await flow.integratorContext.clearCookies();
    await integrator.getByTestId("integrator-reload-button").click();
    await expect(integrator.getByTestId("integrator-projection-error")).toHaveText(
      "请求未找到或无权访问",
      { timeout: 10000 },
    );
    expect(await integrator.getByTestId("integrator-panel").textContent()).not.toContain(
      flow.applicationId,
    );
    expect(
      await integrator
        .getByTestId("integrator-panel")
        .textContent(),
    ).not.toContain("LSVAA4182N2444555");
    expect(
      await integrator.getByRole("button", { name: "提交附件版本" }).isHidden(),
    ).toBe(true);

    // Wrong workload on the command seam: rejected receipt, no effect.
    await integrator.goto(
      `${server.baseURL}/controlled/s02/react?request=${encodeURIComponent(requestId)}`,
    );
    await expect(integrator.getByTestId("integrator-projection-status")).toHaveText(
      "open",
      { timeout: 10000 },
    );
    const openProjection = await integratorProjection(integrator, requestId);
    const wrongWorkload = t04SubmissionFromProjection(openProjection, server, {
      closed: false,
      batchId: "t04-scope-batch",
      streamId: "t04-scope-stream",
    });
    wrongWorkload.workload_identity_id = "wrong-workload-identity";
    const wrongWorkloadAnnouncement = await integratorSubmitsEnvelope(
      integrator,
      wrongWorkload,
    );
    expect(wrongWorkloadAnnouncement).toBe(
      "附件版本被拒绝（intake.source_disabled）",
    );
    const afterWrongWorkload = await integratorProjection(integrator, requestId);
    expect(afterWrongWorkload.next_request_progress_revision).toBe(1);

    expectExactDiagnostics(diagnostics, {
      api404s: [
        {
          method: "GET",
          path: `/controlled/s01/api/queries/applications/${flow.applicationId}/workspace`,
          status: 404,
        },
        {
          method: "GET",
          path: `/controlled/s02/api/queries/supplement-requests/${encodeURIComponent(requestId)}`,
          status: 404,
        },
        {
          method: "GET",
          path: `/controlled/s02/api/queries/supplement-requests/${encodeURIComponent("supplement_request_missing00000000000000000000000")}`,
          status: 404,
        },
        {
          method: "GET",
          path: `/controlled/s02/api/queries/supplement-requests/${encodeURIComponent(requestId)}`,
          status: 404,
        },
      ],
    });
  } finally {
    await stopT04Flow(flow);
  }
});

async function tabToTestId(page, testId, { maxTabs = 100 } = {}) {
  for (let attempt = 0; attempt < maxTabs; attempt += 1) {
    await page.keyboard.press("Tab");
    const reached = await page.evaluate((id) => {
      const active = document.activeElement;
      if (active === null || active === document.body) return false;
      if ((active.getAttribute?.("data-testid") ?? "") === id) return true;
      return active.closest(`[data-testid="${id}"]`) !== null;
    }, testId);
    if (reached) {
      const focusVisible = await page.evaluate(
        () =>
          document.activeElement !== null &&
          document.activeElement !== document.body &&
          document.activeElement.matches(":focus-visible"),
      );
      expect(focusVisible, `${testId} must carry visible focus`).toBe(true);
      return;
    }
  }
  throw new Error(`keyboard navigation could not reach ${testId}`);
}

test("T04 React: the full operator flow works by keyboard with visible focus and truthful live status", async ({
  browser,
}) => {
  test.setTimeout(120_000);
  const flow = await startT04Flow(browser, {
    viewport: { width: 1280, height: 800 },
  });
  const { reviewer, integrator, server, diagnostics } = flow;
  try {
    expect(
      (await reviewer.goto(`${server.baseURL}/controlled/s01/react`)).status(),
    ).toBe(200);
    await integrator.goto(`${server.baseURL}/controlled/s02/react`);
    const admitted = await api(
      reviewer,
      "POST",
      "/controlled/s01/api/commands/submit",
      {
        scenario_id: "app_missing_vin_docs.json",
        idempotency_key: `t04-keyboard-admission-${crypto.randomUUID()}`,
      },
    );
    expect(admitted.status).toBe(200);
    await reviewer.reload();
    await expect(reviewer.getByTestId("queue-item")).toBeVisible({ timeout: 15000 });

    await tabToTestId(reviewer, "queue-manual-link");
    await reviewer.keyboard.press("Enter");
    await expect(reviewer.getByTestId("claim-button")).toBeEnabled({
      timeout: 10000,
    });
    await tabToTestId(reviewer, "claim-button");
    await reviewer.keyboard.press("Enter");
    await expect(reviewer.getByTestId("review-command-status")).toContainText(
      "认领已接受",
      { timeout: 10000 },
    );
    await tabToTestId(reviewer, "supplement-button");
    await reviewer.keyboard.press("Enter");
    await expect(reviewer.getByTestId("review-supplement-request")).toBeVisible({
      timeout: 10000,
    });
    await expect(reviewer.getByTestId("review-supplement-status")).toHaveText("open");
    const requestId = (
      await reviewer.getByTestId("review-supplement-request-id").textContent()
    ).trim();
    flow.applicationId = (
      await api(
        reviewer,
        "GET",
        `/controlled/s01/api/queries/supplement-requests/${encodeURIComponent(requestId)}`,
      )
    ).body.application_id;

    await integrator.goto(
      `${server.baseURL}/controlled/s02/react?request=${encodeURIComponent(requestId)}`,
    );
    await expect(integrator.getByTestId("integrator-projection-status")).toHaveText(
      "open",
      { timeout: 10000 },
    );
    const openProjection = await integratorProjection(integrator, requestId);
    const openSubmission = t04SubmissionFromProjection(openProjection, server, {
      closed: false,
      batchId: "t04-keyboard-batch",
      streamId: "t04-keyboard-stream",
    });
    await tabToTestId(integrator, "integrator-envelope-input");
    await integrator.keyboard.insertText(JSON.stringify(openSubmission));
    await tabToTestId(integrator, "integrator-submit-button");
    await integrator.keyboard.press("Enter");
    await expect(integrator.getByTestId("integrator-disposition-announcement")).toHaveText(
      "附件版本已接受",
      { timeout: 10000 },
    );
    expect(
      await integrator.getByTestId("integrator-command-status").textContent(),
    ).toContain("附件版本已接受");

    const afterProgress = await integratorProjection(integrator, requestId);
    const closedSubmission = t04SubmissionFromProjection(afterProgress, server, {
      closed: true,
      batchId: "t04-keyboard-batch",
      streamId: "t04-keyboard-stream",
    });
    await tabToTestId(integrator, "integrator-envelope-input");
    await integrator.keyboard.press("Control+a");
    await integrator.keyboard.press("Delete");
    await integrator.keyboard.insertText(JSON.stringify(closedSubmission));
    await tabToTestId(integrator, "integrator-submit-button");
    await integrator.keyboard.press("Enter");
    await expect(
      integrator.getByTestId("integrator-receipt-request-status"),
    ).toHaveText("fulfilled", { timeout: 10000 });

    await tabToTestId(reviewer, "supplement-reload-button");
    await reviewer.keyboard.press("Enter");
    await expect(reviewer.getByTestId("review-supplement-converged")).toBeVisible({
      timeout: 20000,
    });
    expect(
      await reviewer.getByTestId("review-supplement-converged").textContent(),
    ).toContain("证据修订 2");
    expectExactDiagnostics(diagnostics, {
      api404s: expectedReviewerWorkspace404s(flow),
    });
  } finally {
    await stopT04Flow(flow);
  }
});
