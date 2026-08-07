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

/** Starts the S06 server.  With ``clockSeconds`` set, a test-only expiring
 * S02 session factory is used whose session clock is backed by a file under
 * this harness's own temporary root, and the S02 session TTL is shortened,
 * so retained-token expiry can be exercised without a production backdoor. */
async function startServer({
  appTarget = "task4_consistency.web.app:create_s02_test_app",
  extraEnv = {},
  clockSeconds = null,
} = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "xiaopeng-s06-browser-"));
  let clockPath = null;
  if (clockSeconds !== null) {
    clockPath = path.join(root, "session-clock.txt");
    // The clock baseline matches the browser epoch so claim/expiry
    // comparisons stay live while the short S02 TTL can genuinely expire.
    fs.writeFileSync(clockPath, String(Math.floor(Date.now() / 1000)), "ascii");
    appTarget = "tests.test_s06_http:create_expiring_s02_session_app";
    extraEnv = {
      ...extraEnv,
      TASK4_S01_TEST_SESSION_CLOCK_PATH: clockPath,
      TASK4_S02_TEST_SESSION_TTL_SECONDS: String(clockSeconds),
    };
  }
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
        ...extraEnv,
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
        return { baseURL, child, output, page, result, clockPath, root };
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

/** Secret-safe browser diagnostics for the T04 flows: every >=400 response,
 * console error, page error, and failed request is classified at capture
 * time into a stable category plus sanitized method/path/status.  Raw
 * message/error/failure text, bodies, cookies, keys, hashes, envelopes,
 * internal IDs, and rendered surfaces are never retained, so a failing
 * assertion can only print categories. */
const CONSOLE_RESOURCE_404 = "console:resource-404";
const CONSOLE_NET_FAILURE = "console:net-failure";
const CONSOLE_UNEXPECTED = "console:unexpected";
const PAGE_ERROR = "page-error";
const NETWORK_FAILURE = "network-failure";
const DIAGNOSTIC_WORKSPACE_PATH =
  "/controlled/s01/api/queries/applications/{application_id}/workspace";
const DIAGNOSTIC_S02_REQUEST_PATH =
  "/controlled/s02/api/queries/supplement-requests/{request_id}";
const DIAGNOSTIC_RESOURCE_IDS = new Map([
  ["applications", "{application_id}"],
  ["review-work-items", "{work_item_id}"],
  ["supplement-requests", "{request_id}"],
]);

function sanitizedDiagnosticPath(url) {
  const segments = new URL(url).pathname.split("/");
  return segments
    .map((segment, index) => DIAGNOSTIC_RESOURCE_IDS.get(segments[index - 1]) ?? segment)
    .join("/");
}

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
          path: sanitizedDiagnosticPath(response.url()),
          status: response.status(),
        });
      }
    });
    page.on("console", (message) => {
      if (message.type() !== "error") return;
      const text = message.text();
      if (text === GENERIC_404_CONSOLE) {
        records.consoleErrors.push(CONSOLE_RESOURCE_404);
      } else if (text.startsWith("Failed to load resource: net::")) {
        records.consoleErrors.push(CONSOLE_NET_FAILURE);
      } else {
        records.consoleErrors.push(CONSOLE_UNEXPECTED);
      }
    });
    page.on("pageerror", () => records.pageErrors.push(PAGE_ERROR));
    page.on("requestfailed", (request) => {
      records.failedRequests.push({
        method: request.method(),
        path: sanitizedDiagnosticPath(request.url()),
        category: NETWORK_FAILURE,
      });
    });
  }
  return records;
}

/** Every allowed diagnostic is matched by exact URL/status/count; all other
 * console/network/page diagnostics fail.  The only permitted console
 * categories are the resource-404 messages correlated one-to-one with the
 * expected API 404s, plus one net-failure category per aborted POST. */
function expectExactDiagnostics(records, { api404s, abortedPosts = [] }) {
  const observed404s = records.responses.filter((entry) => entry.status === 404);
  expect(observed404s).toEqual(api404s);
  expect(records.responses.filter((entry) => entry.status !== 404)).toEqual([]);
  expect(
    records.failedRequests.map((entry) => `${entry.method} ${entry.path}`),
  ).toEqual(abortedPosts.map((entry) => `${entry.method} ${entry.path}`));
  expect(records.failedRequests.map((entry) => entry.category)).toEqual(
    abortedPosts.map(() => NETWORK_FAILURE),
  );
  expect(records.pageErrors).toEqual([]);
  const resource404Count = records.consoleErrors.filter(
    (entry) => entry === CONSOLE_RESOURCE_404,
  ).length;
  expect(resource404Count).toBe(api404s.length);
  expect(
    records.consoleErrors.filter((entry) => entry === CONSOLE_NET_FAILURE),
  ).toEqual(abortedPosts.map(() => CONSOLE_NET_FAILURE));
  expect(
    records.consoleErrors.filter((entry) => entry === CONSOLE_UNEXPECTED),
  ).toEqual([]);
}

/** Boolean surface predicate with a category-only failure label: a failing
 * assertion never prints the surface text or the searched value. */
async function surfaceContains(page, testId, needle) {
  const text = (await page.getByTestId(testId).textContent()) ?? "";
  return text.includes(needle);
}

async function expectSurfaceAbsent(page, testId, needle, label) {
  expect(await surfaceContains(page, testId, needle), label).toBe(false);
}

test("T04 diagnostic path oracle removes dynamic resource identifiers", () => {
  const cases = [
    {
      id: "app_t04_secret_identifier",
      url: "https://example.test/controlled/s01/api/queries/applications/app_t04_secret_identifier/workspace",
      label: "diagnostic path: no application id",
    },
    {
      id: "work_t04_secret_identifier",
      url: "https://example.test/controlled/s01/api/queries/review-work-items/work_t04_secret_identifier",
      label: "diagnostic path: no work id",
    },
    {
      id: "supplement_request_t04_secret_identifier",
      url: "https://example.test/controlled/s02/api/queries/supplement-requests/supplement_request_t04_secret_identifier",
      label: "diagnostic path: no request id",
    },
  ];
  for (const item of cases) {
    expect(sanitizedDiagnosticPath(item.url).includes(item.id), item.label).toBe(
      false,
    );
  }
});

/** Starts one T04 server plus the separate Reviewer/Integrator contexts and
 * returns the running flow handles. */
async function startT04Flow(browser, { viewport, serverOptions = {} }) {
  const server = await startServer(serverOptions);
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
    (
      (await integrator
        .getByTestId("integrator-projection-request-id")
        .textContent()) ?? ""
    ).includes(requestId),
    "integrator-projection: bound request id rendered",
  ).toBe(true);
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

/** Reviewer authoritative refetch and the exact post-fulfillment
 * assertions.  The API reads remain the authority; the visible banner and
 * history must then carry that exact current run and route, and the history
 * must mark that run current exactly once.  Restricted run/attachment ids
 * are only ever compared through boolean predicates with category-only
 * labels. */
async function reviewerConverges(flow) {
  const { reviewer, server } = flow;
  await reviewer.getByTestId("supplement-reload-button").click();
  await expect(reviewer.getByTestId("review-supplement-converged")).toBeVisible({
    timeout: 20000,
  });
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
  const currentRunId = route.body.current_run_id;
  expect(typeof currentRunId).toBe("string");
  expect(currentRunId.length).toBeGreaterThan(0);
  expect(route.body.phase).toBe("Manual Review");

  // The visible banner carries that exact current run and route.
  const bannerText =
    (await reviewer.getByTestId("review-supplement-converged").textContent()) ?? "";
  expect(bannerText.includes("证据修订 2"), "converged banner: revision").toBe(true);
  expect(
    bannerText.includes(currentRunId),
    "converged banner: exact current run id",
  ).toBe(true);
  expect(
    bannerText.includes(route.body.route),
    "converged banner: exact current route",
  ).toBe(true);

  // The visible history marks that exact run current exactly once and the
  // predecessor run non-current.
  const historyRunIds = await reviewer.evaluate(() =>
    Array.from(
      document.querySelectorAll('[data-testid="review-history-runs"] li'),
    ).map((li) => {
      const parts = (li.textContent ?? "").split(" · ");
      return { runId: parts[0] ?? "", current: parts.includes("当前") };
    }),
  );
  expect(
    historyRunIds.filter((entry) => entry.current).length,
    "history runs: exactly one current",
  ).toBe(1);
  expect(
    historyRunIds.some(
      (entry) => entry.current && entry.runId === currentRunId,
    ),
    "history runs: the current run is the authoritative run",
  ).toBe(true);
  expect(
    historyRunIds.filter((entry) => entry.runId === currentRunId).length,
    "history runs: authoritative run appears exactly once",
  ).toBe(1);
  expect(
    historyRunIds.filter((entry) => !entry.current).length,
    "history runs: predecessor run is non-current",
  ).toBe(1);

  // v1 non-current, v2 current (server authority).
  expect(history.body.current_run_id).toBe(currentRunId);
  expect(history.body.runs.map((run) => run.current)).toEqual([false, true]);
  expect(history.body.attachment_versions.map((item) => item.version)).toEqual([
    1,
    2,
  ]);
  expect(history.body.attachment_versions.map((item) => item.current)).toEqual([
    false,
    true,
  ]);
  const attachmentsText =
    (await reviewer.getByTestId("review-history-attachments").textContent()) ?? "";
  expect(
    attachmentsText.match(/· 当前/g)?.length === 1,
    "attachment history: exactly one current version",
  ).toBe(true);
  expect(
    attachmentsText.match(/· 非当前/g)?.length === 1,
    "attachment history: exactly one non-current version",
  ).toBe(true);
}

/** The exact expected Reviewer 404 set: the invalidated work item's
 * workspace existence-hides once at acceptance and once at the reviewer's
 * authoritative reload. */
function expectedReviewerWorkspace404s() {
  return [
    {
      method: "GET",
      path: DIAGNOSTIC_WORKSPACE_PATH,
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
      await expectViewportIntegrity(
        reviewer,
        [
          "review-panel",
          "review-supplement-request",
          "supplement-reload-button",
          "review-history-attachments",
        ],
        viewport,
      );
      await expectViewportIntegrity(
        integrator,
        [
          "integrator-projection",
          "integrator-envelope-input",
          "integrator-reload-button",
          "integrator-submit-button",
          "integrator-receipt",
        ],
        viewport,
      );
      expectExactDiagnostics(diagnostics, {
        api404s: expectedReviewerWorkspace404s(),
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
      expect(integratorUrl.match(/[0-9a-f]{64}/) === null).toBe(true);
      expect(
        integratorUrl.includes("envelope"),
        "integrator url: no envelope marker",
      ).toBe(false);
      expect(
        integratorUrl.includes("idempotency"),
        "integrator url: no idempotency marker",
      ).toBe(false);
      expect(
        integratorUrl.includes(flow.applicationId),
        "integrator url: no reviewer application identifier",
      ).toBe(false);
      // No foreign facts in either panel surface; boolean predicates with
      // category-only labels never print restricted values.
      await expectSurfaceAbsent(
        reviewer,
        "review-panel",
        "LSVAA4182N2444555",
        "review-panel: no raw lease value",
      );
      await expectSurfaceAbsent(
        integrator,
        "integrator-panel",
        "LSVAA4182N2444555",
        "integrator-panel: no raw lease value",
      );
      expect(
        integratorUrl.includes("LSVAA4182N2444555"),
        "integrator url: no raw lease value",
      ).toBe(false);
    } finally {
      await stopT04Flow(flow);
    }
  });
}

/** Viewport oracle: the expected viewport is asserted exactly, the document
 * scroll extents are recorded, and every targeted surface's leaf-control /
 * rendered-text rectangle is checked against the viewport and its clipping
 * ancestor.  Assertion messages carry stable test IDs/categories only —
 * never rendered text. */
async function expectViewportIntegrity(page, testIds, viewport) {
  const facts = await page.evaluate((ids) => {
    const root = document.documentElement;
    const documentRect = {
      x: 0,
      y: 0,
      w: root.clientWidth,
      h: root.scrollHeight,
    };
    const visible = (element) => {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return (
        style.display !== "none" &&
        style.visibility !== "hidden" &&
        rect.width > 0 &&
        rect.height > 0
      );
    };
    const documentBox = (rect) => ({
      x: rect.x,
      y: rect.y + window.scrollY,
      w: rect.width,
      h: rect.height,
    });
    const clips = (value) => ["auto", "scroll", "hidden", "clip"].includes(value);
    const clippingAncestor = (element) => {
      let current = element;
      while (current !== null && current !== root) {
        const style = getComputedStyle(current);
        if (clips(style.overflowX) || clips(style.overflowY)) {
          return documentBox(current.getBoundingClientRect());
        }
        current = current.parentElement;
      }
      return documentRect;
    };
    const counts = ids.map((id) => ({
      id,
      count: document.querySelectorAll(`[data-testid="${id}"]`).length,
    }));
    const targets = [];
    const entries = [];
    const seenControls = new Set();
    const seenText = new Set();
    const controlSelector =
      'button, input, textarea, select, a[href], [role="button"], [tabindex]:not([tabindex="-1"])';

    for (const id of ids) {
      const element = document.querySelector(`[data-testid="${id}"]`);
      if (element === null || !visible(element)) continue;
      const style = getComputedStyle(element);
      targets.push({
        id,
        ...documentBox(element.getBoundingClientRect()),
        scrollWidth: element.scrollWidth,
        clientWidth: element.clientWidth,
        scrollHeight: element.scrollHeight,
        clientHeight: element.clientHeight,
        overflowX: style.overflowX,
        overflowY: style.overflowY,
      });

      const controls = [
        ...(element.matches(controlSelector) ? [element] : []),
        ...element.querySelectorAll(controlSelector),
      ];
      for (const control of controls) {
        if (seenControls.has(control) || !visible(control)) continue;
        seenControls.add(control);
        entries.push({
          node: control,
          label: `${id}:control:${entries.length}:${control.getAttribute("data-testid") ?? control.tagName.toLowerCase()}`,
          rects: [documentBox(control.getBoundingClientRect())],
          clip: clippingAncestor(control.parentElement),
        });
      }

      const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
      while (walker.nextNode()) {
        const node = walker.currentNode;
        const parent = node.parentElement;
        if (
          seenText.has(node) ||
          parent === null ||
          node.textContent.trim() === "" ||
          !visible(parent)
        ) {
          continue;
        }
        seenText.add(node);
        const range = document.createRange();
        range.selectNodeContents(node);
        const rects = [...range.getClientRects()]
          .filter((rect) => rect.width > 0 && rect.height > 0)
          .map(documentBox);
        if (rects.length > 0) {
          entries.push({
            node: parent,
            label: `${id}:text:${entries.length}`,
            rects,
            clip: clippingAncestor(parent),
          });
        }
      }
    }

    const overlaps = [];
    for (let left = 0; left < entries.length; left += 1) {
      for (let right = left + 1; right < entries.length; right += 1) {
        const a = entries[left];
        const b = entries[right];
        if (
          a.node === b.node ||
          a.node.contains(b.node) ||
          b.node.contains(a.node)
        ) {
          continue;
        }
        const intersects = a.rects.some((first) =>
          b.rects.some(
            (second) =>
              first.x < second.x + second.w - 0.5 &&
              second.x < first.x + first.w - 0.5 &&
              first.y < second.y + second.h - 0.5 &&
              second.y < first.y + first.h - 0.5,
          ),
        );
        if (intersects) overlaps.push(`${a.label}|${b.label}`);
      }
    }

    return {
      layout: {
        innerWidth: window.innerWidth,
        innerHeight: window.innerHeight,
        scrollWidth: root.scrollWidth,
        clientWidth: root.clientWidth,
      },
      counts,
      targets,
      leaves: entries.flatMap((entry) =>
        entry.rects.map((rect, index) => ({
          label: `${entry.label}:${index}`,
          ...rect,
          clip: entry.clip,
        })),
      ),
      overlaps,
    };
  }, testIds);

  expect(facts.layout.innerWidth).toBe(viewport.width);
  expect(facts.layout.innerHeight).toBe(viewport.height);
  expect(facts.layout.scrollWidth).toBeLessThanOrEqual(facts.layout.clientWidth);
  for (const target of facts.counts) {
    expect(target.count, `${target.id}: exactly one visible target`).toBe(1);
  }
  expect(facts.targets.length, "viewport oracle: every target visible").toBe(
    testIds.length,
  );
  for (const target of facts.targets) {
    if (target.overflowX !== "visible") {
      expect(target.scrollWidth, `${target.id}: horizontal content fit`).toBeLessThanOrEqual(
        target.clientWidth + 1,
      );
    }
    if (["hidden", "clip"].includes(target.overflowY)) {
      expect(target.scrollHeight, `${target.id}: vertical content fit`).toBeLessThanOrEqual(
        target.clientHeight + 1,
      );
    }
    expect(target.x, `${target.id}: document left edge`).toBeGreaterThanOrEqual(-1);
    expect(target.x + target.w, `${target.id}: document right edge`).toBeLessThanOrEqual(
      facts.layout.innerWidth + 1,
    );
  }
  for (const leaf of facts.leaves) {
    expect(leaf.x, `${leaf.label}: clipping left`).toBeGreaterThanOrEqual(
      leaf.clip.x - 1,
    );
    expect(leaf.y, `${leaf.label}: clipping top`).toBeGreaterThanOrEqual(
      leaf.clip.y - 1,
    );
    expect(leaf.x + leaf.w, `${leaf.label}: clipping right`).toBeLessThanOrEqual(
      leaf.clip.x + leaf.clip.w + 1,
    );
    expect(leaf.y + leaf.h, `${leaf.label}: clipping bottom`).toBeLessThanOrEqual(
      leaf.clip.y + leaf.clip.h + 1,
    );
  }
  expect(facts.overlaps, "viewport oracle: unrelated leaves do not overlap").toEqual(
    [],
  );
}

test("T04 viewport oracle rejects clipped text and overlapping leaf controls", async ({
  page,
}) => {
  const viewport = { width: 390, height: 844 };
  await page.setViewportSize(viewport);
  await page.setContent(`
    <style>
      #clipped { width: 80px; height: 20px; overflow: hidden; white-space: nowrap; }
      #overlap { position: relative; width: 200px; height: 80px; }
      #overlap button { position: absolute; inset: 10px auto auto 10px; }
    </style>
    <p id="clipped" data-testid="oracle-clipped">deliberately clipped operator status</p>
    <div id="overlap" data-testid="oracle-overlap">
      <button data-testid="oracle-first">First</button>
      <button data-testid="oracle-second">Second</button>
    </div>
  `);
  await expect(
    expectViewportIntegrity(page, ["oracle-clipped"], viewport),
  ).rejects.toThrow();
  await expect(
    expectViewportIntegrity(page, ["oracle-overlap"], viewport),
  ).rejects.toThrow();
});

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
    let firstRaw = null;
    let retryRaw = null;
    let retryKey = null;
    let conflictBodyDigest = null;
    const rawDigest = (value) => sha256(Buffer.from(value, "utf8"));
    await integrator.route(
      "**/api/commands/submit-attachment-version",
      async (route) => {
        submitCount += 1;
        const postRaw = route.request().postData() ?? "";
        const post = route.request().postDataJSON();
        if (submitCount === 1) {
          // The server commits the progress receipt, then the response is
          // lost at the transport: the panel must hold the exact command
          // and key for replay.  Only digests/booleans are retained so a
          // failure can never print the raw envelope or the key.
          firstKey = post.idempotency_key;
          firstRaw = rawDigest(postRaw);
          await route.fetch();
          await route.abort("connectionrefused");
        } else if (submitCount === 2) {
          retryRaw = rawDigest(postRaw);
          retryKey = post.idempotency_key;
          await route.continue();
        } else if (submitCount === 3) {
          // Same semantic key with a different fingerprint: the domain
          // answers a definitive conflict receipt with no second effect.
          conflictBodyDigest = rawDigest(postRaw);
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

    // Baseline after the single committed attempt (the lost response): the
    // progress effect advanced the attachment evidence once.
    const baselineHistory = await api(
      reviewer,
      "GET",
      `/controlled/s01/api/queries/applications/${flow.applicationId}/history`,
    );
    expect(baselineHistory.body.runs).toHaveLength(1);
    expect(baselineHistory.body.attachment_versions).toHaveLength(2);
    expect(baselineHistory.body.runs[0].evidence_revision).toBe(1);
    expect(baselineHistory.body.attachment_versions[1].evidence_revision).toBe(2);

    // Exact replay: byte-identical raw wire body and the same semantic key,
    // no third POST.  Comparisons are boolean/digest only.
    await integrator.getByRole("button", { name: "重试" }).click();
    await expect(
      integrator.getByTestId("integrator-disposition-announcement"),
    ).toHaveText("附件版本已接受（重放原回执）", { timeout: 10000 });
    await expect(integrator.getByTestId("integrator-receipt-replayed")).toHaveText(
      "true",
    );
    expect(submitCount).toBe(2);
    expect(retryRaw !== null && retryRaw === firstRaw).toBe(true);
    expect(retryKey !== null && retryKey === firstKey).toBe(true);
    expect(typeof firstKey).toBe("string");
    expect(firstKey.length).toBeGreaterThan(0);
    // One committed progress effect only: the replay advanced nothing —
    // evidence revision, runs and attachments are byte-identical to the
    // baseline, and the next request revision advanced exactly once.
    const afterReplay = await integratorProjection(integrator, requestId);
    expect(afterReplay.next_request_progress_revision).toBe(2);
    expect(afterReplay.status).toBe("open");
    const historyAfterReplay = await api(
      reviewer,
      "GET",
      `/controlled/s01/api/queries/applications/${flow.applicationId}/history`,
    );
    expect(historyAfterReplay.body.runs).toHaveLength(1);
    expect(historyAfterReplay.body.attachment_versions).toHaveLength(2);
    expect(historyAfterReplay.body.evidence_revision).toBe(
      baselineHistory.body.evidence_revision,
    );
    expect(historyAfterReplay.body.runs).toEqual(baselineHistory.body.runs);
    expect(historyAfterReplay.body.attachment_versions).toEqual(
      baselineHistory.body.attachment_versions,
    );

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
    expect(
      conflictBodyDigest !== null && conflictBodyDigest !== firstRaw,
    ).toBe(true);
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
      api404s: expectedReviewerWorkspace404s(),
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

    // Request-bound version rejection: the lineage is canonical and
    // structurally valid (attachment_version == predecessor + 1) but the
    // pair is inconsistent with the server's expected predecessor version,
    // so the request-context gate rejects it.
    const versionSubmission = structuredClone(baseSubmission);
    versionSubmission.attachment_lineage.predecessor_attachment_version =
      openProjection.expected_predecessor_attachment_version + 1;
    versionSubmission.attachment_lineage.attachment_version =
      openProjection.expected_predecessor_attachment_version + 2;
    const versionAnnouncement = await integratorSubmitsEnvelope(
      integrator,
      versionSubmission,
    );
    expect(versionAnnouncement).toBe(
      "附件版本被拒绝（intake.request_context_mismatch）",
    );
    await expect(
      integrator.getByTestId("integrator-receipt-disposition"),
    ).toHaveText("rejected");

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
      api404s: expectedReviewerWorkspace404s(),
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
    await expectSurfaceAbsent(
      integrator,
      "integrator-projection-error",
      "S02_NOT_FOUND",
      "integrator-projection-error: sanitized copy only",
    );

    // A valid projection loads and still leaks no protected fact.
    await integratorOpensProjection(integrator, server, requestId);
    const protectedFacts = [
      flow.applicationId,
      "finding_",
      "run_",
      "LSVAA4182N2444555",
      "requester_subject",
      "snapshot_",
    ];
    for (const fact of protectedFacts) {
      await expectSurfaceAbsent(
        integrator,
        "integrator-panel",
        fact,
        "integrator-panel: no reviewer/application/finding/run/snapshot/raw facts",
      );
    }

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
          path: DIAGNOSTIC_WORKSPACE_PATH,
          status: 404,
        },
        {
          method: "GET",
          path: DIAGNOSTIC_S02_REQUEST_PATH,
          status: 404,
        },
        {
          method: "GET",
          path: DIAGNOSTIC_S02_REQUEST_PATH,
          status: 404,
        },
      ],
    });
  } finally {
    await stopT04Flow(flow);
  }
});

test("T04 React: a genuinely expired retained s02 session is existence-hiding and suppresses cached protected facts", async ({
  browser,
}) => {
  test.setTimeout(120_000);
  // The harness-owned clock file backs the session clock; the S02 session
  // TTL is shortened so the retained token can actually expire.
  const flow = await startT04Flow(browser, {
    viewport: { width: 1280, height: 800 },
    serverOptions: { clockSeconds: 30 },
  });
  const { reviewer, integrator, server, diagnostics } = flow;
  try {
    expect(flow.server.clockPath).toBeTruthy();
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

    // The retained token is present and valid.
    const cookieNames = (await flow.integratorContext.cookies()).map(
      (cookie) => cookie.name,
    );
    expect(cookieNames).toContain("s02_session");
    const validProjection = await integratorProjection(integrator, requestId);
    expect(validProjection.request_id).toBe(requestId);

    // Advance the server clock beyond the short TTL; the cookie is retained
    // but the identity is genuinely expired.
    const clockBaseline = Math.floor(Date.now() / 1000);
    fs.writeFileSync(
      flow.server.clockPath,
      String(clockBaseline + 31),
      "ascii",
    );
    await integrator.getByTestId("integrator-reload-button").click();
    await expect(integrator.getByTestId("integrator-projection-error")).toHaveText(
      "请求未找到或无权访问",
      { timeout: 10000 },
    );
    expect(
      (await flow.integratorContext.cookies()).some(
        (cookie) => cookie.name === "s02_session",
      ),
    ).toBe(true);
    // The cached request/protected facts are suppressed from the rendered
    // surface; submission is gone.
    await expectSurfaceAbsent(
      integrator,
      "integrator-panel",
      requestId,
      "integrator-panel: no request identifier after expiry",
    );
    await expectSurfaceAbsent(
      integrator,
      "integrator-panel",
      flow.applicationId,
      "integrator-panel: no reviewer application identifier after expiry",
    );
    await expectSurfaceAbsent(
      integrator,
      "integrator-panel",
      "LSVAA4182N2444555",
      "integrator-panel: no raw lease value after expiry",
    );
    expect(
      await integrator.getByRole("button", { name: "提交附件版本" }).isHidden(),
    ).toBe(true);

    // The same retained cookie answers the query seam with the sanitized 404.
    const expiredRead = await api(
      integrator,
      "GET",
      `/controlled/s02/api/queries/supplement-requests/${encodeURIComponent(requestId)}`,
    );
    expect(expiredRead.status).toBe(404);
    expect(expiredRead.body).toEqual({ detail: { error: "S02_NOT_FOUND" } });

    expectExactDiagnostics(diagnostics, {
      api404s: [
        {
          method: "GET",
          path: DIAGNOSTIC_WORKSPACE_PATH,
          status: 404,
        },
        {
          method: "GET",
          path: DIAGNOSTIC_S02_REQUEST_PATH,
          status: 404,
        },
        {
          method: "GET",
          path: DIAGNOSTIC_S02_REQUEST_PATH,
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
      api404s: expectedReviewerWorkspace404s(),
    });
  } finally {
    await stopT04Flow(flow);
  }
});
