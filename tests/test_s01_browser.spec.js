const { spawn } = require("node:child_process");
const { once } = require("node:events");
const fs = require("node:fs");
const net = require("node:net");
const path = require("node:path");

const { expect, test: base } = require("@playwright/test");

const ROOT = path.resolve(__dirname, "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");
const SCENARIO = "app_r53_bad_engine.json";
const DEMO_CREDENTIAL = "s01-registered-demo-test-credential";
const OPERATOR_CREDENTIAL = "s01-registered-operator-test-credential";
const ARTIFACT_ROOT = "/tmp/xiaopeng-task4-s01-browser";
const SOURCE_SHA256 = "8f3bf94619690887fbbb3a5c4fa3bfdb815f178874e0b0dda2469b69454b2a58";
const PROVENANCE_MANIFEST_DIGEST =
  "39540fb8b087cb3baad722ff622415dc54c3a4063582e894344d9026c6a36d2e";
const ENGINE_PROVENANCE = [
  {
    document: "机动车登记证书 · engine_no",
    reference: "reg · present",
    observation: "Observation observation_0ca1597414109c7274c6a788",
    page: "Page 1",
    region: "Region /documents/0/fields/engine_no",
  },
  {
    document: "交强险保单 · engine_no",
    reference: "pol · present",
    observation: "Observation observation_a103a7f1fa8535d64140ef0c",
    page: "Page 2",
    region: "Region /documents/1/fields/engine_no",
  },
  {
    document: "发票 · engine_no",
    reference: "inv · present",
    observation: "Observation observation_dd33dc828b5ee2246e1ff6a2",
    page: "Page 4",
    region: "Region /documents/3/fields/engine_no",
  },
];

async function reservePort() {
  const server = net.createServer();
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  const port = address.port;
  await new Promise((resolve, reject) => server.close((error) => (error ? reject(error) : resolve())));
  return port;
}

async function startUvicorn({
  appTarget = "task4_consistency.web.app:app",
  appFactory = false,
  extraEnv = {},
} = {}) {
  const port = await reservePort();
  const statePath = path.join(
    "/tmp",
    `xiaopeng-task4-s01-browser-${process.pid}-${port}-${Date.now()}.sqlite3`,
  );
  const output = [];
  const child = spawn(
    PYTHON,
    [
      "-m",
      "uvicorn",
      appTarget,
      "--host",
      "127.0.0.1",
      "--port",
      String(port),
      "--log-level",
      "warning",
      ...(appFactory ? ["--factory"] : []),
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
        TASK4_S01_DEMO_CREDENTIAL: DEMO_CREDENTIAL,
        TASK4_S01_DEMO_SUBJECT: "c-demo-browser-user",
        TASK4_S01_OPERATOR_CREDENTIAL: OPERATOR_CREDENTIAL,
        TASK4_S01_OPERATOR_SUBJECT: "c-demo-browser-operator",
        ...extraEnv,
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  child.stdout.on("data", (chunk) => output.push(chunk.toString()));
  child.stderr.on("data", (chunk) => output.push(chunk.toString()));

  const baseURL = `http://127.0.0.1:${port}`;
  const deadline = Date.now() + 8_000;
  let lastError;
  while (Date.now() < deadline && child.exitCode === null) {
    try {
      const response = await fetch(`${baseURL}/api/health`);
      if (response.ok) {
        return { baseURL, child, output };
      }
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }

  child.kill("SIGKILL");
  throw new Error(`uvicorn did not start: ${lastError || output.join("")}`);
}

async function stopUvicorn(server) {
  if (server.child.exitCode !== null) return;
  server.child.kill("SIGTERM");
  const exited = once(server.child, "exit");
  const timeout = new Promise((resolve) => setTimeout(resolve, 5_000, "timeout"));
  if ((await Promise.race([exited, timeout])) === "timeout") {
    server.child.kill("SIGKILL");
    await once(server.child, "exit");
  }
}

async function startExpiringS01Server({ backgroundEnabled }) {
  const clockPath = path.join(
    "/tmp",
    `xiaopeng-task4-s01-browser-clock-${process.pid}-${Date.now()}.txt`,
  );
  fs.writeFileSync(clockPath, "100", "ascii");
  const server = await startUvicorn({
    appTarget: "tests.test_s01_http:create_expiring_session_app",
    appFactory: true,
    extraEnv: {
      TASK4_S01_TEST_SESSION_CLOCK_PATH: clockPath,
      TASK4_S01_TEST_SESSION_TTL_SECONDS: "10",
      TASK4_S01_TEST_BACKGROUND_ENABLED: backgroundEnabled ? "1" : "0",
    },
  });
  return { ...server, clockPath };
}

const test = base.extend({
  s01Server: async ({}, use) => {
    const server = await startUvicorn();
    try {
      await use(server);
    } finally {
      await stopUvicorn(server);
    }
  },
  s01ExpiringServer: async ({}, use) => {
    const server = await startExpiringS01Server({ backgroundEnabled: true });
    try {
      await use(server);
    } finally {
      await stopUvicorn(server);
    }
  },
  s01PendingExpiringServer: async ({}, use) => {
    const server = await startExpiringS01Server({ backgroundEnabled: false });
    try {
      await use(server);
    } finally {
      await stopUvicorn(server);
    }
  },
});

function rawLexemesFromFixture(fixture) {
  const lexemes = [];
  const seen = new Set();
  for (const document of fixture.documents || []) {
    for (const field of Object.values(document.fields || {})) {
      const raw = field && typeof field === "object" && !Array.isArray(field) ? field.raw : field;
      if (raw === null || raw === undefined || raw === "") continue;
      const type = Array.isArray(raw) ? "array" : typeof raw;
      const encoded = JSON.stringify(raw);
      const key = `${type}:${encoded}`;
      if (seen.has(key)) continue;
      seen.add(key);
      lexemes.push({ type, value: raw, lexeme: type === "string" ? raw : encoded });
    }
  }
  return lexemes;
}

function fixtureSecrets() {
  const fixture = JSON.parse(
    fs.readFileSync(path.join(ROOT, "fixtures", "applications", SCENARIO), "utf8"),
  );
  return {
    upstreamApplicationReference: String(fixture.application_id),
    rawLexemes: rawLexemesFromFixture(fixture),
  };
}

function typedValues(value, found = []) {
  const type = Array.isArray(value) ? "array" : typeof value;
  found.push({ type, value, encoded: JSON.stringify(value) });
  if (Array.isArray(value)) {
    for (const item of value) typedValues(item, found);
  } else if (value && typeof value === "object") {
    for (const item of Object.values(value)) typedValues(item, found);
  }
  return found;
}

function supportsEmbeddedStringProbe(raw) {
  return raw.type === "string" && raw.value.length > 1;
}

function responseSecretLeaks(data, secrets = fixtureSecrets()) {
  const values = typedValues(data);
  const leaks = [];
  if (JSON.stringify(data).includes(secrets.upstreamApplicationReference)) {
    leaks.push(`upstream:${secrets.upstreamApplicationReference}`);
  }
  for (const raw of secrets.rawLexemes) {
    const exact = values.some(
      (candidate) => candidate.type === raw.type && candidate.encoded === JSON.stringify(raw.value),
    );
    const embedded =
      supportsEmbeddedStringProbe(raw) &&
      values.some(
        (candidate) =>
          candidate.type === "string" && String(candidate.value).includes(raw.value),
      );
    if (exact || embedded) leaks.push(`raw:${raw.type}:${raw.lexeme}`);
  }
  return [...new Set(leaks)];
}

async function domSecretLeaks(page, secrets = fixtureSecrets()) {
  const text = await page.locator("body").evaluate((root) => {
    const textNodes = [];
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const value = walker.currentNode.textContent.trim();
      if (value) textNodes.push(value);
    }
    return { body: root.innerText, textNodes };
  });
  const leaks = [];
  if (text.body.includes(secrets.upstreamApplicationReference)) {
    leaks.push(`upstream:${secrets.upstreamApplicationReference}`);
  }
  for (const raw of secrets.rawLexemes) {
    const display = raw.type === "string" ? raw.value : raw.lexeme;
    const leaked = supportsEmbeddedStringProbe(raw)
      ? text.body.includes(raw.value)
      : text.textNodes.includes(String(display));
    if (leaked) leaks.push(`raw:${raw.type}:${raw.lexeme}`);
  }
  return [...new Set(leaks)];
}

function objectKeys(value, found = []) {
  if (Array.isArray(value)) {
    for (const item of value) objectKeys(item, found);
    return found;
  }
  if (value && typeof value === "object") {
    for (const [key, item] of Object.entries(value)) {
      found.push(key);
      objectKeys(item, found);
    }
  }
  return found;
}

async function expectNoLayoutFaults(page) {
  const faults = await page.evaluate(() => {
    const visible = (element) => {
      const style = getComputedStyle(element);
      const box = element.getBoundingClientRect();
      return style.display !== "none" && style.visibility !== "hidden" && box.width > 0 && box.height > 0;
    };
    const overflow = [...document.querySelectorAll("button, h1, h2, h3, dt, dd, [data-fit-text]")]
      .filter(visible)
      .filter((element) => element.scrollWidth > element.clientWidth + 1)
      .map((element) => element.getAttribute("data-testid") || element.textContent.trim().slice(0, 40));
    const stages = [...document.querySelectorAll("[data-overlap-check]")].filter(visible);
    const overlaps = [];
    for (let left = 0; left < stages.length; left += 1) {
      for (let right = left + 1; right < stages.length; right += 1) {
        const a = stages[left].getBoundingClientRect();
        const b = stages[right].getBoundingClientRect();
        const intersects = a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;
        if (intersects) overlaps.push(`${left}:${right}`);
      }
    }
    return {
      scaledDesktop:
        window.screen.width <= 480 && window.innerWidth > window.screen.width + 1,
      horizontalOverflow: document.documentElement.scrollWidth > window.innerWidth + 1,
      overflow,
      overlaps,
    };
  });
  expect(faults).toEqual({
    scaledDesktop: false,
    horizontalOverflow: false,
    overflow: [],
    overlaps: [],
  });
}

async function completeControlledFlow(page, baseURL) {
  const secrets = fixtureSecrets();
  const pageRequests = [];
  const pageResponses = [];
  page.on("request", (request) => pageRequests.push(request));
  page.on("response", (response) => pageResponses.push(response));

  const navigation = await openControlledPage(page, baseURL);
  expect(navigation.status()).toBe(200);
  expect(navigation.headers()["cache-control"]).toContain("no-store");

  await expect(page.getByRole("heading", { name: "一致性审核工作台" })).toBeVisible();
  await expect(page.getByTestId("boundary-track")).toHaveText("C-DEMO");
  await expect(page.getByTestId("boundary-gate")).toContainText("G1");

  await page.getByRole("button", { name: "提交受控场景" }).click();
  await expect(page.getByTestId("receipt-panel")).toBeVisible();
  await expect(page.getByTestId("receipt-id")).not.toHaveText("");
  await expect(page.getByTestId("application-id")).not.toHaveText("");
  const receipt = await page.getByTestId("receipt-id").textContent();

  await expect(page.getByTestId("process-status")).toHaveText("检查完成");
  await expect(page.getByTestId("receipt-id")).toHaveText(receipt);
  await expect(page.getByTestId("queue-phase")).toHaveText("Manual Review");
  await expect(page.getByTestId("queue-route")).toHaveText("manual_review");
  await expect(page.getByTestId("blocker-rule")).toHaveText("R_ENGINE_CROSS");
  const evidenceItems = page.getByTestId("evidence-item");
  await expect(evidenceItems).toHaveCount(ENGINE_PROVENANCE.length);
  const provenanceManifestDigests = [];
  for (const [index, expected] of ENGINE_PROVENANCE.entries()) {
    const item = evidenceItems.nth(index);
    await expect(item.getByTestId("evidence-document")).toHaveText(expected.document);
    await expect(item.getByTestId("evidence-reference")).toHaveText(expected.reference);
    await expect(item.getByTestId("evidence-observation")).toHaveText(expected.observation);
    await expect(item.getByTestId("evidence-object-ref")).toHaveText(
      `Source c-demo-object:sha256:${SOURCE_SHA256}`,
    );
    await expect(item.getByTestId("evidence-sha256")).toHaveText(`SHA-256 ${SOURCE_SHA256}`);
    const provenanceManifest = item.getByTestId("evidence-provenance-manifest");
    await expect(provenanceManifest).toHaveText(
      `Manifest SHA-256 ${PROVENANCE_MANIFEST_DIGEST}`,
    );
    provenanceManifestDigests.push(await provenanceManifest.textContent());
    await expect(item.getByTestId("evidence-page")).toHaveText(expected.page);
    await expect(item.getByTestId("evidence-region")).toHaveText(expected.region);
  }
  expect(new Set(provenanceManifestDigests).size).toBe(1);
  await expect(page.getByTestId("lifecycle-revision")).toHaveText("6");
  await expect(page.getByTestId("evidence-revision")).toHaveText("1");
  await expect(page.getByTestId("current-run-id")).toHaveText(/^run_/);
  await expect(page.getByTestId("evidence-snapshot-id")).toHaveText(
    /^snapshot_sha256_[0-9a-f]{64}$/,
  );

  const bodyText = await page.locator("body").innerText();
  expect(await domSecretLeaks(page, secrets)).toEqual([]);
  for (const forbidden of [
    "label",
    "expected_verdicts",
    "disposition",
    "policy",
    "loan",
    "规则与知识库",
    "运行校验",
  ]) {
    expect(bodyText.toLowerCase()).not.toContain(forbidden.toLowerCase());
  }
  await expect(page.locator("#app-json, #fixture-select, [data-legacy-page]")).toHaveCount(0);

  const browserApiRequests = pageRequests.filter((request) =>
    ["fetch", "xhr"].includes(request.resourceType()),
  );
  expect(browserApiRequests.length).toBeGreaterThanOrEqual(3);
  for (const request of browserApiRequests) {
    expect(new URL(request.url()).pathname).toMatch(/^\/controlled\/s01\/api\//);
    expect(request.headers()["x-s01-role"]).toBeUndefined();
    expect(request.headers()["x-s01-scope"]).toBeUndefined();
  }
  expect(pageRequests.some((request) => new URL(request.url()).pathname === "/api/check")).toBe(false);
  expect(
    pageRequests.some(
      (request) => new URL(request.url()).pathname === "/controlled/s01/api/commands/process",
    ),
  ).toBe(false);

  const forbiddenResponseKeys = new Set([
    "label",
    "expected_verdicts",
    "policy",
    "loan",
  ]);
  const leakedKeys = [];
  const leakedValues = [];
  for (const response of pageResponses.filter((item) =>
    ["fetch", "xhr"].includes(item.request().resourceType()),
  )) {
    const data = await response.json();
    leakedKeys.push(...objectKeys(data).filter((key) => forbiddenResponseKeys.has(key.toLowerCase())));
    leakedValues.push(...responseSecretLeaks(data, secrets));
  }
  expect(leakedKeys).toEqual([]);
  expect(leakedValues).toEqual([]);

  await expectNoLayoutFaults(page);
  return { receipt, pageRequests };
}

async function openControlledPage(page, baseURL) {
  await page.setExtraHTTPHeaders({ Authorization: `Bearer ${DEMO_CREDENTIAL}` });
  return page.goto(`${baseURL}/controlled/s01`, { waitUntil: "networkidle" });
}

test("the browser leak oracle detects every current short raw string without broad one-character matching", async ({
  page,
}) => {
  const fixed = fixtureSecrets();
  const currentShortStrings = fixed.rawLexemes.filter(
    (raw) => raw.type === "string" && raw.value.length < 4,
  );
  expect(currentShortStrings).toEqual([
    { type: "string", value: "半真壬", lexeme: "半真壬" },
    { type: "string", value: "汉EV", lexeme: "汉EV" },
  ]);

  for (const raw of currentShortStrings) {
    const oneSecret = {
      upstreamApplicationReference: "UPSTREAM-NOT-PRESENT",
      rawLexemes: [raw],
    };
    const expectedLeak = [`raw:string:${raw.lexeme}`];
    expect(responseSecretLeaks({ exact: raw.value }, oneSecret)).toEqual(expectedLeak);
    expect(
      responseSecretLeaks(
        { prefix: `before:${raw.value}`, suffix: `${raw.value}:after` },
        oneSecret,
      ),
    ).toEqual(expectedLeak);

    await page.setContent(`<main><span>${raw.value}</span></main>`);
    expect(await domSecretLeaks(page, oneSecret)).toEqual(expectedLeak);
    await page.setContent(
      `<main><span>before:${raw.value}</span><span>${raw.value}:after</span></main>`,
    );
    expect(await domSecretLeaks(page, oneSecret)).toEqual(expectedLeak);
  }

  const syntheticSecrets = {
    upstreamApplicationReference: "UPSTREAM-LEAK-SENTINEL",
    rawLexemes: rawLexemesFromFixture({
      documents: [
        {
          fields: {
            short: { raw: "abc" },
            singleCharacter: { raw: "x" },
            number: { raw: 917263 },
            boolean: { raw: false },
            empty: { raw: "" },
            absent: { raw: null },
          },
        },
      ],
    }),
  };
  expect(syntheticSecrets.rawLexemes).toEqual([
    { type: "string", value: "abc", lexeme: "abc" },
    { type: "string", value: "x", lexeme: "x" },
    { type: "number", value: 917263, lexeme: "917263" },
    { type: "boolean", value: false, lexeme: "false" },
  ]);
  const responseLeaks = responseSecretLeaks(
    {
      source: "UPSTREAM-LEAK-SENTINEL",
      short: "abc",
      singleCharacter: "x",
      number: 917263,
      boolean: false,
    },
    syntheticSecrets,
  );
  expect(responseLeaks).toHaveLength(5);

  const oneCharacterSecret = {
    upstreamApplicationReference: "UPSTREAM-NOT-PRESENT",
    rawLexemes: [{ type: "string", value: "x", lexeme: "x" }],
  };
  expect(responseSecretLeaks({ wrapped: "prefix-x-suffix" }, oneCharacterSecret)).toEqual(
    [],
  );

  await page.setContent(
    "<main><span>UPSTREAM-LEAK-SENTINEL</span><span>abc</span><span>x</span><span>917263</span><span>false</span></main>",
  );
  expect(await domSecretLeaks(page, syntheticSecrets)).toHaveLength(5);
  await page.setContent("<main><span>prefix-x-suffix</span></main>");
  expect(await domSecretLeaks(page, oneCharacterSecret)).toEqual([]);
});

test("desktop reviewer follows the controlled scenario to minimized blocker evidence", async ({
  browser,
  s01Server,
}) => {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await completeControlledFlow(page, s01Server.baseURL);

  fs.mkdirSync(ARTIFACT_ROOT, { recursive: true });
  const screenshot = await page.screenshot({
    path: path.join(ARTIFACT_ROOT, "desktop-flow.png"),
    fullPage: true,
  });
  expect(screenshot.length).toBeGreaterThan(20_000);
  await page.close();
});

test("mobile reviewer completes the same controlled flow without layout faults", async ({
  browser,
  s01Server,
}) => {
  const page = await browser.newPage({
    viewport: { width: 390, height: 844 },
    screen: { width: 390, height: 844 },
    deviceScaleFactor: 2,
    hasTouch: true,
    isMobile: true,
  });
  await completeControlledFlow(page, s01Server.baseURL);

  fs.mkdirSync(ARTIFACT_ROOT, { recursive: true });
  const screenshot = await page.screenshot({
    path: path.join(ARTIFACT_ROOT, "mobile-flow.png"),
    fullPage: true,
  });
  expect(screenshot.length).toBeGreaterThan(15_000);
  await page.close();
});

test("a newer queue refresh aborts the superseded browser request", async ({
  page,
  s01Server,
}) => {
  await openControlledPage(page, s01Server.baseURL);
  await page.getByRole("button", { name: "提交受控场景" }).click();
  await expect(page.getByTestId("application-id")).not.toHaveText("");
  await expect(page.getByTestId("process-status")).toHaveText("检查完成");
  const applicationId = await page.getByTestId("application-id").textContent();

  let requestNumber = 0;
  let startFirst;
  let releaseFirst;
  const firstStarted = new Promise((resolve) => {
    startFirst = resolve;
  });
  const firstCanFinish = new Promise((resolve) => {
    releaseFirst = resolve;
  });
  await page.route("**/controlled/s01/api/queries/queue", async (route) => {
    requestNumber += 1;
    if (requestNumber === 1) {
      startFirst();
      await firstCanFinish;
      await route
        .fulfill({
          status: 200,
          contentType: "application/json",
          headers: { "Cache-Control": "no-store" },
          body: JSON.stringify({
            items: [
              {
                application_id: applicationId,
                phase: "Superseded",
                route: "superseded",
                evidence_ready: false,
                mandatory_blockers: [],
              },
            ],
            projection_watermark: 1,
          }),
        })
        .catch(() => {});
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "Cache-Control": "no-store" },
      body: JSON.stringify({
        items: [
          {
            application_id: applicationId,
            phase: "Manual Review",
            route: "manual_review",
            evidence_ready: true,
            mandatory_blockers: [],
          },
        ],
        projection_watermark: 777,
      }),
    });
  });

  await page.getByRole("button", { name: "刷新队列" }).click();
  await firstStarted;
  const aborted = page.waitForEvent("requestfailed", {
    predicate: (request) =>
      new URL(request.url()).pathname === "/controlled/s01/api/queries/queue",
    timeout: 5_000,
  });
  await page.getByRole("button", { name: "刷新队列" }).click();

  await expect(page.getByTestId("queue-watermark")).toHaveText("777");
  await expect(page.getByTestId("queue-phase")).toHaveText("Manual Review");
  const failedRequest = await aborted;
  expect(failedRequest.failure().errorText).toMatch(/aborted/i);
  releaseFirst();
  await expect(page.getByTestId("queue-watermark")).toHaveText("777");
  expect(requestNumber).toBe(2);
});

test("an uncertain browser submit retries the same in-memory command key", async ({
  page,
  s01Server,
}) => {
  const requestBodies = [];
  let committedPayload;
  let replayPayload;
  await page.route("**/controlled/s01/api/workbench/commands/submit", async (route) => {
    requestBodies.push(route.request().postDataJSON());
    const response = await route.fetch();
    const payload = await response.json();
    if (requestBodies.length === 1) {
      committedPayload = payload;
      await route.abort("failed");
      return;
    }
    replayPayload = payload;
    await route.fulfill({ response });
  });

  await openControlledPage(page, s01Server.baseURL);
  await page.getByRole("button", { name: "提交受控场景" }).click();
  await expect(page.locator("#submit-status")).toHaveText("提交未完成");
  await expect(page.getByTestId("receipt-panel")).toBeHidden();

  await page.getByRole("button", { name: "提交受控场景" }).click();
  await expect(page.locator("#submit-status")).toHaveText("回执已确认");
  await expect(page.getByTestId("receipt-panel")).toBeVisible();

  expect(requestBodies).toHaveLength(2);
  expect(requestBodies[1].idempotency_key).toBe(requestBodies[0].idempotency_key);
  expect(committedPayload.replayed).toBe(false);
  expect(replayPayload.replayed).toBe(true);
  await expect(page.getByTestId("receipt-id")).toHaveText(committedPayload.receipt_id);
  await expect(page.getByTestId("application-id")).toHaveText(
    committedPayload.application_id,
  );
  expect(await page.evaluate(() => [localStorage.length, sessionStorage.length])).toEqual([0, 0]);
});

test("reload starts from a clean no-store shell with no browser-owned facts", async ({
  browser,
  s01Server,
}) => {
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  const controlledResponses = [];
  page.on("response", (response) => {
    const pathname = new URL(response.url()).pathname;
    if (pathname === "/controlled/s01" || pathname.startsWith("/controlled/s01/api/")) {
      controlledResponses.push(response);
    }
  });

  const { receipt } = await completeControlledFlow(page, s01Server.baseURL);
  const applicationId = await page.getByTestId("application-id").textContent();
  const reloadResponse = await page.reload({ waitUntil: "networkidle" });
  expect(reloadResponse.status()).toBe(200);
  expect(reloadResponse.fromServiceWorker()).toBe(false);
  expect((await reloadResponse.allHeaders())["cache-control"]).toContain("no-store");

  await expect(page.getByTestId("receipt-panel")).toBeHidden();
  await expect(page.getByTestId("blocker-panel")).toBeHidden();
  await expect(page.locator("#submit-status")).toHaveText("待提交");
  const reloadedBody = await page.locator("body").innerText();
  expect(reloadedBody).not.toContain(receipt);
  expect(reloadedBody).not.toContain(applicationId);

  const browserState = await page.evaluate(async () => ({
    localStorageEntries: localStorage.length,
    sessionStorageEntries: sessionStorage.length,
    cacheNames: "caches" in window ? await caches.keys() : [],
    databases: indexedDB.databases ? await indexedDB.databases() : [],
    serviceWorkers:
      "serviceWorker" in navigator ? (await navigator.serviceWorker.getRegistrations()).length : 0,
  }));
  expect(browserState).toEqual({
    localStorageEntries: 0,
    sessionStorageEntries: 0,
    cacheNames: [],
    databases: [],
    serviceWorkers: 0,
  });

  expect(controlledResponses.length).toBeGreaterThanOrEqual(5);
  for (const response of controlledResponses) {
    const headers = await response.allHeaders();
    expect(headers["cache-control"]).toContain("no-store");
    expect(response.fromServiceWorker()).toBe(false);
  }
  await page.close();
});

test("an issued identity expiry hides authority and clears rendered sensitive state", async ({
  browser,
  s01ExpiringServer,
}) => {
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  const { receipt } = await completeControlledFlow(page, s01ExpiringServer.baseURL);
  const applicationId = await page.getByTestId("application-id").textContent();
  const currentRunId = await page.getByTestId("current-run-id").textContent();
  const snapshotId = await page.getByTestId("evidence-snapshot-id").textContent();

  fs.writeFileSync(s01ExpiringServer.clockPath, "110", "ascii");
  const expiredCommand = await page.evaluate(async () => {
    const response = await fetch("/controlled/s01/api/workbench/commands/submit", {
      method: "POST",
      cache: "no-store",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        scenario_id: "app_r53_bad_engine.json",
        idempotency_key: "browser-expired-command",
      }),
    });
    return { status: response.status, text: await response.text() };
  });
  expect(expiredCommand.status).toBe(403);
  expect(expiredCommand.text).not.toContain(applicationId);

  const queueResponse = page.waitForResponse(
    (response) => new URL(response.url()).pathname === "/controlled/s01/api/queries/queue",
  );
  await page.getByRole("button", { name: "刷新队列" }).click();
  expect((await queueResponse).status()).toBe(200);
  const hiddenWorkspace = await page.evaluate(async (id) => {
    const response = await fetch(
      `/controlled/s01/api/queries/applications/${encodeURIComponent(id)}/workspace`,
      { cache: "no-store", credentials: "same-origin" },
    );
    return { status: response.status, text: await response.text() };
  }, applicationId);
  expect(hiddenWorkspace.status).toBe(404);
  expect(hiddenWorkspace.text).not.toContain(applicationId);

  await expect(page.getByTestId("receipt-panel")).toBeHidden();
  await expect(page.getByTestId("blocker-panel")).toBeHidden();
  await expect(page.locator("#queue-summary")).toBeHidden();
  const bodyText = await page.locator("body").innerText();
  for (const sensitive of [receipt, applicationId, currentRunId, snapshotId]) {
    expect(bodyText).not.toContain(sensitive);
  }
  await page.close();
});

test("identity expiry clears an admitted receipt before any projection exists", async ({
  browser,
  s01PendingExpiringServer,
}) => {
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  await openControlledPage(page, s01PendingExpiringServer.baseURL);
  await page.getByRole("button", { name: "提交受控场景" }).click();
  await expect(page.getByTestId("receipt-panel")).toBeVisible();
  await expect(page.getByTestId("process-status")).toHaveText("检查中");
  const receipt = await page.getByTestId("receipt-id").textContent();
  const applicationId = await page.getByTestId("application-id").textContent();

  fs.writeFileSync(s01PendingExpiringServer.clockPath, "110", "ascii");
  await page.getByRole("button", { name: "刷新队列" }).click();

  await expect(page.getByTestId("receipt-panel")).toBeHidden();
  await expect(page.getByTestId("blocker-panel")).toBeHidden();
  await expect(page.locator("#queue-summary")).toBeHidden();
  const bodyText = await page.locator("body").innerText();
  expect(bodyText).not.toContain(receipt);
  expect(bodyText).not.toContain(applicationId);
  await page.close();
});

test("a queue network interruption clears rendered sensitive state", async ({
  page,
  s01Server,
}) => {
  const { receipt } = await completeControlledFlow(page, s01Server.baseURL);
  const applicationId = await page.getByTestId("application-id").textContent();
  await page.route(
    "**/controlled/s01/api/queries/queue",
    (route) => route.abort("failed"),
    { times: 1 },
  );

  await page.getByRole("button", { name: "刷新队列" }).click();
  await expect(page.locator("#submit-status")).toHaveText("队列请求未完成");
  await expect(page.getByTestId("receipt-panel")).toBeHidden();
  await expect(page.getByTestId("blocker-panel")).toBeHidden();
  await expect(page.locator("#queue-summary")).toBeHidden();
  const bodyText = await page.locator("body").innerText();
  expect(bodyText).not.toContain(receipt);
  expect(bodyText).not.toContain(applicationId);
});
