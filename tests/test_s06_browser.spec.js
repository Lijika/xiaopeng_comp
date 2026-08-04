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
