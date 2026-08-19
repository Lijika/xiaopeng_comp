/** S11 entity-link correction browser E2E (highest public seam).

 * Reviewer: queue -> application-local candidate/provenance comparison ->
 * explicit accept decision -> Evidence successor -> readiness/rerun -> new
 * current run/route.  Every prior candidate claim, predecessor decision and
 * both runs stay immutable and navigable.  Low confidence, conflict,
 * staleness, wrong-release and unauthorized scope remain explicit backend
 * outcomes; the browser never auto-links and never creates cross-application
 * identity.
 */
const { spawn } = require("node:child_process");
const { once } = require("node:events");
const fs = require("node:fs");
const net = require("node:net");
const path = require("node:path");

const { expect, test } = require("@playwright/test");

const ROOT = path.resolve(__dirname, "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");
const DEMO_CREDENTIAL = "s11-registered-demo-credential";
const S02_CREDENTIAL = "s11-registered-s02-credential";
const S02_SUBJECT = "s11-registered-s02-reviewer";
const S02_TENANT = "tenant-s11-react";
const S02_SOURCE = "registered-s11-react-source";
const SCENARIO = "app_s11_entity_ambiguity.json";
const REACT_URL = "/controlled/s01/react";
const MENTION_ORG = "s11_mention_org_pol";
const MENTION_CITY = "s11_mention_city_lease";
const MENTION_BRAND = "s11_mention_brand_inv";
const STATE_PREFIX = `xiaopeng-task4-s11-react-${process.pid}-`;
const S02_PREFIX = `xiaopeng-task4-s11-react-s02-${process.pid}-`;
const FIXTURE_PREFIX = `xiaopeng-task4-s11-react-${process.pid}-fixture-`;

function createS02Fixture() {
  const root = fs.mkdtempSync(
    path.join("/tmp", `xiaopeng-task4-s11-react-s02-${process.pid}-`),
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
          workload_identity_id: "s11-react-workload",
          adapter_id: "s11-react-adapter",
          adapter_version: "1",
          source_shape: "ocr-detection/unversioned",
          producer_family: "s11-react-ocr",
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
          object_ref: "s11-react-result-object",
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

/** Removes exactly this server's owned SQLite state and its -wal/-shm
 * siblings; every artifact is attempted even if one removal rejects. */
function cleanupStatePath(statePath) {
  let firstError;
  for (const suffix of ["", "-wal", "-shm"]) {
    try {
      fs.rmSync(`${statePath}${suffix}`, { force: true });
    } catch (error) {
      if (firstError === undefined) firstError = error;
    }
  }
  if (firstError !== undefined) throw firstError;
}

/** The unique owned artifacts this worker process creates in /tmp: the S11
 * SQLite state (with siblings), the S02 fixture root, and the runtime
 * reciprocal fixture root. */
function listOwnedArtifacts() {
  const state = fs
    .readdirSync("/tmp")
    .filter(
      (name) => name.startsWith(STATE_PREFIX) && name.includes(".sqlite3"),
    )
    .sort();
  const s02 = fs
    .readdirSync("/tmp")
    .filter((name) => name.startsWith(S02_PREFIX))
    .sort();
  const fixture = fs
    .readdirSync("/tmp")
    .filter((name) => name.startsWith(FIXTURE_PREFIX))
    .sort();
  return { state, s02, fixture };
}

async function startServer({ appTarget, ...extraEnv } = {}) {
  const port = await reservePort();
  const s02Fixture = createS02Fixture();
  const statePath = path.join(
    "/tmp",
    `${STATE_PREFIX}${port}-${Date.now()}.sqlite3`,
  );
  const output = [];
  const child = spawn(
    PYTHON,
    [
      "-m",
      "uvicorn",
      appTarget ?? "tests.test_s11_http:create_s11_react_test_app",
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
        TASK4_S01_TEST_BACKGROUND_ENABLED: "0",
        TASK4_S02_TEST_BACKGROUND_ENABLED: "1",
        TASK4_S01_DEMO_CREDENTIAL: DEMO_CREDENTIAL,
        TASK4_S01_DEMO_SUBJECT: "s11-browser-reviewer",
        TASK4_S01_OPERATOR_CREDENTIAL: "s11-operator-credential",
        TASK4_S01_OPERATOR_SUBJECT: "s11-browser-operator",
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
  let ready = false;
  while (Date.now() < deadline && child.exitCode === null) {
    try {
      if ((await fetch(`${baseURL}/api/health`)).ok) {
        ready = true;
        break;
      }
    } catch (_) {
      /* bounded readiness retry */
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  if (ready) {
    return { baseURL, child, output, statePath, s02Fixture };
  }
  // Startup failure fully owns its child and its exact temp state: register
  // the exit promise before killing, reap the process, then remove the owned
  // database/-wal/-shm artifacts and the owned S02 root before throwing.
  const exited = once(child, "exit");
  if (child.exitCode === null) {
    child.kill("SIGKILL");
    await exited;
  }
  cleanupStatePath(statePath);
  try {
    fs.rmSync(s02Fixture.root, { recursive: true, force: true });
  } catch (_) {
    /* best effort */
  }
  throw new Error(`S11 React server did not start: ${output.join("")}`);
}

async function stopServer(server) {
  const failures = [];
  try {
    const exited = once(server.child, "exit");
    if (server.child.exitCode === null) {
      server.child.kill("SIGTERM");
      if (
        (await Promise.race([
          exited,
          new Promise((resolve) => setTimeout(resolve, 5_000, "timeout")),
        ])) === "timeout"
      ) {
        server.child.kill("SIGKILL");
        await exited;
      }
    }
  } catch (error) {
    failures.push(error);
  }
  try {
    cleanupStatePath(server.statePath);
  } catch (error) {
    failures.push(error);
  }
  try {
    fs.rmSync(server.s02Fixture.root, { recursive: true, force: true });
  } catch (error) {
    failures.push(error);
  }
  if (failures.length > 0) throw failures[0];
}

/** Advance the S01 worker explicitly through the test-driver boundary and
 * refresh the minimized projection so the successor work becomes visible.
 * The background runtime stays disabled so every run transition is
 * deterministic and observable from React DOM while queued. */
async function processNextJob(reviewer, server) {
  const response = await reviewer.request.post(
    `${server.baseURL}/controlled/s01/api/_test/commands/process`,
    {
      data: {
        worker_id: "s11-react-driver",
        now: Math.floor(Date.now() / 1000),
      },
    },
  );
  expect(response.ok()).toBeTruthy();
  const result = await response.json();
  expect(result.status).toBe("complete");
  const projected = await reviewer.request.post(
    `${server.baseURL}/controlled/s01/api/_test/commands/project`,
    { data: {} },
  );
  expect(projected.ok()).toBeTruthy();
  return result;
}

async function installManualWork(baseURL, reviewer) {
  const admission = await reviewer.request.post(
    `${baseURL}/controlled/s01/api/commands/submit`,
    { data: { scenario_id: SCENARIO, idempotency_key: "s11-react-admission" } },
  );
  expect(admission.ok()).toBeTruthy();
  const accepted = await admission.json();
  await processNextJob(reviewer, { baseURL });
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
  throw new Error("S11 manual review work never appeared");
}

/** Press Tab until the focused element carries the given data-testid. */
async function tabToTestId(page, testId, maxTabs = 80) {
  for (let attempt = 0; attempt < maxTabs; attempt += 1) {
    await page.keyboard.press("Tab");
    const reached = await page.evaluate((id) => {
      const active = document.activeElement;
      return active !== null && active.dataset?.testid === id;
    }, testId);
    if (reached) return;
  }
  throw new Error(`keyboard focus never reached ${testId}`);
}

/** Press Tab until the focused anchor links to the given work id. */
async function tabToWorkLink(page, workId, maxTabs = 80) {
  const fragment = encodeURIComponent(workId);
  for (let attempt = 0; attempt < maxTabs; attempt += 1) {
    await page.keyboard.press("Tab");
    const reached = await page.evaluate((hrefFragment) => {
      const active = document.activeElement;
      return (
        active !== null &&
        active.tagName === "A" &&
        active.href.includes(hrefFragment)
      );
    }, fragment);
    if (reached) return;
  }
  throw new Error(`keyboard focus never reached ${workId}`);
}

/** Pick a native select option with the keyboard: focus is already on the
 * select, ArrowDown moves the highlight one option at a time (the first
 * option occupies index 0), and Tab commits the choice. */
async function selectOptionByKeyboard(page, locator, value) {
  const count = await locator.locator("option").count();
  let index = -1;
  for (let i = 0; i < count; i += 1) {
    const optionValue = await locator
      .locator("option")
      .nth(i)
      .getAttribute("value");
    if (optionValue === value) {
      index = i;
      break;
    }
  }
  expect(index).toBeGreaterThanOrEqual(0);
  for (let step = 0; step < index; step += 1) {
    await page.keyboard.press("ArrowDown");
  }
  await page.keyboard.press("Tab");
  await expect(locator).toHaveValue(value);
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
  await tabToWorkLink(reviewer, workId);
  await reviewer.keyboard.press("Enter");
  await expect(reviewer.getByTestId("review-panel")).toBeVisible();
  await tabToTestId(reviewer, "claim-button");
  await reviewer.keyboard.press("Enter");
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
  throw new Error("S11 successor manual work never appeared");
}

async function openAndClaimWork(reviewer, server, item) {
  await reviewer.goto(`${server.baseURL}${REACT_URL}`, {
    waitUntil: "networkidle",
  });
  await expect(reviewer.getByTestId("queue-panel")).toBeVisible();
  await tabToWorkLink(reviewer, item.work_item_id);
  await reviewer.keyboard.press("Enter");
  await expect(reviewer.getByTestId("review-panel")).toBeVisible();
  await tabToTestId(reviewer, "claim-button");
  await reviewer.keyboard.press("Enter");
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

/** Runtime-only copy of the S11 fixture with the exact reciprocal same-as
 * cycle the controlled suite exercises: PICC and Ping An each include the
 * other's full entity id in their frozen ``same_as`` knowledge.  No fixture
 * file is committed; the copy lives under the S11-owned temporary root. */
function createReciprocalFixtureRoot() {
  const root = fs.mkdtempSync(
    path.join("/tmp", `xiaopeng-task4-s11-react-${process.pid}-fixture-`),
  );
  const payload = JSON.parse(
    fs.readFileSync(
      path.join(ROOT, "fixtures", "applications", SCENARIO),
      "utf8",
    ),
  );
  const links = payload.graph.entity_links;
  const picc = links.find((record) => record.claim_id === "s11_claim_org_picc");
  const pingan = links.find(
    (record) => record.claim_id === "s11_claim_org_pingan",
  );
  if (picc === undefined || pingan === undefined) {
    throw new Error("S11 reciprocal fixture records missing");
  }
  picc.knowledge.same_as = ["org:picc", "org:pingan_full"];
  pingan.knowledge.same_as = ["org:pingan", "org:picc_full"];
  fs.writeFileSync(
    path.join(root, SCENARIO),
    JSON.stringify(payload, null, 2),
  );
  return root;
}

/** Every pane, the form controls, and the history remain inside the active
 * viewport with no horizontal document overflow. */
async function expectContained(reviewer, testIds) {
  const result = { document: false };
  result.document = await reviewer.evaluate(
    () => document.documentElement.scrollWidth <= window.innerWidth,
  );
  for (const id of testIds) {
    result[id] = await reviewer.evaluate((testId) => {
      const element = document.querySelector(`[data-testid="${testId}"]`);
      if (element === null) return false;
      const box = element.getBoundingClientRect();
      return (
        element.scrollWidth <= element.clientWidth &&
        box.left >= 0 &&
        box.right <= window.innerWidth
      );
    }, id);
  }
  expect(result).toEqual({
    document: true,
    ...Object.fromEntries(testIds.map((id) => [id, true])),
  });
}

/** Every rendered candidate row stays inside the viewport and the document
 * has no horizontal overflow. */
async function expectCandidateRowsContained(reviewer) {
  const result = await reviewer.evaluate(() => {
    const fits = (element) => {
      const box = element.getBoundingClientRect();
      return (
        element.scrollWidth <= element.clientWidth &&
        box.left >= 0 &&
        box.right <= window.innerWidth
      );
    };
    const rows = Array.from(
      document.querySelectorAll('[data-testid="review-entity-link-candidate"]'),
    );
    return {
      document: document.documentElement.scrollWidth <= window.innerWidth,
      rows: rows.length > 0 && rows.every(fits),
    };
  });
  expect(result).toEqual({ document: true, rows: true });
}

/** The comparison form controls and the live status region do not overlap
 * each other at the active viewport. */
async function expectNoFormStatusOverlap(reviewer) {
  const noOverlap = await reviewer.evaluate(() => {
    const elements = Array.from(
      document.querySelectorAll(
        '[data-testid="review-entity-link-candidate-select"], ' +
          '[data-testid="review-entity-link-reason"], ' +
          '[data-testid="review-entity-link-submit"], ' +
          '[data-testid="review-command-status"]',
      ),
    );
    const boxes = elements.map((element) => element.getBoundingClientRect());
    const intersects = (a, b) =>
      !(
        a.right <= b.left ||
        a.left >= b.right ||
        a.bottom <= b.top ||
        a.top >= b.bottom
      );
    for (let i = 0; i < boxes.length; i += 1) {
      for (let j = i + 1; j < boxes.length; j += 1) {
        if (intersects(boxes[i], boxes[j])) return false;
      }
    }
    return true;
  });
  expect(noOverlap).toBe(true);
}

/** The S11 accept command exactly as the production panel serializes it:
 * only the generated contract fields, carrying the candidate identity and
 * the live context/fence/application plus one idempotency key. */
function expectExactEntityLinkCommand(captured) {
  const body = captured.entity_link;
  expect(Object.keys(captured).sort()).toEqual([
    "application_id",
    "entity_link",
    "expected_context",
    "expected_fence",
    "idempotency_key",
  ]);
  expect(body.schema_version).toBe("entity-link-correction/1");
  expect(body.decision).toBe("accept");
  expect(body.relationship).toBe("same_as");
  expect(Object.keys(body).sort()).toEqual([
    "candidate_claim_id",
    "decision",
    "entity_id",
    "entity_type",
    "expected_active_decision_ids",
    "finding_id",
    "knowledge_release_id",
    "label",
    "matcher_id",
    "matcher_version",
    "mention_id",
    "reason_code",
    "relationship",
    "schema_version",
    "source_evidence",
  ]);
  expect(typeof captured.expected_fence).toBe("number");
  expect(typeof captured.idempotency_key).toBe("string");
  expect(body.source_evidence.event_id).toBeTruthy();
  expect(body.source_evidence.evidence_revision).toBeGreaterThanOrEqual(1);
}

async function runS11Flow(browser, viewport) {
  const resources = {};
  let failure;
  try {
    const reciprocalRoot = createReciprocalFixtureRoot();
    resources.reciprocalRoot = reciprocalRoot;
    resources.server = await startServer({
      TASK4_S01_TEST_FIXTURE_ROOT: reciprocalRoot,
    });
    const server = resources.server;
    resources.reviewerContext = await browser.newContext({
      viewport: { width: viewport.width, height: viewport.height },
      extraHTTPHeaders: { Authorization: `Bearer ${DEMO_CREDENTIAL}` },
    });
    const reviewer = await resources.reviewerContext.newPage();
    // Capture every entity-link POST body the production panel serializes.
    const capturedPosts = [];
    reviewer.on("request", (request) => {
      if (
        request.method() === "POST" &&
        request.url().includes("correct-entity-link")
      ) {
        capturedPosts.push(request.postData());
      }
    });
    const { workId, applicationId } = await openClaimedReviewPanel(
      reviewer,
      server,
    );

    // The minimized workspace shows every application-local mention and
    // candidate/provenance record: ambiguous org, unresolved low-confidence
    // city, and conflicting brand.
    const ledger = reviewer.getByTestId("review-entity-link-ledger");
    await expect(ledger).toBeVisible();
    await expect(ledger).toContainText(MENTION_ORG);
    await expect(ledger).toContainText(MENTION_CITY);
    await expect(ledger).toContainText(MENTION_BRAND);
    await expect(ledger).toContainText("歧义（多候选并存）");
    await expect(ledger).toContainText("未解析");
    await expect(ledger).toContainText("低置信（服务端）");
    await expect(ledger).toContainText("冲突（别名互斥）");
    await expect(ledger).toContainText("s11_claim_org_picc");
    await expect(ledger).toContainText("s11_claim_org_pingan");
    await expect(ledger).toContainText("c-demo-entity-matcher/1");
    await expect(ledger).toContainText("c-demo-entity-knowledge/1");
    await expect(ledger).toContainText("org:picc_full");
    await expect(ledger).toContainText("brand:faw-vw");
    await expect(ledger).toContainText("conflict_with brand:saic-vw");
    await expect(
      reviewer.getByTestId("review-entity-link-ledger-page"),
    ).toHaveCount(3);
    await expectContained(reviewer, ["review-entity-link-ledger"]);

    // The runtime reciprocal same-as cycle stays an explicit review blocker:
    // the real FastAPI workspace and the React ledger show both reciprocal
    // edges, the ambiguous state, both candidates, and zero active decisions
    // before any Reviewer action.  The browser performs no auto-link or
    // cycle collapse.
    const workspace = await (
      await reviewer.request.get(
        `${server.baseURL}/controlled/s01/api/queries/applications/${encodeURIComponent(applicationId)}/workspace`,
      )
    ).json();
    const ambiguous = workspace.mandatory_blockers.find(
      (candidate) => candidate.rule_id === "ENTITY_LINK_AMBIGUOUS",
    );
    const orgLink = ambiguous.entity_link;
    expect(orgLink.mention_id).toBe(MENTION_ORG);
    expect(orgLink.state).toBe("ambiguous");
    expect(orgLink.active_decision_ids).toEqual([]);
    expect(orgLink.candidates).toHaveLength(2);
    const byEntityId = Object.fromEntries(
      orgLink.candidates.map((candidate) => [
        candidate.entity_id,
        candidate,
      ]),
    );
    expect(byEntityId["org:picc_full"].knowledge.same_as).toContain(
      "org:pingan_full",
    );
    expect(byEntityId["org:pingan_full"].knowledge.same_as).toContain(
      "org:picc_full",
    );
    await expect(reviewer.getByTestId("review-entity-link-ledger")).toContainText(
      "org:pingan_full",
    );
    await expect(reviewer.getByTestId("review-entity-link-ledger")).toContainText(
      "org:picc_full",
    );

    // Cross-application attempt before the first accepted correction: the
    // command is formed entirely from the live work/workspace DTOs with only
    // the mention id changed to a foreign mention.  The backend rejects it
    // with zero route/history effects.
    const workItem = await (
      await reviewer.request.get(
        `${server.baseURL}/controlled/s01/api/queries/review-work-items/${encodeURIComponent(workId)}`,
      )
    ).json();
    const crossCandidate = orgLink.candidates[0];
    const crossCommand = {
      application_id: applicationId,
      expected_fence: workItem.claim_fence,
      expected_context: workItem.command_context,
      idempotency_key: "s11-react-cross-app-attempt",
      entity_link: {
        schema_version: "entity-link-correction/1",
        finding_id: ambiguous.finding_id,
        candidate_claim_id: crossCandidate.claim_id,
        mention_id: "mention_from_another_application",
        source_evidence: orgLink.source_evidence,
        expected_active_decision_ids: orgLink.active_decision_ids,
        decision: "accept",
        entity_id: crossCandidate.entity_id,
        entity_type: crossCandidate.entity_type,
        label: crossCandidate.label,
        relationship: "same_as",
        matcher_id: crossCandidate.provenance.matcher_id,
        matcher_version: crossCandidate.provenance.matcher_version,
        knowledge_release_id:
          crossCandidate.provenance.knowledge_release_id,
        reason_code: "ENTITY_LINK_SOURCE_VERIFIED",
      },
    };
    const routeBeforeCross = await (
      await reviewer.request.get(
        `${server.baseURL}/controlled/s01/api/queries/applications/${encodeURIComponent(applicationId)}/current-route`,
      )
    ).json();
    const historyBeforeCross = await (
      await reviewer.request.get(
        `${server.baseURL}/controlled/s01/api/queries/applications/${encodeURIComponent(applicationId)}/history`,
      )
    ).json();
    const cross = await reviewer.request.post(
      `${server.baseURL}/controlled/s01/api/commands/review-work-items/${encodeURIComponent(workId)}/correct-entity-link`,
      { data: crossCommand },
    );
    expect(cross.status()).toBe(409);
    expect(await cross.json()).toEqual({
      detail: {
        error: "S03_REJECTED",
        reason_code: "ENTITY_LINK_MENTION_OUTSIDE_APPLICATION",
      },
    });
    const routeAfterCross = await (
      await reviewer.request.get(
        `${server.baseURL}/controlled/s01/api/queries/applications/${encodeURIComponent(applicationId)}/current-route`,
      )
    ).json();
    const historyAfterCross = await (
      await reviewer.request.get(
        `${server.baseURL}/controlled/s01/api/queries/applications/${encodeURIComponent(applicationId)}/history`,
      )
    ).json();
    expect(routeAfterCross).toEqual(routeBeforeCross);
    expect(historyAfterCross.entity_link_history).toEqual(
      historyBeforeCross.entity_link_history,
    );
    expect(
      historyAfterCross.entity_links.filter(
        (record) => record.record_kind === "accepted",
      ),
    ).toHaveLength(0);

    // The comparison pane shows the server ambiguous state; the opened draft
    // never preselects a candidate and submit stays disabled.
    await expect(reviewer.getByTestId("review-entity-link")).toBeVisible();
    await expect(reviewer.getByTestId("review-command-status")).toHaveAttribute(
      "role",
      "status",
    );
    await expectContained(reviewer, [
      "review-entity-link",
      "review-command-status",
    ]);
    await expectCandidateRowsContained(reviewer);
    await tabToTestId(reviewer, "review-entity-link-start");
    await reviewer.keyboard.press("Enter");
    await expect(reviewer.getByTestId("review-entity-link-form")).toBeVisible();
    await expectContained(reviewer, [
      "review-entity-link",
      "review-entity-link-form",
      "review-command-status",
    ]);
    await expectNoFormStatusOverlap(reviewer);
    const candidateSelect = reviewer.getByRole("combobox", {
      name: "候选实体",
    });
    await expect(candidateSelect).toHaveValue("");
    await expect(candidateSelect.locator('option[value=""]')).toBeDisabled();
    await expect(reviewer.getByTestId("review-entity-link-submit")).toBeDisabled();

    // Transport-unknown retry keeps the exact body and key: abort the first
    // POST, confirm the visible unknown state, then retry with the same
    // serialized command.
    await reviewer.route("**/correct-entity-link", (route) => route.abort());
    await tabToTestId(reviewer, "review-entity-link-candidate-select");
    expect(
      await reviewer.evaluate(() =>
        document.activeElement?.matches(":focus-visible"),
      ),
    ).toBe(true);
    await selectOptionByKeyboard(reviewer, candidateSelect, "s11_claim_org_picc");
    await selectOptionByKeyboard(
      reviewer,
      reviewer.getByRole("combobox", { name: "原因" }),
      "ENTITY_LINK_AMBIGUITY_RESOLVED",
    );
    await tabToTestId(reviewer, "review-entity-link-submit");
    expect(
      await reviewer.evaluate(() =>
        document.activeElement?.matches(":focus-visible"),
      ),
    ).toBe(true);
    await reviewer.keyboard.press("Enter");
    await expect(reviewer.getByTestId("review-command-status")).toContainText(
      "结果未知：网络未确认，重试将使用同一幂等键",
    );
    await reviewer.unroute("**/correct-entity-link");
    await tabToTestId(reviewer, "retry-button");
    expect(
      await reviewer.evaluate(() =>
        document.activeElement?.matches(":focus-visible"),
      ),
    ).toBe(true);
    await expectContained(reviewer, ["retry-button", "review-command-status"]);
    await reviewer.keyboard.press("Enter");
    await expect(reviewer.getByTestId("review-command-status")).toContainText(
      "实体链接已接受",
    );
    expect(capturedPosts.length).toBeGreaterThanOrEqual(2);
    const firstBody = JSON.parse(capturedPosts[0]);
    const retriedBody = JSON.parse(capturedPosts[1]);
    expect(capturedPosts[0]).toBe(capturedPosts[1]);
    expectExactEntityLinkCommand(firstBody);
    expect(firstBody.entity_link.candidate_claim_id).toBe("s11_claim_org_picc");
    expect(firstBody.entity_link.entity_id).toBe("org:picc_full");
    expect(firstBody.entity_link.reason_code).toBe(
      "ENTITY_LINK_AMBIGUITY_RESOLVED",
    );
    expect(firstBody.entity_link.mention_id).toBe(MENTION_ORG);

    // Deterministic readiness: the accepted mutation invalidated the S01
    // reads and the replacement job is still queued.
    await expect(reviewer.getByTestId("review-correction-pending")).toContainText(
      "证据修订 2",
    );
    await expect(reviewer.getByTestId("gate-phase")).toHaveText("Assembly");
    await expect(reviewer.getByTestId("gate-route")).toHaveText("pending_check");
    await expect(reviewer.getByTestId("gate-currentness")).toHaveText(
      "NO_CURRENT_RUN",
    );

    // A stale re-issue of the exact accepted command with a fresh key is a
    // conflict with no second successor.
    const staleCommand = JSON.parse(JSON.stringify(firstBody));
    staleCommand.idempotency_key = "s11-react-stale-attempt";
    const stale = await reviewer.request.post(
      `${server.baseURL}/controlled/s01/api/commands/review-work-items/${encodeURIComponent(workId)}/correct-entity-link`,
      { data: staleCommand },
    );
    expect(stale.status()).toBe(409);
    expect((await stale.json()).detail.error).toBe("S03_STALE");

    const firstWorker = await processNextJob(reviewer, server);
    await expect(reviewer.getByTestId("review-correction-converged")).toContainText(
      "证据修订 2",
    );
    await expect(
      reviewer.getByTestId("review-correction-converged"),
    ).toContainText(firstWorker.run_id);
    await expect(
      reviewer.getByTestId("review-correction-converged"),
    ).toContainText("manual_review");
    await expect(
      reviewer.getByTestId("review-correction-converged"),
    ).toHaveAttribute("role", "status");
    await expect(
      reviewer.getByTestId("review-history-run").filter({
        hasText: firstWorker.run_id,
      }),
    ).toContainText("当前");
    await expect(reviewer.getByTestId("gate-route")).toHaveText("manual_review");
    const firstHistory = await (
      await reviewer.request.get(
        `${server.baseURL}/controlled/s01/api/queries/applications/${encodeURIComponent(applicationId)}/history`,
      )
    ).json();
    expect(firstHistory.entity_link_history).toHaveLength(1);
    await expect(reviewer.getByTestId("review-history-entity-links")).toContainText(
      "active",
    );
    await expect(reviewer.getByTestId("review-history-entity-link-corrections")).toContainText(
      "ENTITY_LINK_AMBIGUITY_RESOLVED",
    );
    await expect(
      reviewer.getByTestId("review-run-entity-link-decisions"),
    ).toContainText("c-demo-entity-matcher/1");
    // Ordinary history output never re-exposes the raw mention value.
    await expect(
      reviewer.getByTestId("review-history-entity-links"),
    ).not.toContainText("人保财险");

    // Cycle 2: the unresolved low-confidence city mention.  A wrong-release
    // command (mutated matcher identity) is rejected 422 with the stable
    // reason and zero side effects; the panel then accepts the exact
    // candidate.
    const cityWork = await waitForApplicationWork(
      reviewer,
      server,
      applicationId,
      workId,
    );
    await openAndClaimWork(reviewer, server, cityWork);
    const cityItem = await (
      await reviewer.request.get(
        `${server.baseURL}/controlled/s01/api/queries/review-work-items/${encodeURIComponent(cityWork.work_item_id)}`,
      )
    ).json();
    const cityWorkspace = await (
      await reviewer.request.get(
        `${server.baseURL}/controlled/s01/api/queries/applications/${encodeURIComponent(applicationId)}/workspace`,
      )
    ).json();
    const cityFinding = cityWorkspace.mandatory_blockers.find(
      (candidate) => candidate.rule_id === "ENTITY_LINK_UNRESOLVED",
    );
    expect(cityFinding.entity_link.mention_id).toBe(MENTION_CITY);
    expect(cityFinding.entity_link.low_confidence).toBe(true);
    const cityCandidate = cityFinding.entity_link.candidates[0];
    const wrongReleaseCommand = {
      application_id: applicationId,
      expected_fence: cityItem.claim_fence,
      expected_context: cityItem.command_context,
      idempotency_key: "s11-react-wrong-release",
      entity_link: {
        schema_version: "entity-link-correction/1",
        finding_id: cityFinding.finding_id,
        candidate_claim_id: cityCandidate.claim_id,
        mention_id: MENTION_CITY,
        source_evidence: cityFinding.entity_link.source_evidence,
        expected_active_decision_ids:
          cityFinding.entity_link.active_decision_ids,
        decision: "accept",
        entity_id: cityCandidate.entity_id,
        entity_type: cityCandidate.entity_type,
        label: cityCandidate.label,
        relationship: "same_as",
        matcher_id: "c-demo-entity-matcher/unknown",
        matcher_version: cityCandidate.provenance.matcher_version,
        knowledge_release_id: cityCandidate.provenance.knowledge_release_id,
        reason_code: "ENTITY_LINK_SOURCE_VERIFIED",
      },
    };
    const wrongRelease = await reviewer.request.post(
      `${server.baseURL}/controlled/s01/api/commands/review-work-items/${encodeURIComponent(cityWork.work_item_id)}/correct-entity-link`,
      { data: wrongReleaseCommand },
    );
    expect(wrongRelease.status()).toBe(422);
    const wrongReleaseText = await wrongRelease.text();
    expect(JSON.parse(wrongReleaseText)).toEqual({
      detail: {
        error: "S03_REJECTED",
        reason_code: "ENTITY_LINK_RELEASE_MISMATCH",
      },
    });
    expect(wrongReleaseText.includes("manifest")).toBe(false);

    // The city comparison pane renders the unresolved low-confidence server
    // state; the Reviewer accepts the single candidate with the keyboard.
    await expect(reviewer.getByTestId("review-entity-link")).toContainText(
      "未解析",
    );
    await expect(
      reviewer.getByTestId("review-entity-link-low-confidence"),
    ).toBeVisible();
    await tabToTestId(reviewer, "review-entity-link-start");
    await reviewer.keyboard.press("Enter");
    await expect(reviewer.getByTestId("review-entity-link-form")).toBeVisible();
    await tabToTestId(reviewer, "review-entity-link-candidate-select");
    await selectOptionByKeyboard(
      reviewer,
      reviewer.getByRole("combobox", { name: "候选实体" }),
      cityCandidate.claim_id,
    );
    await tabToTestId(reviewer, "review-entity-link-submit");
    await reviewer.keyboard.press("Enter");
    await expect(reviewer.getByTestId("review-command-status")).toContainText(
      "实体链接已接受",
    );
    await expect(reviewer.getByTestId("review-correction-pending")).toContainText(
      "证据修订 3",
    );
    const cityWorker = await processNextJob(reviewer, server);
    await expect(reviewer.getByTestId("review-correction-converged")).toContainText(
      "证据修订 3",
    );
    await expect(
      reviewer.getByTestId("review-correction-converged"),
    ).toContainText(cityWorker.run_id);
    await expect(reviewer.getByTestId("gate-route")).toHaveText("manual_review");

    // Cycle 3: the conflicting brand mention stays explicit until a Reviewer
    // action; no client auto-link or cross-application identity appears.
    const brandWork = await waitForApplicationWork(
      reviewer,
      server,
      applicationId,
      cityWork.work_item_id,
    );
    await openAndClaimWork(reviewer, server, brandWork);
    const brandWorkspace = await (
      await reviewer.request.get(
        `${server.baseURL}/controlled/s01/api/queries/applications/${encodeURIComponent(applicationId)}/workspace`,
      )
    ).json();
    const brandFinding = brandWorkspace.mandatory_blockers.find(
      (candidate) => candidate.rule_id === "ENTITY_LINK_CONFLICT",
    );
    expect(brandFinding.entity_link.mention_id).toBe(MENTION_BRAND);
    expect(
      brandFinding.entity_link.candidates.map((candidate) => candidate.entity_id).sort(),
    ).toEqual(["brand:faw-vw", "brand:saic-vw"]);
    await expect(reviewer.getByTestId("review-entity-link")).toContainText(
      "冲突（别名互斥）",
    );
    await expect(reviewer.getByTestId("review-entity-link-ledger")).toContainText(
      "conflict_with brand:saic-vw",
    );
    const brandItem = await (
      await reviewer.request.get(
        `${server.baseURL}/controlled/s01/api/queries/review-work-items/${encodeURIComponent(brandWork.work_item_id)}`,
      )
    ).json();
    const brandCandidate = brandFinding.entity_link.candidates.find(
      (candidate) => candidate.entity_id === "brand:faw-vw",
    );
    await tabToTestId(reviewer, "review-entity-link-start");
    await reviewer.keyboard.press("Enter");
    await expect(reviewer.getByTestId("review-entity-link-form")).toBeVisible();
    await tabToTestId(reviewer, "review-entity-link-candidate-select");
    await selectOptionByKeyboard(
      reviewer,
      reviewer.getByRole("combobox", { name: "候选实体" }),
      brandCandidate.claim_id,
    );
    await tabToTestId(reviewer, "review-entity-link-submit");
    await reviewer.keyboard.press("Enter");
    await expect(reviewer.getByTestId("review-command-status")).toContainText(
      "实体链接已接受",
    );
    await expect(reviewer.getByTestId("review-correction-pending")).toContainText(
      "证据修订 4",
    );
    const brandWorker = await processNextJob(reviewer, server);
    await expect(reviewer.getByTestId("review-correction-converged")).toContainText(
      "证据修订 4",
    );
    await expect(
      reviewer.getByTestId("review-correction-converged"),
    ).toHaveAttribute("role", "status");
    await expect(
      reviewer.getByTestId("review-correction-converged"),
    ).toContainText(brandWorker.run_id);
    await expect(
      reviewer.getByTestId("review-correction-converged"),
    ).toContainText("auto_complete");
    await expect(reviewer.getByTestId("gate-route")).toHaveText("auto_complete");

    // Unauthorized scope stays an existence-hidden 404 with no candidate or
    // application identifier in the response.
    const anonContext = await browser.newContext({
      viewport: { width: viewport.width, height: viewport.height },
    });
    const anon = await anonContext.request.get(
      `${server.baseURL}/controlled/s01/api/queries/applications/${encodeURIComponent(applicationId)}/workspace`,
    );
    expect(anon.status()).toBe(404);
    const anonText = await anon.text();
    expect(anonText).toContain("S01_NOT_FOUND");
    expect(anonText).not.toContain("s11_claim_org_picc");
    expect(anonText).not.toContain("org:picc_full");
    expect(anonText).not.toContain(applicationId);
    await anonContext.close();

    // Final server facts: every run immutable, all three decisions appended,
    // the predecessor links preserved, and the route current through the
    // last fresh run.
    const history = await (
      await reviewer.request.get(
        `${server.baseURL}/controlled/s01/api/queries/applications/${encodeURIComponent(applicationId)}/history`,
      )
    ).json();
    expect(history.runs).toHaveLength(4);
    expect(history.entity_link_history).toHaveLength(3);
    expect(history.entity_links).toHaveLength(8);
    const decisions = history.entity_links.filter(
      (record) => record.record_kind === "accepted",
    );
    expect(decisions).toHaveLength(3);
    // Each decision resolves a distinct mention, so all three stay active;
    // supersession is exercised by the focused unit tests on second-decision
    // histories.
    expect(decisions.filter((record) => record.status === "active")).toHaveLength(3);
    expect(decisions.filter((record) => record.status === "superseded")).toHaveLength(0);
    const route = await (
      await reviewer.request.get(
        `${server.baseURL}/controlled/s01/api/queries/applications/${encodeURIComponent(applicationId)}/current-route`,
      )
    ).json();
    expect(route.route).toBe("auto_complete");
    expect(route.evidence_revision).toBe(4);

    // The final review shell renders the immutable history and the live
    // status region; every pane stays inside the active viewport.
    await expect(reviewer.getByTestId("review-history-entity-links")).toContainText(
      "active",
    );
    await expect(
      reviewer.getByTestId("review-history-entity-link-corrections"),
    ).toContainText("周期 1");
    // The completed flow's live status region is the server-derived
    // convergence banner (the workspace and command controls leave after
    // completion); the route/history reads stay alive in the shell.
    await expect(reviewer.getByTestId("review-correction-converged")).toContainText(
      "证据修订 4",
    );
    expect(reviewer.viewportSize()).toEqual({
      width: viewport.width,
      height: viewport.height,
    });
    await expectContained(reviewer, [
      "review-history-entity-links",
      "review-history-entity-link-corrections",
    ]);
  } catch (error) {
    failure = error;
    throw error;
  } finally {
    try {
      await settleCleanup([
        resources.reviewerContext
          ? () => resources.reviewerContext.close()
          : () => Promise.resolve(),
        resources.reciprocalRoot
          ? () =>
              fs.rmSync(resources.reciprocalRoot, {
                recursive: true,
                force: true,
              })
          : () => Promise.resolve(),
        resources.server ? () => stopServer(resources.server) : () => Promise.resolve(),
      ]);
    } catch (cleanupError) {
      if (failure === undefined) throw cleanupError;
    }
  }
}

const VIEWPORTS = [
  { width: 1280, height: 800, label: "desktop 1280x800" },
  { width: 390, height: 844, label: "mobile 390x844" },
];

for (const viewport of VIEWPORTS) {
  test(
    `S11 entity-link comparison reruns via keyboard at ${viewport.label}`,
    async ({ browser }) => {
      await runS11Flow(browser, viewport);
    },
  );
}

test("failed server startup cleans every owned artifact", async () => {
  const before = listOwnedArtifacts();
  await expect(
    startServer({ appTarget: "tests.test_s11_http:no_such_s11_app" }),
  ).rejects.toThrow(/S11 React server did not start/);
  const after = listOwnedArtifacts();
  expect(after).toEqual(before);
});

module.exports.__startServerForDebug = startServer;
