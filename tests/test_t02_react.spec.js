const { spawn } = require("node:child_process");
const { once } = require("node:events");
const fs = require("node:fs");
const net = require("node:net");
const path = require("node:path");

const { expect, test } = require("@playwright/test");

const ROOT = path.resolve(__dirname, "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");
const DEMO_CREDENTIAL = "s01-registered-demo-test-credential";
const OPERATOR_CREDENTIAL = "s01-registered-operator-test-credential";
const S02_CREDENTIAL = "t02-registered-s02-credential";
const S02_SUBJECT = "t02-registered-s02-reviewer";
const S02_TENANT = "tenant-t02-react";
const S02_SOURCE = "registered-t02-react-source";
const SCENARIO = "app_uncertain_ocr_noise.json";
const REACT_URL = "/controlled/s01/react";

/** Minimal S02 registered-source runtime so the legacy Reviewer shell serves
 * 200 on the same test app (the C-DEMO flow itself never touches S02). */
function createS02Fixture() {
  const root = fs.mkdtempSync(
    path.join("/tmp", `xiaopeng-task4-t02-react-s02-${process.pid}-`),
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
          workload_identity_id: "t02-react-workload",
          adapter_id: "t02-react-adapter",
          adapter_version: "1",
          source_shape: "ocr-detection/unversioned",
          producer_family: "t02-react-ocr",
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
          object_ref: "t02-react-result-object",
          media_type: "application/json",
          file: "result.json",
        },
      ],
    }),
  );
  return { root, registryPath: path.join(root, "registry.json"), objectRoot };
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

async function startServer(extraEnv = {}, options = {}) {
  const port = await reservePort();
  const s02Fixture =
    options.s02Fixture ?? createS02Fixture();
  const statePath =
    options.statePath ??
    path.join(
      "/tmp",
      `xiaopeng-task4-t02-react-${process.pid}-${port}-${Date.now()}.sqlite3`,
    );
  const output = [];
  const child = spawn(
    PYTHON,
    [
      "-m",
      "uvicorn",
      options.appTarget ?? "task4_consistency.web.app:create_s02_test_app",
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
        PYTHONPYCACHEPREFIX: "/tmp/xiaopeng-task4-t02-react-pycache",
        TASK4_S01_STATE_PATH: statePath,
        TASK4_S01_TEST_STATE_PATH: statePath,
        TASK4_S02_TEST_STATE_PATH: statePath,
        TASK4_S01_TEST_BACKGROUND_ENABLED: "1",
        TASK4_S02_TEST_BACKGROUND_ENABLED: "1",
        TASK4_S01_DEMO_CREDENTIAL: DEMO_CREDENTIAL,
        TASK4_S01_DEMO_SUBJECT: "t02-browser-reviewer",
        TASK4_S01_OPERATOR_CREDENTIAL: OPERATOR_CREDENTIAL,
        TASK4_S01_OPERATOR_SUBJECT: "t02-browser-operator",
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
  const deadline = Date.now() + 8_000;
  let ready = false;
  while (Date.now() < deadline && child.exitCode === null) {
    try {
      if ((await fetch(`${baseURL}/api/health`)).ok) {
        ready = true;
        break;
      }
    } catch (_) {
      // The bounded readiness loop owns the retry.
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  if (ready) {
    return { baseURL, child, output, statePath, s02Fixture };
  }
  const exited = once(child, "exit");
  if (child.exitCode === null) {
    child.kill("SIGKILL");
    await exited;
  }
  cleanupStatePath(statePath);
  fs.rmSync(s02Fixture.root, { recursive: true, force: true });
  throw new Error(`T02 React server did not start: ${output.join("")}`);
}

async function stopServer(server, options = {}) {
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
  if (options.preserveState !== true) {
    try {
      cleanupStatePath(server.statePath);
    } catch (error) {
      failures.push(error);
    }
  }
  try {
    if (server.s02Fixture !== undefined) {
      fs.rmSync(server.s02Fixture.root, { recursive: true, force: true });
    }
  } catch (error) {
    failures.push(error);
  }
  if (failures.length > 0) throw failures[0];
}

async function settleCleanup(cleanups) {
  const failures = [];
  for (const cleanup of cleanups) {
    try {
      await cleanup();
    } catch (error) {
      if (failures.length === 0) failures.push(error);
    }
  }
  if (failures.length > 0) throw failures[0];
}

function restrictedStrings() {
  const fixture = JSON.parse(
    fs.readFileSync(path.join(ROOT, "fixtures", "applications", SCENARIO), "utf8"),
  );
  const values = [fixture.application_id];
  for (const document of fixture.documents || []) {
    for (const field of Object.values(document.fields || {})) {
      if (typeof field.raw === "string" && field.raw.length > 1) values.push(field.raw);
    }
  }
  return values;
}

/** Submits the frozen C-DEMO scenario and waits for the background worker to
 * publish its Manual Review projection into the Reviewer queue. */
async function installManualWork(baseURL, reviewer) {
  const admission = await reviewer.request.post(
    `${baseURL}/controlled/s01/api/commands/submit`,
    { data: { scenario_id: SCENARIO, idempotency_key: "t02-react-admission" } },
  );
  expect(admission.ok()).toBeTruthy();
  const accepted = await admission.json();
  const deadline = Date.now() + 15_000;
  while (Date.now() < deadline) {
    const queue = await reviewer.request.get(
      `${baseURL}/controlled/s01/api/queries/queue`,
    );
    expect(queue.ok()).toBeTruthy();
    const body = await queue.json();
    const item = (body.items || []).find(
      (candidate) => candidate.application_id === accepted.application_id,
    );
    if (item !== undefined) return item;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`manual review work never appeared for ${accepted.application_id}`);
}

function assertNoOverflow(page) {
  return page.evaluate(
    () => document.documentElement.scrollWidth <= window.innerWidth + 1,
  );
}

function trackPageDiagnostics(page, expectations = []) {
  const browserErrors = [];
  const consoleErrors = [];
  const networkErrors = [];
  const counts = {};
  for (const expectation of expectations) counts[expectation.name] = 0;
  page.on("pageerror", (error) => browserErrors.push(error.message));
  page.on("console", (message) => {
    if (message.type() !== "error") return;
    const location = message.location().url || "";
    if (location.endsWith("/favicon.ico")) return;
    for (const expectation of expectations) {
      if (
        expectation.url !== undefined &&
        location === expectation.url &&
        message.text().includes(expectation.statusText)
      ) {
        counts[expectation.name] += 1;
        return;
      }
    }
    consoleErrors.push(`${message.text()} @ ${location}`);
  });
  page.on("requestfailed", (request) => {
    const url = request.url();
    if (url.endsWith("/favicon.ico")) return;
    networkErrors.push({
      url,
      failure: request.failure()?.errorText ?? "failed",
    });
  });
  return { browserErrors, consoleErrors, networkErrors, counts };
}

/** A test-id entry may pin a specific occurrence (per-evidence-link facts). */
async function assertControlsFitAndDoNotOverlap(page, testIds) {
  const boxes = [];
  const centerHits = [];
  for (const entry of testIds) {
    const testId = typeof entry === "string" ? entry : entry.testId;
    const index = typeof entry === "string" ? 0 : entry.index;
    let locator = page.getByTestId(testId).nth(index);
    expect(await locator.count(), `${testId} count`).toBeGreaterThan(0);
    await locator.scrollIntoViewIfNeeded();
    expect(await locator.isVisible(), `${testId} visible`).toBe(true);
    const box = await locator.boundingBox();
    expect(box, `${testId} bounding box`).not.toBeNull();
    const clip = await page.evaluate((id) => {
      const el = document.querySelector(`[data-testid="${id}"]`);
      if (!el) return null;
      return {
        scrollWidth: el.scrollWidth,
        clientWidth: el.clientWidth,
        scrollHeight: el.scrollHeight,
        clientHeight: el.clientHeight,
      };
    }, testId);
    expect(clip, `${testId} clip metrics`).not.toBeNull();
    expect(
      clip.scrollWidth,
      `${testId} horizontal clipping`,
    ).toBeLessThanOrEqual(clip.clientWidth + 1);
    if (!testId.endsWith("-panel")) {
      expect(
        clip.scrollHeight,
        `${testId} vertical clipping`,
      ).toBeLessThanOrEqual(clip.clientHeight + 1);
    }
    const scroll = await page.evaluate(() => window.scrollY);
    // Container panels may extend far below the fold; their center is not a
    // meaningful pointer target (the clipping checks below already cover
    // them).  Only interactive controls must be center-hittable.
    if (testId.endsWith("-panel")) {
      centerHits.push(Promise.resolve(true));
    } else {
      centerHits.push(
        await page.evaluate(({ testId, box }) => {
          const element = document.querySelector(`[data-testid="${testId}"]`);
          if (!element) return null;
          if (getComputedStyle(element).pointerEvents === "none") {
            const disabled =
              element.hasAttribute("disabled") ||
              element.getAttribute("aria-disabled") === "true";
            return disabled;
          }
          const hit = document.elementFromPoint(
            box.x + box.width / 2,
            box.y + box.height / 2,
          );
          return element.contains(hit);
        }, { testId, box }),
      );
    }
    boxes.push({
      testId,
      box,
      docTop: box.y + scroll,
      mustFitViewport: !testId.endsWith("-panel"),
    });
  }
  const { innerWidth, innerHeight } = await page.evaluate(() => ({
    innerWidth: window.innerWidth,
    innerHeight: window.innerHeight,
  }));
  for (const { testId, box, mustFitViewport } of boxes) {
    expect(box.x, `${testId} left edge`).toBeGreaterThanOrEqual(0);
    expect(box.y, `${testId} top edge`).toBeGreaterThanOrEqual(0);
    expect(
      box.x + box.width,
      `${testId} right edge`,
    ).toBeLessThanOrEqual(innerWidth + 1);
    if (mustFitViewport) {
      expect(
        box.y + box.height,
        `${testId} bottom edge`,
      ).toBeLessThanOrEqual(innerHeight + 1);
    }
  }
  const containsRelation = await page.evaluate(
    (ids) => {
      const byId = Object.fromEntries(
        ids.map((id) => [
          id,
          document.querySelector(`[data-testid="${id}"]`),
        ]),
      );
      const contains = {};
      for (const a of ids) {
        for (const b of ids) {
          if (a === b || !byId[a] || !byId[b]) continue;
          if (byId[a].contains(byId[b])) contains[`${a}>${b}`] = true;
        }
      }
      return contains;
    },
    boxes.map((entry) => entry.testId),
  );
  for (let i = 0; i < boxes.length; i += 1) {
    for (let j = i + 1; j < boxes.length; j += 1) {
      if (containsRelation[`${boxes[i].testId}>${boxes[j].testId}`]) continue;
      if (containsRelation[`${boxes[j].testId}>${boxes[i].testId}`]) continue;
      const a = boxes[i];
      const b = boxes[j];
      const overlap =
        a.docTop < b.docTop + b.box.height &&
        a.docTop + a.box.height > b.docTop &&
        a.box.x < b.box.x + b.box.width &&
        a.box.x + a.box.width > b.box.x;
      expect(overlap, `${a.testId} overlaps ${b.testId}`).toBe(false);
    }
  }
  for (let index = 0; index < centerHits.length; index += 1) {
    expect(await centerHits[index], `${boxes[index].testId} center hit`).toBe(
      true,
    );
  }
}

async function runFullChainTracer(browser, viewport, label) {
  const resources = {};
  let failure;
  try {
    resources.server = await startServer();
    const server = resources.server;
    resources.reviewerContext = await browser.newContext({
      viewport,
      extraHTTPHeaders: { Authorization: `Bearer ${DEMO_CREDENTIAL}` },
    });
    const reviewer = await resources.reviewerContext.newPage();
    // The workspace endpoint existence-hides once the work item completes:
    // the submit invalidation can race one last refetch into the 404, which
    // is the server-owned existence-hiding contract, not a failure.
    const workspaceGone404 = { name: "workspaceGone404", url: null, statusText: "404" };
    const diagnostics = trackPageDiagnostics(reviewer, [workspaceGone404]);
    const restricted = restrictedStrings();

    const posts = [];
    const countPosts = (page) =>
      page.on("request", (request) => {
        const url = new URL(request.url());
        if (
          request.method() === "POST" &&
          url.pathname.includes("/commands/review-work-items/")
        ) {
          posts.push({ url: url.pathname, body: request.postDataJSON() });
        }
      });

    const shellResponse = await reviewer.goto(`${server.baseURL}${REACT_URL}`, {
      waitUntil: "networkidle",
    });
    expect(shellResponse.status()).toBe(200);
    expect(shellResponse.headers()["cache-control"]).toContain("no-store");
    await expect(reviewer.getByTestId("queue-panel")).toBeVisible();
    await expect(reviewer.getByTestId("queue-empty")).toBeVisible();
    expect(await assertNoOverflow(reviewer)).toBe(true);

    const item = await installManualWork(server.baseURL, reviewer);
    const workId = item.work_item_id;
    const applicationId = item.application_id;
    // Bind the expected existence-hiding 404 to the exact workspace URL of
    // the generated application: an unrelated or late workspace 404 can no
    // longer substitute for the intended completion 404.
    workspaceGone404.url = `${server.baseURL}/controlled/s01/api/queries/applications/${encodeURIComponent(applicationId)}/workspace`;
    await reviewer.reload({ waitUntil: "networkidle" });
    const queueItem = reviewer.getByRole("link", { name: new RegExp(workId) });
    await expect(queueItem).toBeVisible();
    await expect(reviewer.getByTestId("queue-manual-link")).toHaveCount(1);
    await expect(reviewer.getByTestId("queue-manual-phase")).toHaveText(
      "Manual Review",
    );
    const restrictedInQueue = await reviewer.getByTestId("queue-panel").innerText();
    for (const value of restricted) expect(restrictedInQueue).not.toContain(value);

    countPosts(reviewer);
    // Keyboard operation: Tab reaches the manual queue link with visible
    // focus and Enter opens the review panel; the panel heading receives focus.
    await reviewer.keyboard.press("Tab");
    await expect(reviewer.locator(":focus")).toHaveAttribute(
      "href",
      expect.stringContaining(encodeURIComponent(workId)),
    );
    expect(
      await reviewer.evaluate(() => {
        const element = document.activeElement;
        if (!(element instanceof HTMLElement)) return false;
        const style = window.getComputedStyle(element);
        return style.outlineStyle !== "none" || style.boxShadow !== "none";
      }),
    ).toBe(true);
    await reviewer.keyboard.press("Enter");
    await expect(reviewer.getByTestId("review-panel")).toBeVisible();
    await expect(reviewer.locator(":focus")).toHaveText("人工核验");

    const workBeforeResponse = await reviewer.request.get(
      `${server.baseURL}/controlled/s01/api/queries/review-work-items/${encodeURIComponent(workId)}`,
    );
    expect(workBeforeResponse.ok()).toBeTruthy();
    const workBefore = await workBeforeResponse.json();
    const automaticFindingsBefore = workBefore.automatic_findings;
    expect(
      automaticFindingsBefore.find(
        (finding) => finding.rule_id === "R_ENGINE_CROSS",
      )?.verdict,
    ).toBe("uncertain");

    // Server-owned work-item facts, no Lifecycle derivation.
    await expect(reviewer.getByTestId("review-status")).toHaveText("unclaimed");
    await expect(reviewer.getByTestId("review-phase")).toHaveText("Manual Review");
    await expect(reviewer.getByTestId("review-route")).toHaveText("manual_review");
    await expect(reviewer.getByTestId("review-claim-fence")).toHaveText("0");
    const uncertainFinding = reviewer
      .getByTestId("review-finding")
      .filter({ hasText: "R_ENGINE_CROSS" });
    await expect(uncertainFinding).toHaveCount(1);
    await expect(uncertainFinding).toContainText("uncertain");
    const uncertainFindingBefore = await uncertainFinding.innerText();
    await expect(reviewer.getByTestId("review-run-digest")).toHaveText(
      /^[0-9a-f]{64}$/,
    );
    const runDigestBefore = await reviewer.getByTestId("review-run-digest").innerText();

    // Lease freshness, current revisions, and the projection watermark are
    // server facts the Reviewer must be able to read before deciding.
    await expect(reviewer.getByTestId("review-claim-expiry")).toHaveText("0");
    await expect(reviewer.getByTestId("review-lifecycle-revision")).toHaveText(
      "6",
    );
    await expect(reviewer.getByTestId("review-evidence-revision")).toHaveText(
      "1",
    );
    await expect(reviewer.getByTestId("review-workspace-expiry")).toHaveText(
      "0",
    );
    await expect(reviewer.getByTestId("review-workspace-lifecycle")).toHaveText(
      "6",
    );
    await expect(
      reviewer.getByTestId("review-workspace-evidence-revision"),
    ).toHaveText("1");
    await expect(reviewer.getByTestId("review-workspace-watermark")).toHaveText(
      "1",
    );
    await expect(reviewer.getByTestId("review-workspace-current-run")).toHaveText(
      /^run_/,
    );
    await expect(reviewer.getByTestId("review-workspace-snapshot")).toHaveText(
      /^snapshot_/,
    );

    // Finding-first workspace: the automatic finding with masked evidence.
    await expect(reviewer.getByTestId("review-workspace-rule")).toHaveText(
      "R_VIN_CROSS",
    );
    await expect(reviewer.getByTestId("review-workspace-verdict")).toHaveText(
      "uncertain",
    );
    await expect(reviewer.getByTestId("review-evidence-masked")).toHaveCount(4);
    for (const masked of await reviewer
      .getByTestId("review-evidence-masked")
      .all()) {
      await expect(masked).toHaveText("[REDACTED]");
    }
    // Masked evidence carries safe provenance and eligibility facts.
    await expect(reviewer.getByTestId("review-evidence-role").first()).toHaveText(
      "机动车登记证书",
    );
    await expect(
      reviewer.getByTestId("review-evidence-source-page").first(),
    ).toHaveText("1");
    await expect(
      reviewer.getByTestId("review-evidence-source-region").first(),
    ).toHaveText("None");
    await expect(
      reviewer.getByTestId("review-evidence-provenance").first(),
    ).toHaveText("None");
    await expect(
      reviewer.getByTestId("review-evidence-eligibility").first(),
    ).toHaveText("ineligible");
    await expect(reviewer.getByTestId("gate-phase")).toHaveText(
      "Manual Review",
    );
    await expect(reviewer.getByTestId("review-history-runs")).toBeVisible();
    const reviewerText = await reviewer.locator("body").innerText();
    for (const value of restricted) expect(reviewerText).not.toContain(value);

    await assertControlsFitAndDoNotOverlap(reviewer, [
      "queue-panel",
      "review-panel",
      "review-actions",
      "review-command-status",
      "queue-manual-link",
      "reload-button",
      "claim-button",
      "renew-button",
      "release-button",
      "submit-button",
      "review-claim-expiry",
      "review-lifecycle-revision",
      "review-evidence-revision",
      "review-workspace-expiry",
      "review-workspace-lifecycle",
      "review-workspace-evidence-revision",
      "review-workspace-watermark",
      "review-workspace-current-run",
      "review-workspace-snapshot",
      { testId: "review-evidence-role", index: 0 },
      { testId: "review-evidence-source-page", index: 0 },
      { testId: "review-evidence-source-region", index: 0 },
      { testId: "review-evidence-provenance", index: 0 },
      { testId: "review-evidence-eligibility", index: 0 },
    ]);

    // Claim -> authoritative refetch -> renew -> release -> reclaim: the fence
    // must strictly increase across the release boundary.
    await reviewer.getByRole("button", { name: "认领" }).click();
    await expect(reviewer.getByTestId("review-command-status")).toContainText(
      "认领已接受",
    );
    await expect(reviewer.getByTestId("review-status")).toHaveText("claimed");
    await expect(reviewer.getByTestId("review-claim-fence")).toHaveText("1");

    await reviewer.getByRole("button", { name: "续期" }).click();
    await expect(reviewer.getByTestId("review-command-status")).toContainText(
      "续期已接受",
    );
    await expect(reviewer.getByTestId("review-claim-fence")).toHaveText("1");

    await reviewer.getByRole("button", { name: "释放" }).click();
    await expect(reviewer.getByTestId("review-command-status")).toContainText(
      "释放已接受",
    );
    await expect(reviewer.getByTestId("review-status")).toHaveText("released");
    await expect(reviewer.getByTestId("review-claim-fence")).toHaveText("1");

    await reviewer.getByRole("button", { name: "认领" }).click();
    await expect(reviewer.getByTestId("review-command-status")).toContainText(
      "认领已接受",
    );
    await expect(reviewer.getByTestId("review-claim-fence")).toHaveText("2");

    // A post-reclaim renew is a new logical command on the live fence; it is
    // accepted with a fresh idempotency key, never a key-conflict.
    await reviewer.getByRole("button", { name: "续期" }).click();
    await expect(reviewer.getByTestId("review-command-status")).toContainText(
      "续期已接受",
    );
    await expect(reviewer.getByTestId("review-claim-fence")).toHaveText("2");

    // One allowed manual verification.  The Reviewer explicitly chooses the
    // structured disposition (never silently fixed by the UI); the POST body
    // is exactly the generated contract fields, the chosen outcome is bound
    // to the overall and every per-finding decision, and the verification
    // carries no automatic verdict.
    await expect(reviewer.getByTestId("review-outcome")).toHaveValue(
      "confirmed",
    );
    await reviewer.getByTestId("review-outcome").selectOption("inconclusive");
    await expect(reviewer.getByTestId("review-outcome")).toHaveValue(
      "inconclusive",
    );
    await assertControlsFitAndDoNotOverlap(reviewer, [
      "review-outcome",
      "submit-button",
      "renew-button",
      "release-button",
      "review-command-status",
    ]);
    await reviewer.getByRole("button", { name: "提交人工核验" }).click();
    await expect(reviewer.getByTestId("review-command-status")).toContainText(
      "核验已接受",
    );
    await expect(reviewer.getByTestId("review-status")).toHaveText("completed");

    const commandPosts = posts.filter((entry) => entry.url.endsWith("/claim")
      || entry.url.endsWith("/renew")
      || entry.url.endsWith("/release")
      || entry.url.endsWith("/submit"));
    const claimBodies = commandPosts
      .filter((entry) => entry.url.endsWith("/claim"))
      .map((entry) => entry.body);
    const renewBodies = commandPosts
      .filter((entry) => entry.url.endsWith("/renew"))
      .map((entry) => entry.body);
    const releaseBodies = commandPosts
      .filter((entry) => entry.url.endsWith("/release"))
      .map((entry) => entry.body);
    const submitBodies = commandPosts
      .filter((entry) => entry.url.endsWith("/submit"))
      .map((entry) => entry.body);
    expect(claimBodies).toHaveLength(2);
    expect(renewBodies).toHaveLength(2);
    expect(releaseBodies).toHaveLength(1);
    expect(submitBodies).toHaveLength(1);
    // Each logical command (claim, renew, release, submit) carries its own
    // key; the two renews on different fences must not share one.
    expect(renewBodies[0].idempotency_key).not.toBe(renewBodies[1].idempotency_key);
    for (const body of claimBodies) {
      expect(Object.keys(body).sort()).toEqual(["expected_context"]);
    }
    for (const body of [...renewBodies, ...releaseBodies]) {
      expect(Object.keys(body).sort()).toEqual([
        "expected_context",
        "expected_fence",
        "idempotency_key",
      ]);
    }
    const submitBody = submitBodies[0];
    expect(Object.keys(submitBody).sort()).toEqual([
      "expected_context",
      "expected_fence",
      "idempotency_key",
      "verification",
    ]);
    expect(Object.keys(submitBody.verification).sort()).toEqual([
      "finding_decisions",
      "outcome",
      "reason_code",
      "schema_version",
    ]);
    expect(submitBody.verification.outcome).toBe("inconclusive");
    expect(
      submitBody.verification.finding_decisions
        .map((decision) => decision.finding_id)
        .sort(),
    ).toEqual(
      automaticFindingsBefore.map((finding) => finding.finding_id).sort(),
    );
    for (const decision of submitBody.verification.finding_decisions) {
      expect(Object.keys(decision).sort()).toEqual(["finding_id", "outcome"]);
      expect(decision.outcome).toBe("inconclusive");
    }
    const serializedVerification = JSON.stringify(submitBody.verification);
    expect(serializedVerification).not.toContain("verdict");
    expect(serializedVerification).not.toContain("route");
    expect(serializedVerification).not.toContain("target");
    for (const value of restricted) {
      expect(JSON.stringify(submitBody)).not.toContain(value);
    }

    // Authoritative refetch: route/history from the server, automatic finding
    // and run authority unchanged.
    await expect(reviewer.getByTestId("review-run-digest")).toHaveText(
      runDigestBefore,
    );
    // The automatic uncertain finding is server authority: its complete
    // visible fact stays byte-for-byte unchanged after human completion.
    await expect(uncertainFinding).toHaveText(uncertainFindingBefore);
    const workAfterResponse = await reviewer.request.get(
      `${server.baseURL}/controlled/s01/api/queries/review-work-items/${encodeURIComponent(workId)}`,
    );
    expect(workAfterResponse.ok()).toBeTruthy();
    expect((await workAfterResponse.json()).automatic_findings).toEqual(
      automaticFindingsBefore,
    );
    await expect(reviewer.getByTestId("gate-phase")).toHaveText(
      "Verification Completed",
    );
    await expect(reviewer.getByTestId("gate-route")).toHaveText(
      "human_complete",
    );
    await expect(reviewer.getByTestId("review-history-decisions")).toContainText(
      "decision_",
    );
    await expect(reviewer.getByTestId("queue-empty")).toBeVisible();
    expect(await assertNoOverflow(reviewer)).toBe(true);

    const reviewerUrl = new URL(reviewer.url());
    expect(reviewerUrl.pathname).toBe(REACT_URL);
    expect(reviewerUrl.search).toContain(encodeURIComponent(workId));
    for (const value of restricted) expect(reviewerUrl.search).not.toContain(value);
    expect(await reviewer.evaluate(() => localStorage.length)).toBe(0);
    expect(await reviewer.evaluate(() => sessionStorage.length)).toBe(0);

    const assetRequests = await reviewer.evaluate(async () => {
      const scripts = [...document.querySelectorAll("script[src]")].map(
        (node) => node.src,
      );
      return scripts;
    });
    expect(assetRequests.length).toBeGreaterThan(0);
    for (const asset of assetRequests) {
      expect(new URL(asset).pathname.startsWith("/static/react/")).toBeTruthy();
    }
    const assetsBundle = await Promise.all(
      assetRequests.map((asset) =>
        reviewer.request.get(new URL(asset, server.baseURL).toString()),
      ),
    );
    for (const assetResponse of assetsBundle) {
      expect(assetResponse.ok()).toBeTruthy();
      expect(assetResponse.headers()["cache-control"]).toContain("immutable");
    }
    const bundleText = (await Promise.all(assetsBundle.map((r) => r.text()))).join("");
    for (const value of restricted) expect(bundleText).not.toContain(value);
    expect(bundleText).not.toContain(DEMO_CREDENTIAL);
    expect(bundleText).not.toContain(OPERATOR_CREDENTIAL);

    await assertControlsFitAndDoNotOverlap(reviewer, [
      "queue-panel",
      "review-panel",
      "review-actions",
      "review-command-status",
      "gate-panel",
      "history-panel",
      "reload-button",
      "review-claim-expiry",
      "review-lifecycle-revision",
      "review-evidence-revision",
    ]);

    // Rollback smoke: both legacy shells still serve on the same app.
    const legacyS01 = await reviewer.goto(`${server.baseURL}/controlled/s01`);
    expect(legacyS01.status()).toBe(200);
    const legacyS02 = await reviewer.request.get(
      `${server.baseURL}/controlled/s02`,
      { headers: { Authorization: `Bearer ${S02_CREDENTIAL}` } },
    );
    expect(legacyS02.ok()).toBeTruthy();
    expect(legacyS02.headers()["cache-control"]).toContain("no-store");

    expect(diagnostics.browserErrors).toEqual([]);
    expect(diagnostics.consoleErrors).toEqual([]);
    expect(diagnostics.networkErrors).toEqual([]);
    expect(diagnostics.counts.workspaceGone404).toBe(1);
  } catch (error) {
    failure = error;
    throw error;
  } finally {
    try {
      await settleCleanup([
        resources.reviewerContext
          ? () => resources.reviewerContext.close()
          : () => Promise.resolve(),
        resources.server
          ? () => stopServer(resources.server)
          : () => Promise.resolve(),
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
  test(`T02 production tracer (${viewport.label}): manual work discovery, workspace, claim lifecycle, one verification, server-owned route/history`, async ({
    browser,
  }) => {
    test.setTimeout(180_000);
    await runFullChainTracer(browser, viewport, viewport.label);
  });
}

test("T02 production tracer: expired session clears the review panel and hides work existence", async ({
  browser,
}) => {
  test.setTimeout(120_000);
  const clockPath = path.join(
    "/tmp",
    `xiaopeng-task4-t02-react-clock-${process.pid}-${Date.now()}.txt`,
  );
  fs.writeFileSync(clockPath, "100", "ascii");
  const resources = {};
  let failure;
  try {
    resources.server = await startServer(
      {
        TASK4_S01_TEST_SESSION_CLOCK_PATH: clockPath,
        TASK4_S01_TEST_SESSION_TTL_SECONDS: "10",
      },
      { appTarget: "tests.test_s01_http:create_expiring_session_app" },
    );
    const server = resources.server;
    resources.reviewerContext = await browser.newContext({
      viewport: { width: 1280, height: 800 },
      extraHTTPHeaders: { Authorization: `Bearer ${DEMO_CREDENTIAL}` },
    });
    const reviewer = await resources.reviewerContext.newPage();
    const expiry404 = { name: "hiddenWork404", url: null, statusText: "404" };
    const expiryWorkspace404 = { name: "expiryWorkspace404", url: null, statusText: "404" };
    const expiryRoute404 = { name: "expiryRoute404", url: null, statusText: "404" };
    const expiryHistory404 = { name: "expiryHistory404", url: null, statusText: "404" };
    const diagnostics = trackPageDiagnostics(reviewer, [
      expiry404,
      expiryWorkspace404,
      expiryRoute404,
      expiryHistory404,
    ]);
    const restricted = restrictedStrings();

    await reviewer.goto(`${server.baseURL}${REACT_URL}`, {
      waitUntil: "networkidle",
    });
    await expect(reviewer.getByTestId("queue-panel")).toBeVisible();

    const item = await installManualWork(server.baseURL, reviewer);
    const workId = item.work_item_id;
    const applicationId = item.application_id;
    expiry404.url = `${server.baseURL}/controlled/s01/api/queries/review-work-items/${encodeURIComponent(workId)}`;
    expiryWorkspace404.url = `${server.baseURL}/controlled/s01/api/queries/applications/${encodeURIComponent(applicationId)}/workspace`;
    expiryRoute404.url = `${server.baseURL}/controlled/s01/api/queries/applications/${encodeURIComponent(applicationId)}/current-route`;
    expiryHistory404.url = `${server.baseURL}/controlled/s01/api/queries/applications/${encodeURIComponent(applicationId)}/history`;
    await reviewer.reload({ waitUntil: "networkidle" });
    await expect(
      reviewer.getByRole("link", { name: new RegExp(workId) }),
    ).toBeVisible();

    // Open the review panel before expiry so cached identifiers are present
    // when the session ends.
    await reviewer.getByRole("link", { name: new RegExp(workId) }).click();
    await expect(reviewer.getByTestId("review-panel")).toBeVisible();
    await expect(reviewer.getByTestId("review-status")).toHaveText("unclaimed");

    // The session expires while the SPA stays open; a focus-driven refetch
    // must surface the explicit expired state, unmount the cached panel, and
    // leak no work identifiers or restricted facts.
    fs.writeFileSync(clockPath, "111", "ascii");
    await reviewer.evaluate(() =>
      window.dispatchEvent(new Event("visibilitychange")),
    );
    await expect(reviewer.getByTestId("queue-access-ended")).toBeVisible();
    await expect(reviewer.getByTestId("queue-access-ended")).toHaveText(
      /会话已过期/,
    );
    await expect(reviewer.getByTestId("review-panel")).toHaveCount(0);
    const expiredText = await reviewer.locator("body").innerText();
    for (const value of restricted) expect(expiredText).not.toContain(value);
    expect(expiredText).not.toContain(workId);
    expect(await assertNoOverflow(reviewer)).toBe(true);
    expect(diagnostics.browserErrors).toEqual([]);
    expect(diagnostics.consoleErrors).toEqual([]);
    expect(diagnostics.networkErrors).toEqual([]);
    expect(diagnostics.counts.hiddenWork404).toBe(1);
    // The dependent panel queries may race one existence-hiding 404 before
    // the work error disables them; their counts are therefore racy and are
    // only consumed by the expectations above, never asserted.
    void expiryWorkspace404;
    void expiryRoute404;
    void expiryHistory404;
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
        () => fs.unlinkSync(clockPath),
      ]);
    } catch (cleanupError) {
      if (failure === undefined) throw cleanupError;
    }
  }
});

test("T02 production tracer: unauthorized requests hide queue and work existence", async ({
  browser,
}) => {
  test.setTimeout(120_000);
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
    await reviewer.goto(`${server.baseURL}${REACT_URL}`, {
      waitUntil: "networkidle",
    });
    const item = await installManualWork(server.baseURL, reviewer);
    const workId = item.work_item_id;
    const restricted = restrictedStrings();

    // An unauthenticated shell request is refused at the page boundary.
    const anonymousContext = await browser.newContext({
      viewport: { width: 1280, height: 800 },
    });
    const anonymous = await anonymousContext.newPage();
    const refused = await anonymous.goto(`${server.baseURL}${REACT_URL}`, {
      waitUntil: "networkidle",
    });
    expect(refused.status()).toBe(403);

    // The API seam existence-hides: an empty minimized queue and 404 details.
    const queue = await anonymous.request.get(
      `${server.baseURL}/controlled/s01/api/queries/queue`,
    );
    expect(queue.ok()).toBeTruthy();
    const queueBody = await queue.json();
    expect(queueBody.items).toEqual([]);
    expect(queueBody.recovery_items).toEqual([]);
    const hiddenWork = await anonymous.request.get(
      `${server.baseURL}/controlled/s01/api/queries/review-work-items/${encodeURIComponent(workId)}`,
    );
    expect(hiddenWork.status()).toBe(404);
    expect((await hiddenWork.json()).detail).toEqual({ error: "S03_NOT_FOUND" });
    const hiddenClaim = await anonymous.request.post(
      `${server.baseURL}/controlled/s01/api/commands/review-work-items/${encodeURIComponent(workId)}/claim`,
      { data: { expected_context: { current_context: "x" } } },
    );
    expect(hiddenClaim.status()).toBe(404);
    for (const value of restricted) {
      expect(JSON.stringify(await queue.json())).not.toContain(value);
      expect(JSON.stringify(await hiddenWork.json())).not.toContain(value);
    }
    await anonymousContext.close();
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



test("T02 production tracer: a lost claim response reconciles through refetch without a second lease", async ({
  browser,
}) => {
  test.setTimeout(120_000);
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
    const restricted = restrictedStrings();
    const claimUrl = "**/controlled/s01/api/commands/review-work-items/*/claim";
    const lost502 = { name: "lostClaim502", url: null, statusText: "502" };
    const diagnostics = trackPageDiagnostics(reviewer, [lost502]);

    let claimPosts = 0;
    const claimBodies = [];
    reviewer.on("request", (request) => {
      const url = new URL(request.url());
      if (request.method() === "POST" && url.pathname.endsWith("/claim")) {
        claimPosts += 1;
        claimBodies.push(request.postDataJSON());
      }
    });

    await reviewer.goto(`${server.baseURL}${REACT_URL}`, {
      waitUntil: "networkidle",
    });
    const item = await installManualWork(server.baseURL, reviewer);
    const workId = item.work_item_id;
    lost502.url = `${server.baseURL}/controlled/s01/api/commands/review-work-items/${encodeURIComponent(workId)}/claim`;
    await reviewer.reload({ waitUntil: "networkidle" });
    await reviewer.getByRole("link", { name: new RegExp(workId) }).click();
    await expect(reviewer.getByTestId("review-panel")).toBeVisible();

    // The claim reaches the server and is accepted, but the response is lost
    // at the transport boundary (502 after commit).
    await reviewer.route(claimUrl, async (route) => {
      const response = await route.fetch();
      expect(response.ok()).toBeTruthy();
      await route.fulfill({
        status: 502,
        contentType: "application/json",
        body: JSON.stringify({ detail: { error: "S01_TRANSPORT_LOST" } }),
      });
    });
    await reviewer.getByRole("button", { name: "认领" }).click();
    await expect(reviewer.getByTestId("review-command-status")).toHaveText(
      "结果未知：网络未确认，重试将使用同一幂等键",
    );
    await expect(reviewer.getByTestId("retry-button")).toBeVisible();
    await reviewer.unroute(claimUrl);

    // The reconcile retry refetches the authority and identifies the live
    // lease instead of issuing a second claim effect.
    await reviewer.getByRole("button", { name: "重试" }).click();
    await expect(reviewer.getByTestId("review-command-status")).toHaveText(
      "认领已接受",
    );
    await expect(reviewer.getByTestId("review-status")).toHaveText("claimed");
    await expect(reviewer.getByTestId("review-claim-fence")).toHaveText("1");

    expect(claimPosts).toBe(1);
    expect(claimBodies).toHaveLength(1);
    expect(Object.keys(claimBodies[0]).sort()).toEqual(["expected_context"]);
    const reviewerText = await reviewer.locator("body").innerText();
    for (const value of restricted) expect(reviewerText).not.toContain(value);
    expect(diagnostics.browserErrors).toEqual([]);
    expect(diagnostics.networkErrors).toEqual([]);
    expect(diagnostics.counts.lostClaim502).toBe(1);
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

test("T02 production tracer: a real stale fence keeps every write fenced until an authoritative reload", async ({
  browser,
}) => {
  test.setTimeout(120_000);
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
    const stale409 = { name: "staleRenew409", url: null, statusText: "409" };
    const diagnostics = trackPageDiagnostics(reviewer, [stale409]);

    await reviewer.goto(`${server.baseURL}${REACT_URL}`, {
      waitUntil: "networkidle",
    });
    const item = await installManualWork(server.baseURL, reviewer);
    const workId = item.work_item_id;
    stale409.url = `${server.baseURL}/controlled/s01/api/commands/review-work-items/${encodeURIComponent(workId)}/renew`;
    await reviewer.reload({ waitUntil: "networkidle" });
    await reviewer.getByRole("link", { name: new RegExp(workId) }).click();
    await expect(reviewer.getByTestId("review-panel")).toBeVisible();
    await expect(reviewer.getByTestId("review-status")).toHaveText("unclaimed");

    const renewPosts = [];
    reviewer.on("request", (request) => {
      const url = new URL(request.url());
      if (request.method() === "POST" && url.pathname.endsWith("/renew")) {
        renewPosts.push(request.postDataJSON());
      }
    });

    // Claim inside the browser: fence 1 on the real authority.
    await reviewer.getByRole("button", { name: "认领" }).click();
    await expect(reviewer.getByTestId("review-command-status")).toContainText(
      "认领已接受",
    );
    await expect(reviewer.getByTestId("review-status")).toHaveText("claimed");
    await expect(reviewer.getByTestId("review-claim-fence")).toHaveText("1");

    // The same session releases the item through a second client, so the
    // browser's loaded claim data is stale against the real authority.
    const workNow = await reviewer
      .context()
      .request.get(
        `${server.baseURL}/controlled/s01/api/queries/review-work-items/${encodeURIComponent(workId)}`,
      );
    expect(workNow.ok()).toBeTruthy();
    const workBody = await workNow.json();
    expect(workBody.status).toBe("claimed");
    const releaseResponse = await reviewer
      .context()
      .request.post(
        `${server.baseURL}/controlled/s01/api/commands/review-work-items/${encodeURIComponent(workId)}/release`,
        {
          data: {
            expected_fence: 1,
            expected_context: workBody.command_context,
            idempotency_key: "t02-stale-external-release",
          },
        },
      );
    expect(releaseResponse.ok()).toBeTruthy();

    // The browser renew carries the stale fence: the real FastAPI authority
    // answers the registered 409 S03_STALE without any route interception.
    await reviewer.getByRole("button", { name: "续期" }).click();
    await expect(reviewer.getByTestId("review-command-status")).toContainText(
      "续期未接受（STALE_WORK_ITEM_CLAIM）：请重新加载权威上下文后再试",
    );
    await expect(reviewer.getByTestId("review-claim-fence")).toHaveText("1");
    // No optimistic ownership, every write (including claim) fenced, and a
    // definitive 409 exposes no retry.
    for (const name of ["认领", "续期", "释放", "提交人工核验"]) {
      await expect(reviewer.getByRole("button", { name })).toBeDisabled();
    }
    await expect(reviewer.getByTestId("retry-button")).toHaveCount(0);

    // Recovery only after the authoritative reload: the refetched state shows
    // released and the ordinary claim path re-opens.
    await reviewer.getByRole("button", { name: "重新加载" }).click();
    await expect(reviewer.getByTestId("review-status")).toHaveText("released");
    await expect(reviewer.getByRole("button", { name: "认领" })).toBeEnabled();
    await reviewer.getByRole("button", { name: "认领" }).click();
    await expect(reviewer.getByTestId("review-command-status")).toContainText(
      "认领已接受",
    );
    await expect(reviewer.getByTestId("review-claim-fence")).toHaveText("2");

    expect(renewPosts).toHaveLength(1);
    expect(Object.keys(renewPosts[0]).sort()).toEqual([
      "expected_context",
      "expected_fence",
      "idempotency_key",
    ]);
    expect(diagnostics.browserErrors).toEqual([]);
    expect(diagnostics.consoleErrors).toEqual([]);
    expect(diagnostics.networkErrors).toEqual([]);
    expect(diagnostics.counts.staleRenew409).toBe(1);
  } catch (error) {
    failure = error;
    throw error;
  } finally {
    try {
      await settleCleanup([
        resources.reviewerContext
          ? () => resources.reviewerContext.close()
          : () => Promise.resolve(),
        resources.server
          ? () => stopServer(resources.server)
          : () => Promise.resolve(),
      ]);
    } catch (cleanupError) {
      if (failure === undefined) throw cleanupError;
    }
  }
});

test("T02 production tracer: the deterministic audit fault yields a real 503 with zero side effect and recovery after authority recovery", async ({
  browser,
}) => {
  test.setTimeout(120_000);
  const resources = {};
  let failure;
  try {
    resources.server = await startServer();
    const server = resources.server;
    // A second FastAPI authority on the same state file injects the
    // deterministic review.audit fault.  The browser's claim is forwarded to
    // it, so the 503 is a real registered S03 response, never a stub.
    resources.faultServer = await startServer(
      { TASK4_S03_TEST_FAULT_POINT: "review.audit" },
      { statePath: server.statePath, s02Fixture: server.s02Fixture },
    );
    const faultServer = resources.faultServer;
    resources.reviewerContext = await browser.newContext({
      viewport: { width: 1280, height: 800 },
      extraHTTPHeaders: { Authorization: `Bearer ${DEMO_CREDENTIAL}` },
    });
    const reviewer = await resources.reviewerContext.newPage();
    const audit503 = { name: "auditClaim503", url: null, statusText: "503" };
    const diagnostics = trackPageDiagnostics(reviewer, [audit503]);

    await reviewer.goto(`${server.baseURL}${REACT_URL}`, {
      waitUntil: "networkidle",
    });
    const item = await installManualWork(server.baseURL, reviewer);
    const workId = item.work_item_id;
    audit503.url = `${server.baseURL}/controlled/s01/api/commands/review-work-items/${encodeURIComponent(workId)}/claim`;
    await reviewer.reload({ waitUntil: "networkidle" });
    await reviewer.getByRole("link", { name: new RegExp(workId) }).click();
    await expect(reviewer.getByTestId("review-panel")).toBeVisible();
    await expect(reviewer.getByTestId("review-status")).toHaveText("unclaimed");

    let claimPosts = 0;
    reviewer.on("request", (request) => {
      const url = new URL(request.url());
      if (request.method() === "POST" && url.pathname.endsWith("/claim")) {
        claimPosts += 1;
      }
    });

    const claimUrl = "**/controlled/s01/api/commands/review-work-items/*/claim";
    await reviewer.route(claimUrl, async (route) => {
      const request = route.request();
      const forwarded = await reviewer
        .context()
        .request.fetch(
          `${faultServer.baseURL}${new URL(request.url()).pathname}`,
          {
            method: request.method(),
            headers: request.headers(),
            data: request.postDataJSON(),
          },
        );
      await route.fulfill({ response: forwarded });
    });
    await reviewer.getByRole("button", { name: "认领" }).click();
    await expect(reviewer.getByTestId("review-command-status")).toContainText(
      "认领未接受（AUDIT_UNAVAILABLE）：请重新加载权威上下文后再试",
    );
    await expect(reviewer.getByTestId("review-status")).toHaveText("unclaimed");
    await expect(reviewer.getByTestId("review-claim-fence")).toHaveText("0");
    // Zero business side effect on the real authority: the work item is still
    // unclaimed and the claim button stays fenced with no retry (the
    // structured 503 proves the lease was never created).
    const after = await reviewer
      .context()
      .request.get(
        `${server.baseURL}/controlled/s01/api/queries/review-work-items/${encodeURIComponent(workId)}`,
      );
    expect(after.ok()).toBeTruthy();
    expect((await after.json()).status).toBe("unclaimed");
    await expect(reviewer.getByRole("button", { name: "认领" })).toBeDisabled();
    await expect(reviewer.getByTestId("retry-button")).toHaveCount(0);
    await reviewer.unroute(claimUrl);

    // Authority recovers: the authoritative reload refetches the main
    // authority and the allowed next action completes against it.
    await reviewer.getByRole("button", { name: "重新加载" }).click();
    await expect(reviewer.getByRole("button", { name: "认领" })).toBeEnabled();
    await reviewer.getByRole("button", { name: "认领" }).click();
    await expect(reviewer.getByTestId("review-command-status")).toContainText(
      "认领已接受",
    );
    await expect(reviewer.getByTestId("review-status")).toHaveText("claimed");
    await expect(reviewer.getByTestId("review-claim-fence")).toHaveText("1");

    expect(claimPosts).toBe(2);
    expect(diagnostics.browserErrors).toEqual([]);
    expect(diagnostics.consoleErrors).toEqual([]);
    expect(diagnostics.networkErrors).toEqual([]);
    expect(diagnostics.counts.auditClaim503).toBe(1);
  } catch (error) {
    failure = error;
    throw error;
  } finally {
    try {
      await settleCleanup([
        resources.reviewerContext
          ? () => resources.reviewerContext.close()
          : () => Promise.resolve(),
        resources.faultServer
          ? () => stopServer(resources.faultServer)
          : () => Promise.resolve(),
        resources.server
          ? () => stopServer(resources.server)
          : () => Promise.resolve(),
      ]);
    } catch (cleanupError) {
      if (failure === undefined) throw cleanupError;
    }
  }
});
