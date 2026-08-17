const { expect, test } = require("@playwright/test");

const BASE_URL = process.env.TASK4_T54_PRIOR_BASE_URL;
const S01_CREDENTIAL = "s01-registered-demo-test-credential";
const S02_CREDENTIAL = "t54-s02-credential";
const VIEWPORTS = [
  { width: 1280, height: 800, label: "1280x800" },
  { width: 390, height: 844, label: "390x844" },
];

test("T54 installed prior artifact serves legacy root, S01, and S02 at both viewports", async ({
  browser,
}) => {
  test.skip(!BASE_URL, "executed by the installed rollback harness");
  test.setTimeout(120_000);

  for (const viewport of VIEWPORTS) {
    const rootContext = await browser.newContext({ viewport });
    const root = await rootContext.newPage();
    const rootResponse = await root.goto(`${BASE_URL}/`, {
      waitUntil: "domcontentloaded",
    });
    expect(rootResponse.status(), viewport.label).toBe(200);
    await expect(root.locator('script[src="/static/app.js"]')).toHaveCount(1);
    await expect(root.locator("#scenario-grid")).toBeVisible();
    await rootContext.close();

    const s01Context = await browser.newContext({
      viewport,
      extraHTTPHeaders: { Authorization: `Bearer ${S01_CREDENTIAL}` },
    });
    const s01 = await s01Context.newPage();
    const s01Response = await s01.goto(`${BASE_URL}/controlled/s01`, {
      waitUntil: "domcontentloaded",
    });
    expect(s01Response.status(), viewport.label).toBe(200);
    await expect(s01.locator("#normal-workbench")).toBeVisible();
    await expect(s01.locator('[data-testid="boundary-gate"]')).toContainText("G1");
    await s01Context.close();

    const s02Context = await browser.newContext({
      viewport,
      extraHTTPHeaders: { Authorization: `Bearer ${S02_CREDENTIAL}` },
    });
    const s02 = await s02Context.newPage();
    const s02Response = await s02.goto(`${BASE_URL}/controlled/s02`, {
      waitUntil: "domcontentloaded",
    });
    expect(s02Response.status(), viewport.label).toBe(200);
    await expect(s02.locator("#queue-body")).toBeVisible();
    await expect(s02.locator("#decision-form")).toBeAttached();
    await s02Context.close();
  }
});
