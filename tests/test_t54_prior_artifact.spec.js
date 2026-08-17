const { expect, test } = require("@playwright/test");

const BASE_URL = process.env.TASK4_T54_PRIOR_BASE_URL;
const S01_CREDENTIAL = "s01-registered-demo-test-credential";
const S02_CREDENTIAL = "t54-s02-credential";
const VIEWPORTS = [
  { width: 1280, height: 800, label: "1280x800" },
  { width: 390, height: 844, label: "390x844" },
];

/** The canonical qualified React shell contract shared by the T01 shell and
 * canonical-route contracts: no-store shell response, the #root shell element
 * and exactly one content-hashed /static/react/assets/ module reference. */
async function assertReactShell(page, response) {
  expect(response.status()).toBe(200);
  expect(response.headers()["cache-control"]).toContain("no-store");
  await expect(page.locator("#root")).toBeAttached();
  await expect(page.locator('script[src*="/static/react/assets/"]')).toHaveCount(
    1,
  );
}

test("T54 installed prior artifact serves the qualified React shell on root, S01, and S02 at both viewports", async ({
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
    await assertReactShell(root, rootResponse);
    await expect(root.getByTestId("demo-boundary-track")).toHaveText("C-DEMO");
    await expect(root.getByTestId("demo-boundary-scope")).toHaveText(
      "synthetic",
    );
    await rootContext.close();

    const s01Context = await browser.newContext({
      viewport,
      extraHTTPHeaders: { Authorization: `Bearer ${S01_CREDENTIAL}` },
    });
    const s01 = await s01Context.newPage();
    const s01Response = await s01.goto(`${BASE_URL}/controlled/s01`, {
      waitUntil: "domcontentloaded",
    });
    await assertReactShell(s01, s01Response);
    await expect(s01.getByTestId("boundary-track")).toHaveText("C-DEMO");
    await expect(s01.getByTestId("boundary-gate")).toHaveText("G2");
    await s01Context.close();

    const s02Context = await browser.newContext({
      viewport,
      extraHTTPHeaders: { Authorization: `Bearer ${S02_CREDENTIAL}` },
    });
    const s02 = await s02Context.newPage();
    const s02Response = await s02.goto(`${BASE_URL}/controlled/s02`, {
      waitUntil: "domcontentloaded",
    });
    await assertReactShell(s02, s02Response);
    await expect(s02.getByTestId("integrator-boundary-track")).toHaveText(
      "R-OBSERVED",
    );
    await expect(s02.getByTestId("integrator-boundary-gate")).toHaveText("S02");
    await s02Context.close();
  }
});
