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
const CREDENTIAL = "synthetic-s03-browser-credential";
const TENANT = "tenant-s03-browser";
const SOURCE = "registered-s03-browser-source";
const SUBJECT = "registered-s03-browser-reviewer";
const RAW_VALUE = "SYNTHETIC-S03-RAW-VIN";
const NOTE = "SYNTHETIC-S03-PRIVATE-NOTE";
const ARTIFACT_ROOT = "/tmp/xiaopeng-task4-s03-browser";

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
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "xiaopeng-s03-browser-runtime-"));
  const objectRoot = path.join(root, "objects");
  fs.mkdirSync(objectRoot);
  const page = Buffer.from([
    0xff, 0xd8, 0xff, 0xc0, 0x00, 0x07, 0x08, 0x00, 0x01, 0x00, 0x01, 0xff, 0xd9,
  ]);
  const result = {
    per_image_results: [
      {
        image_path: "synthetic-page.jpg",
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
          workload_identity_id: "s03-browser-workload",
          adapter_id: "s03-browser-detection-adapter",
          adapter_version: "1",
          source_shape: "ocr-detection/unversioned",
          producer_family: "s03-browser-ocr",
          enabled: true,
        },
      ],
      objects: [
        {
          tenant_id: TENANT,
          source_system_id: SOURCE,
          object_ref: "s03-browser-result-object",
          media_type: "application/json",
          file: "result.json",
        },
        {
          tenant_id: TENANT,
          source_system_id: SOURCE,
          object_ref: "s03-browser-page-object",
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
    statePath: path.join(root, "target.sqlite3"),
    submission: {
      envelope_id: "s03-browser-envelope-1",
      schema_version: "1.0.0",
      semantic_version: "1.0.0",
      command_type: "submit_observation_result",
      upstream_application_ref: "s03-browser-upstream-1",
      stream_id: "s03-browser-stream-1",
      source_revision: 1,
      predecessor_revision: null,
      must_understand: [],
      workload_identity_id: "s03-browser-workload",
      document_binding: {
        source_document_ref: "s03-browser-document-1",
        document_type: "motor_vehicle_registration_certificate",
        document_role: "registration_certificate",
      },
      result_object: descriptor("s03-browser-result-object", "application/json", resultBytes),
      attachments: [
        {
          source_attachment_ref: "s03-browser-attachment-1",
          page_ref: "s03-browser-page-1",
          page_ordinal: 1,
          source_name_sha256: sha256(Buffer.from("synthetic-page.jpg")),
          object: descriptor("s03-browser-page-object", "image/jpeg", page),
        },
      ],
      producer: {
        producer_id: "s03-browser-producer",
        producer_family: "s03-browser-ocr",
        task_id: "registration-extraction",
        task_version: "1",
        run_id: "s03-browser-run-1",
        model_id: "s03-browser-model",
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

async function startServer({ fixture = createSourceFixture(), appTarget, extraEnv = {} } = {}) {
  const port = await reservePort();
  const output = [];
  const child = spawn(
    PYTHON,
    [
      "-m",
      "uvicorn",
      appTarget || "task4_consistency.web.app:create_s02_test_app",
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
        PYTHONPYCACHEPREFIX: "/tmp/xiaopeng-task4-s03-browser-pycache",
        TASK4_S01_STATE_PATH: fixture.statePath,
        TASK4_S02_TEST_STATE_PATH: fixture.statePath,
        TASK4_S02_TEST_REGISTRY_PATH: fixture.registryPath,
        TASK4_S02_TEST_OBJECT_ROOT: fixture.objectRoot,
        TASK4_S02_CREDENTIAL: CREDENTIAL,
        TASK4_S02_SUBJECT: SUBJECT,
        TASK4_S02_TENANT_ID: TENANT,
        TASK4_S02_SOURCE_SYSTEM_ID: SOURCE,
        TASK4_S02_TEST_BACKGROUND_ENABLED: "1",
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
  while (Date.now() < deadline && child.exitCode === null) {
    try {
      if ((await fetch(`${baseURL}/api/health`)).ok) {
        return { ...fixture, baseURL, child, output };
      }
    } catch (_) {}
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  child.kill("SIGKILL");
  throw new Error(`S03 browser server did not start: ${output.join("")}`);
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

async function openWorkbench(page, server) {
  await page.setExtraHTTPHeaders({ Authorization: `Bearer ${CREDENTIAL}` });
  const response = await page.goto(`${server.baseURL}/controlled/s02`, { waitUntil: "networkidle" });
  expect(response.status()).toBe(200);
  expect(response.headers()["cache-control"]).toContain("no-store");
}

async function submitObservation(page, submission, idempotencyKey) {
  return page.evaluate(
    async ({ body, key }) => {
      const response = await fetch("/controlled/s02/api/commands/submit", {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ idempotency_key: key, submission: body }),
      });
      if (!response.ok) throw new Error(`submit failed: ${response.status}`);
      return response.json();
    },
    { body: submission, key: idempotencyKey },
  );
}

async function waitForQueueItem(page) {
  await expect
    .poll(async () => {
      await page.getByRole("button", { name: "Refresh queue" }).click();
      return page.getByTestId("queue-item").count();
    })
    .toBeGreaterThan(0);
  await expect(page.getByRole("button", { name: "Refresh queue" })).toBeEnabled();
}

async function expectNoLayoutFaults(page) {
  const faults = await page.evaluate(() => {
    const visible = (node) => {
      const style = getComputedStyle(node);
      const box = node.getBoundingClientRect();
      return style.display !== "none" && style.visibility !== "hidden" && box.width > 0 && box.height > 0;
    };
    const regions = [...document.querySelectorAll("[data-layout-region]")].filter(visible);
    const overlaps = [];
    for (let left = 0; left < regions.length; left += 1) {
      for (let right = left + 1; right < regions.length; right += 1) {
        const a = regions[left].getBoundingClientRect();
        const b = regions[right].getBoundingClientRect();
        if (a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top) {
          overlaps.push(`${regions[left].dataset.layoutRegion}:${regions[right].dataset.layoutRegion}`);
        }
      }
    }
    return {
      horizontalOverflow: document.documentElement.scrollWidth > window.innerWidth + 1,
      clipped: [...document.querySelectorAll("button, h1, h2, h3, label, dt, dd")]
        .filter(visible)
        .filter((node) => node.scrollWidth > node.clientWidth + 1)
        .map((node) => node.getAttribute("data-testid") || node.textContent.trim().slice(0, 40)),
      overlaps,
    };
  });
  expect(faults).toEqual({ horizontalOverflow: false, clipped: [], overlaps: [] });
}

async function installCommandFailureController(page) {
  await page.evaluate(() => {
    const fetchRequest = window.fetch.bind(window);
    window.__s03FailNextCommand = null;
    window.fetch = async (input, init) => {
      const url = typeof input === "string" ? input : input.url;
      const failure = window.__s03FailNextCommand;
      if (failure && url.includes(failure.path)) {
        window.__s03FailNextCommand = null;
        return new Response(JSON.stringify({ detail: failure.detail }), {
          status: failure.status,
          headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
        });
      }
      return fetchRequest(input, init);
    };
  });
}

async function failNextCommand(page, path, status, error, reasonCode) {
  await page.evaluate(
    (failure) => {
      window.__s03FailNextCommand = failure;
    },
    {
      path,
      status,
      detail: { error, ...(reasonCode ? { reason_code: reasonCode } : {}) },
    },
  );
}

async function expectPrivateViewCleared(page, ...identifiers) {
  await expect(page.getByTestId("queue-item")).toHaveCount(0);
  await expect(page.getByTestId("review-empty")).toBeVisible();
  const documentText = await page.locator("body").textContent();
  for (const identifier of identifiers) expect(documentText).not.toContain(identifier);
}

const test = base.extend({
  s03Server: async ({}, use) => {
    const server = await startServer();
    try {
      await use(server);
    } finally {
      await stopServer(server);
    }
  },
  browserErrors: async ({ page }, use) => {
    const errors = [];
    page.on("console", (message) => {
      if (message.type() === "error") errors.push(`console: ${message.text()}`);
    });
    page.on("pageerror", (error) => errors.push(`page: ${error.message}`));
    await use(errors);
    expect(errors).toEqual([]);
  },
});

test("reviewer completes one uncertain work item with minimized evidence", async ({
  page,
  s03Server,
  browserErrors,
}) => {
  void browserErrors;
  fs.mkdirSync(ARTIFACT_ROOT, { recursive: true });
  const apiResponses = [];
  page.on("response", (response) => {
    if (response.url().includes("/controlled/s02/api/")) apiResponses.push(response);
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await openWorkbench(page, s03Server);
  await expect(page.getByRole("heading", { name: "人工核实工作台" })).toBeVisible();

  await submitObservation(page, s03Server.submission, "s03-browser-happy-intake");
  await waitForQueueItem(page);
  await expect(page.getByTestId("machine-route")).toHaveText("machine-uncertain");
  await expect(page.getByTestId("finding-row")).toHaveCount(10);
  await expect(page.getByTestId("evidence-value")).toHaveText("[REDACTED]");

  await page.getByRole("button", { name: "Claim work item" }).click();
  await expect(page.getByTestId("lease-state")).toContainText("claimed");
  await page.getByLabel("Outcome").selectOption("confirmed");
  await page.getByLabel("Reason").selectOption("HUMAN_REVIEW_COMPLETED");
  await page.getByLabel("Reviewer note").fill(NOTE);
  await expect(page.getByTestId("note-count")).toHaveText(`${NOTE.length} / 2000`);
  await page.getByRole("button", { name: "Submit decision" }).click();

  await expect(page.getByTestId("human-route")).toHaveText("human-completed");
  await expect(page.getByTestId("decision-actor")).toHaveText(SUBJECT);
  await expect(page.getByTestId("decision-reason")).toHaveText("HUMAN_REVIEW_COMPLETED");
  await expect(page.getByTestId("note-metadata")).toContainText(`chars ${NOTE.length}`);
  await expect(page.getByLabel("Reviewer note")).toHaveValue("");
  await expect(page.getByTestId("queue-item")).toHaveCount(0);
  await expect(page.getByTestId("queue-empty")).toHaveText("No current uncertain work items");

  const forbidden = [
    RAW_VALUE,
    NOTE,
    CREDENTIAL,
    "s03-browser-upstream-1",
    "s03-browser-result-object",
    "s03-browser-page-object",
    "result.json",
    "synthetic-page.jpg",
  ];
  const domSurface = await page.evaluate(() =>
    JSON.stringify({
      text: document.body.innerText,
      values: [...document.querySelectorAll("input, textarea, select")].map(
        (element) => element.value,
      ),
      attributes: [...document.querySelectorAll("*")].flatMap((element) =>
        [...element.attributes].map((attribute) => attribute.value),
      ),
    }),
  );
  const responseSurface = (
    await Promise.all(apiResponses.map((response) => response.text().catch(() => "")))
  ).join("\n");
  for (const secret of forbidden) {
    expect(domSurface).not.toContain(secret);
    expect(responseSurface).not.toContain(secret);
  }
  await expectNoLayoutFaults(page);
  const desktop = await page.screenshot({
    path: path.join(ARTIFACT_ROOT, "s03-workbench-desktop.png"),
    fullPage: true,
  });
  expect(desktop.length).toBeGreaterThan(10_000);

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByTestId("human-route")).toBeVisible();
  await expectNoLayoutFaults(page);
  const mobile = await page.screenshot({
    path: path.join(ARTIFACT_ROOT, "s03-workbench-mobile.png"),
    fullPage: true,
  });
  expect(mobile.length).toBeGreaterThan(10_000);
});

test("reviewer renews, releases, and reclaims with a higher fence", async ({
  page,
  s03Server,
  browserErrors,
}) => {
  void browserErrors;
  await openWorkbench(page, s03Server);
  await submitObservation(page, s03Server.submission, "s03-browser-lease-intake");
  await waitForQueueItem(page);
  await expect(page.getByTestId("lease-state")).toContainText("unclaimed · fence 0");
  await page.getByRole("button", { name: "Claim work item" }).click();
  await expect(page.getByTestId("lease-state")).toContainText("claimed · fence 1");

  const initialFence = Number(
    (await page.getByTestId("lease-state").textContent()).match(/fence (\d+)/)[1],
  );
  await page.getByRole("button", { name: "Renew lease" }).click();
  await expect(page.getByTestId("lease-state")).toContainText("claimed");
  await page.getByRole("button", { name: "Release claim" }).click();
  await expect(page.getByTestId("lease-state")).toContainText("released");

  const releasedFence = Number(
    (await page.getByTestId("lease-state").textContent()).match(/fence (\d+)/)[1],
  );
  expect(releasedFence).toBeGreaterThanOrEqual(initialFence);
  await page.getByRole("button", { name: "Claim work item" }).click();
  await expect(page.getByTestId("lease-state")).toContainText("claimed");
  const reclaimedFence = Number(
    (await page.getByTestId("lease-state").textContent()).match(/fence (\d+)/)[1],
  );
  expect(reclaimedFence).toBeGreaterThan(releasedFence);
});

test("same-run batch preview is read only and commit renders every decision", async ({
  page,
  s03Server,
  browserErrors,
}) => {
  void browserErrors;
  await openWorkbench(page, s03Server);
  await submitObservation(page, s03Server.submission, "s03-browser-batch-intake");
  await waitForQueueItem(page);
  await expect(page.getByTestId("lease-state")).toContainText("unclaimed · fence 0");
  await page.getByRole("button", { name: "Claim work item" }).click();
  await expect(page.getByTestId("lease-state")).toContainText("claimed · fence 1");

  await page.getByRole("button", { name: "Select current run" }).click();
  await expect(page.locator('input[type="checkbox"]:checked')).toHaveCount(10);
  const leaseBeforePreview = await page.getByTestId("lease-state").textContent();
  await page.getByRole("button", { name: "Preview batch" }).click();
  await expect(page.getByTestId("batch-status")).toHaveText("10 findings ready to submit");
  await expect(page.getByTestId("human-route")).toHaveText("human-pending");
  await expect(page.getByTestId("lease-state")).toHaveText(leaseBeforePreview);

  await page.getByRole("button", { name: "Submit batch" }).click();
  await expect(page.getByTestId("batch-status")).toHaveText("10 findings accepted");
  await expect(page.getByTestId("human-route")).toHaveText("human-completed");
  await expect(page.getByTestId("decision-actor")).toHaveText(SUBJECT);
  await expect(page.getByTestId("decision-reason")).toHaveText("HUMAN_REVIEW_COMPLETED");
  await expect(page.getByTestId("queue-item")).toHaveCount(0);
});

test("lost submit response retries the same command and renders the replay", async ({
  page,
  s03Server,
  browserErrors,
}) => {
  void browserErrors;
  await openWorkbench(page, s03Server);
  await submitObservation(page, s03Server.submission, "s03-browser-replay-intake");
  await waitForQueueItem(page);
  await expect(page.getByTestId("lease-state")).toContainText("unclaimed · fence 0");
  await page.getByRole("button", { name: "Claim work item" }).click();

  await page.evaluate(() => {
    const fetchRequest = window.fetch.bind(window);
    let responseLost = false;
    window.fetch = async (input, init) => {
      const response = await fetchRequest(input, init);
      const url = typeof input === "string" ? input : input.url;
      if (!responseLost && url.includes("/review-work-items/") && url.endsWith("/submit")) {
        responseLost = true;
        throw new TypeError("synthetic response loss after acceptance");
      }
      return response;
    };
  });

  await page.getByRole("button", { name: "Submit decision" }).click();
  await expect(page.getByTestId("human-route")).toHaveText("human-pending");
  await expect(page.locator("#state-banner")).toContainText("Response lost");
  await page.getByRole("button", { name: "Retry submit" }).click();

  await expect(page.locator("#state-banner")).toHaveText("Decision replay confirmed");
  await expect(page.getByTestId("human-route")).toHaveText("human-completed");
  await expect(page.getByRole("button", { name: "Submit decision" })).toBeDisabled();
});

test("lost claim response retries against the authoritative own live lease", async ({
  page,
  s03Server,
  browserErrors,
}) => {
  void browserErrors;
  await openWorkbench(page, s03Server);
  await submitObservation(page, s03Server.submission, "s03-browser-claim-replay-intake");
  await waitForQueueItem(page);
  await expect(page.getByTestId("lease-state")).toContainText("unclaimed · fence 0");

  await page.evaluate(() => {
    const fetchRequest = window.fetch.bind(window);
    let responseLost = false;
    window.fetch = async (input, init) => {
      const response = await fetchRequest(input, init);
      const url = typeof input === "string" ? input : input.url;
      if (!responseLost && url.includes("/review-work-items/") && url.endsWith("/claim")) {
        responseLost = true;
        throw new TypeError("synthetic claim response loss after acceptance");
      }
      return response;
    };
  });

  await page.getByRole("button", { name: "Claim work item" }).click();
  await expect(page.getByTestId("lease-state")).toContainText("unclaimed · fence 0");
  await page.getByRole("button", { name: "Claim work item" }).click();
  await expect(page.getByTestId("lease-state")).toContainText("claimed · fence 1");
  await expect(page.locator("#state-banner")).not.toHaveText("Claimed by another reviewer");
  await expect(page.getByRole("button", { name: "Claim work item" })).toBeDisabled();

  await page.getByRole("button", { name: "Refresh queue" }).click();
  await expect(page.getByTestId("queue-item")).toContainText("fence 1");
  await expect(page.getByTestId("lease-state")).toContainText("claimed · fence 1");
});

test("lost renew and release responses retry the same command keys", async ({
  page,
  s03Server,
  browserErrors,
}) => {
  void browserErrors;
  await openWorkbench(page, s03Server);
  await submitObservation(page, s03Server.submission, "s03-browser-lease-replay-intake");
  await waitForQueueItem(page);
  await page.getByRole("button", { name: "Claim work item" }).click();
  await expect(page.getByTestId("lease-state")).toContainText("claimed · fence 1");

  await page.evaluate(() => {
    const fetchRequest = window.fetch.bind(window);
    window.__s03LeaseBodies = { renew: [], release: [] };
    const responseLost = { renew: false, release: false };
    window.fetch = async (input, init) => {
      const url = typeof input === "string" ? input : input.url;
      const action = url.endsWith("/renew") ? "renew" : url.endsWith("/release") ? "release" : null;
      if (!action) return fetchRequest(input, init);
      window.__s03LeaseBodies[action].push(JSON.parse(init.body));
      const response = await fetchRequest(input, init);
      if (!responseLost[action]) {
        responseLost[action] = true;
        throw new TypeError(`synthetic ${action} response loss after acceptance`);
      }
      return response;
    };
  });

  await page.getByRole("button", { name: "Renew lease" }).click();
  await expect(page.locator("#state-banner")).toContainText("Response lost");
  await page.getByRole("button", { name: "Renew lease" }).click();
  await expect(page.getByTestId("lease-state")).toContainText("claimed · fence 1");

  await page.getByRole("button", { name: "Release claim" }).click();
  await expect(page.locator("#state-banner")).toContainText("Response lost");
  await page.getByRole("button", { name: "Release claim" }).click();
  await expect(page.getByTestId("lease-state")).toContainText("released · fence 1");

  const bodies = await page.evaluate(() => window.__s03LeaseBodies);
  for (const action of ["renew", "release"]) {
    expect(bodies[action]).toHaveLength(2);
    expect(bodies[action][0].idempotency_key).toMatch(/^[0-9a-f-]{36}$/);
    expect(bodies[action][1]).toEqual(bodies[action][0]);
  }
});

test("stale, conflict, stopped, and unavailable commands render stable states", async ({
  page,
  s03Server,
  browserErrors,
}) => {
  void browserErrors;
  await openWorkbench(page, s03Server);
  await submitObservation(page, s03Server.submission, "s03-browser-errors-intake");
  await waitForQueueItem(page);
  await expect(page.getByTestId("lease-state")).toContainText("unclaimed · fence 0");
  await page.getByRole("button", { name: "Claim work item" }).click();
  await expect(page.getByTestId("lease-state")).toContainText("claimed · fence 1");
  const unchangedLease = await page.getByTestId("lease-state").textContent();
  await installCommandFailureController(page);

  await failNextCommand(page, "/renew", 409, "S03_STALE", "STALE_WORK_ITEM_CLAIM");
  await page.getByRole("button", { name: "Renew lease" }).click();
  await expect(page.locator("#state-banner")).toHaveAttribute("data-state", "stale");
  await expect(page.locator("#state-banner")).toContainText("refresh before retry");

  await failNextCommand(page, "/renew", 409, "S03_CONFLICT", "IDEMPOTENCY_KEY_CONFLICT");
  await page.getByRole("button", { name: "Renew lease" }).click();
  await expect(page.locator("#state-banner")).toHaveAttribute("data-state", "conflict");
  await expect(page.locator("#state-banner")).toHaveText(
    "Command conflicts with the current work item",
  );

  await failNextCommand(page, "/renew", 503, "S03_STOPPED", "RUNTIME_STOPPED");
  await page.getByRole("button", { name: "Renew lease" }).click();
  await expect(page.locator("#state-banner")).toHaveAttribute("data-state", "stopped");
  await expect(page.locator("#state-banner")).toHaveText("Verification writes are stopped");

  await failNextCommand(
    page,
    "/renew",
    503,
    "S03_STOPPED",
    "SOURCE_EVIDENCE_UNAVAILABLE",
  );
  await page.getByRole("button", { name: "Renew lease" }).click();
  await expect(page.locator("#state-banner")).toHaveText("Source evidence is unavailable");

  await failNextCommand(page, "/renew", 503, "S03_UNAVAILABLE", "AUDIT_UNAVAILABLE");
  await page.getByRole("button", { name: "Renew lease" }).click();
  await expect(page.locator("#state-banner")).toHaveAttribute("data-state", "unavailable");
  await expect(page.locator("#state-banner")).toHaveText("Verification service is unavailable");
  await expect(page.getByTestId("lease-state")).toHaveText(unchangedLease);
});

test("expired and cross-scope sessions clear every cached workspace value", async ({
  page,
  browserErrors,
}) => {
  void browserErrors;
  const expiryFixture = createSourceFixture();
  const clockPath = path.join(expiryFixture.root, "session-clock.txt");
  fs.writeFileSync(clockPath, "1000", "ascii");
  const expiryServer = await startServer({
    fixture: expiryFixture,
    appTarget: "tests.test_s03_http:create_expiring_s03_test_app",
    extraEnv: {
      TASK4_S03_TEST_SESSION_CLOCK_PATH: clockPath,
      TASK4_S03_TEST_SESSION_TTL_SECONDS: "2",
    },
  });
  try {
    await openWorkbench(page, expiryServer);
    const admission = await submitObservation(
      page,
      expiryServer.submission,
      "s03-browser-expiry-intake",
    );
    await waitForQueueItem(page);
    await expect(page.getByTestId("lease-state")).toContainText("unclaimed · fence 0");
    const workItemId = await page.locator("#work-item-id").textContent();
    fs.writeFileSync(clockPath, "1003", "ascii");
    await page.getByRole("button", { name: "Refresh queue" }).click();
    await expect(page.locator("#state-banner")).toContainText("Session expired");
    await expectPrivateViewCleared(page, admission.application_id, workItemId);
  } finally {
    await stopServer(expiryServer);
  }

  const scopeFixture = createSourceFixture();
  const demoCredential = "synthetic-s03-cross-scope-credential";
  const scopeServer = await startServer({
    fixture: scopeFixture,
    extraEnv: {
      TASK4_S01_DEMO_CREDENTIAL: demoCredential,
      TASK4_S01_DEMO_SUBJECT: "synthetic-cross-scope-reviewer",
    },
  });
  try {
    await openWorkbench(page, scopeServer);
    const admission = await submitObservation(
      page,
      scopeServer.submission,
      "s03-browser-cross-scope-intake",
    );
    await waitForQueueItem(page);
    await expect(page.getByTestId("lease-state")).toContainText("unclaimed · fence 0");
    const workItemId = await page.locator("#work-item-id").textContent();
    const crossScopeResponse = await fetch(`${scopeServer.baseURL}/controlled/s01/api/session`, {
      method: "POST",
      headers: { Authorization: `Bearer ${demoCredential}` },
    });
    expect(crossScopeResponse.status).toBe(204);
    const crossScopeCookie = crossScopeResponse.headers.get("set-cookie");
    const crossScopeToken = crossScopeCookie.match(/s01_session=([^;]+)/)[1];
    await page.context().addCookies([
      {
        name: "s02_session",
        value: crossScopeToken,
        url: scopeServer.baseURL,
        httpOnly: true,
        sameSite: "Strict",
      },
    ]);
    await page.getByRole("button", { name: "Refresh queue" }).click();
    await expect(page.locator("#state-banner")).toContainText("scope unavailable");
    await expectPrivateViewCleared(page, admission.application_id, workItemId);
  } finally {
    await stopServer(scopeServer);
  }
});

test("loading, empty, keyboard, and note-boundary states stay operable", async ({
  page,
  s03Server,
  browserErrors,
}) => {
  void browserErrors;
  await page.route(
    "**/controlled/s02/api/queries/queue",
    async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 250));
      await route.continue();
    },
    { times: 1 },
  );
  await page.setExtraHTTPHeaders({ Authorization: `Bearer ${CREDENTIAL}` });
  const navigation = await page.goto(`${s03Server.baseURL}/controlled/s02`, {
    waitUntil: "domcontentloaded",
  });
  expect(navigation.status()).toBe(200);
  await expect(page.locator("#status")).toHaveText("Loading queue");
  await expect(page.locator("#queue-body")).toHaveAttribute("aria-busy", "true");
  await expect(page.getByTestId("queue-empty")).toHaveText("No current uncertain work items");
  await expect(page.locator("#queue-body")).toHaveAttribute("aria-busy", "false");

  await submitObservation(page, s03Server.submission, "s03-browser-boundary-intake");
  await waitForQueueItem(page);
  await expect(page.getByTestId("lease-state")).toContainText("unclaimed · fence 0");
  await page.getByRole("button", { name: "Claim work item" }).click();
  await expect(page.getByTestId("lease-state")).toContainText("claimed · fence 1");
  await page.getByTestId("queue-item").focus();
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("evidence-value")).toHaveText("[REDACTED]");

  await page.getByLabel("Reviewer note").evaluate((element) => {
    element.value = "x".repeat(2001);
    element.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await expect(page.getByTestId("note-count")).toHaveText("2001 / 2000");
  await page.getByRole("button", { name: "Submit decision" }).click();
  await expect(page.locator("#state-banner")).toHaveText(
    "Reviewer note exceeds 2000 characters",
  );
  await expect(page.locator("#state-banner")).toBeFocused();
  await expect(page.getByTestId("human-route")).toHaveText("human-pending");

  await page.getByLabel("Reviewer note").fill("x".repeat(2000));
  await expect(page.getByTestId("note-count")).toHaveText("2000 / 2000");
  await expect(page.getByLabel("Reviewer note")).toHaveAttribute("maxlength", "2000");
  await page.getByLabel("Reviewer note").fill("");
});
