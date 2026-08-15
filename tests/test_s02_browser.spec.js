const { spawn } = require("node:child_process");
const { once } = require("node:events");
const crypto = require("node:crypto");
const fs = require("node:fs");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");

const { expect, test: base } = require("@playwright/test");

const ROOT = path.resolve(__dirname, "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");
const CREDENTIAL = "synthetic-s02-browser-credential";
const TENANT = "tenant-browser";
const SOURCE = "registered-browser-source";
const ARTIFACT_ROOT = "/tmp/xiaopeng-task4-s02-browser";
const RAW_VALUE = "SYNTHETIC-BROWSER-RAW-VIN";

function sha256(value) {
  return crypto.createHash("sha256").update(value).digest("hex");
}

function descriptor(ref, mediaType, content) {
  return {
    controlled_object_ref: ref,
    media_type: mediaType,
    size_bytes: content.length,
    sha256: sha256(content),
  };
}

function createSourceFixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "xiaopeng-s02-browser-runtime-"));
  const objectRoot = path.join(root, "objects");
  fs.mkdirSync(objectRoot);
  const page = Buffer.from([0xff, 0xd8, 0xff, 0xc0, 0x00, 0x07, 0x08, 0x00, 0x01, 0x00, 0x01, 0xff, 0xd9]);
  const result = {
    per_image_results: [
      {
        image_path: "page.jpg",
        image_size: { width: 1, height: 1 },
        detections: [
          {
            bbox: [0, 0, 1, 1],
            class_id: 1,
            class_name: "vehicle_identifier",
            confidence: 0.93,
            field_key: "vin",
            ocr_text: RAW_VALUE,
            value: RAW_VALUE,
          },
        ],
      },
    ],
  };
  const resultBytes = Buffer.from(JSON.stringify(result), "utf8");
  fs.writeFileSync(path.join(objectRoot, "result.json"), resultBytes);
  fs.writeFileSync(path.join(objectRoot, "page.jpg"), page);
  const registryPath = path.join(root, "registry.json");
  fs.writeFileSync(
    registryPath,
    JSON.stringify({
      schema_version: "s02-runtime-registry/1",
      sources: [
        {
          tenant_id: TENANT,
          source_system_id: SOURCE,
          workload_identity_id: "browser-workload",
          adapter_id: "browser-detection-adapter",
          adapter_version: "1",
          source_shape: "ocr-detection/unversioned",
          producer_family: "browser-ocr",
          enabled: true,
        },
      ],
      objects: [
        {
          tenant_id: TENANT,
          source_system_id: SOURCE,
          object_ref: "browser-result-object",
          media_type: "application/json",
          file: "result.json",
        },
        {
          tenant_id: TENANT,
          source_system_id: SOURCE,
          object_ref: "browser-page-object",
          media_type: "image/jpeg",
          file: "page.jpg",
        },
      ],
    }),
  );
  return {
    root,
    objectRoot,
    registryPath,
    pageHash: sha256(page),
    submission: {
      envelope_id: "browser-envelope-1",
      schema_version: "1.0.0",
      semantic_version: "1.0.0",
      command_type: "submit_observation_result",
      upstream_application_ref: "browser-upstream-1",
      stream_id: "browser-stream-1",
      source_revision: 1,
      predecessor_revision: null,
      must_understand: [],
      workload_identity_id: "browser-workload",
      document_binding: {
        source_document_ref: "browser-document-1",
        document_type: "motor_vehicle_registration_certificate",
        document_role: "registration_certificate",
      },
      result_object: descriptor("browser-result-object", "application/json", resultBytes),
      attachments: [
        {
          source_attachment_ref: "browser-attachment-1",
          page_ref: "browser-page-1",
          page_ordinal: 1,
          source_name_sha256: sha256(Buffer.from("page.jpg")),
          object: descriptor("browser-page-object", "image/jpeg", page),
        },
      ],
      producer: {
        producer_id: "browser-producer",
        producer_family: "browser-ocr",
        task_id: "registration-extraction",
        task_version: "1",
        run_id: "browser-run-1",
        model_id: "browser-model",
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
    },
  };
}

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
  const fixture = createSourceFixture();
  const port = await reservePort();
  const statePath = path.join(fixture.root, "target.sqlite3");
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
        PYTHONPYCACHEPREFIX: "/tmp/xiaopeng-task4-s02-browser-pycache",
        TASK4_S01_STATE_PATH: statePath,
        TASK4_S02_TEST_STATE_PATH: statePath,
        TASK4_S02_TEST_REGISTRY_PATH: fixture.registryPath,
        TASK4_S02_TEST_OBJECT_ROOT: fixture.objectRoot,
        TASK4_S02_CREDENTIAL: CREDENTIAL,
        TASK4_S02_SUBJECT: "registered-browser-reviewer",
        TASK4_S02_TENANT_ID: TENANT,
        TASK4_S02_SOURCE_SYSTEM_ID: SOURCE,
        TASK4_S02_TEST_BACKGROUND_ENABLED: "1",
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
  while (Date.now() < deadline && child.exitCode === null) {
    try {
      if ((await fetch(`${baseURL}/api/health`)).ok) return { ...fixture, baseURL, child, output };
    } catch (_) {}
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  child.kill("SIGKILL");
  throw new Error(`S02 browser server did not start: ${output.join("")}`);
}

async function stopServer(server) {
  if (server.child.exitCode !== null) return;
  server.child.kill("SIGTERM");
  const exited = once(server.child, "exit");
  if ((await Promise.race([exited, new Promise((resolve) => setTimeout(resolve, 5_000, "timeout"))])) === "timeout") {
    server.child.kill("SIGKILL");
    await once(server.child, "exit");
  }
}

async function expectNoLayoutFaults(page) {
  const faults = await page.evaluate(() => ({
    horizontalOverflow: document.documentElement.scrollWidth > window.innerWidth + 1,
    clippedText: [...document.querySelectorAll("button, h1, h2, dt, dd, .badge")]
      .filter((node) => {
        const box = node.getBoundingClientRect();
        return box.width > 0 && box.height > 0 && node.scrollWidth > node.clientWidth + 1;
      })
      .map((node) => node.getAttribute("data-testid") || node.textContent.trim().slice(0, 40)),
  }));
  expect(faults).toEqual({ horizontalOverflow: false, clippedText: [] });
}

const test = base.extend({
  s02Server: async ({}, use) => {
    const server = await startServer();
    try {
      await use(server);
    } finally {
      await stopServer(server);
    }
  },
});

test("registered observation renders a minimized Reviewer workspace on desktop and mobile", async ({
  page,
  s02Server,
}) => {
  fs.mkdirSync(ARTIFACT_ROOT, { recursive: true });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.setExtraHTTPHeaders({ Authorization: `Bearer ${CREDENTIAL}` });
  const navigation = await page.goto(`${s02Server.baseURL}/controlled/s02`, {
    waitUntil: "networkidle",
  });
  expect(navigation.status()).toBe(200);
  expect(navigation.headers()["cache-control"]).toContain("no-store");
  await expect(page.getByRole("heading", { name: "人工核实工作台" })).toBeVisible();

  const receipt = await page.evaluate(async (submission) => {
    const response = await fetch("/controlled/s02/api/commands/submit", {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ idempotency_key: "browser-command-1", submission }),
    });
    if (!response.ok) throw new Error(`submit failed: ${response.status}`);
    return response.json();
  }, s02Server.submission);
  expect(receipt.disposition).toBe("accepted");

  await expect
    .poll(async () => {
      await page.getByRole("button", { name: "Refresh queue" }).click();
      return page.getByTestId("evidence-value").count();
    })
    .toBe(1);
  await expect(page.getByText("R-OBSERVED", { exact: true }).first()).toBeVisible();
  await expect(page.getByTestId("evidence-field")).toHaveText("vin");
  await expect(page.getByTestId("evidence-value")).toHaveText("[REDACTED]");
  await expect(page.getByTestId("evidence-page")).toHaveText("1");
  await expect(page.getByTestId("evidence-region")).toHaveText("region:1");
  await expect(page.getByTestId("evidence-producer")).toHaveText("browser-ocr / browser-producer");
  await expect(page.getByTestId("producer-run-id")).toHaveText("browser-run-1");
  await expect(page.getByTestId("source-receipt-id")).toHaveText(receipt.receipt_id);
  const body = await page.locator("body").innerText();
  for (const hiddenValue of [
    RAW_VALUE,
    s02Server.pageHash,
    "browser-result-object",
    "browser-page-object",
    "result.json",
    "page.jpg",
    "bbox:[0,0,1,1]",
  ]) {
    expect(body).not.toContain(hiddenValue);
  }
  expect(await page.locator("body").getAttribute("contenteditable")).not.toBe("true");

  await expectNoLayoutFaults(page);
  const desktop = await page.screenshot({
    path: path.join(ARTIFACT_ROOT, "s02-workbench-desktop.png"),
    fullPage: true,
  });
  expect(desktop.length).toBeGreaterThan(10_000);

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByTestId("evidence-value")).toBeVisible();
  await expectNoLayoutFaults(page);
  const mobile = await page.screenshot({
    path: path.join(ARTIFACT_ROOT, "s02-workbench-mobile.png"),
    fullPage: true,
  });
  expect(mobile.length).toBeGreaterThan(10_000);
});
