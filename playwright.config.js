const { defineConfig } = require("@playwright/test");

module.exports = defineConfig({
  testDir: "./tests",
  testMatch: [
    "test_s01_browser.spec.js",
    "test_s02_browser.spec.js",
    "test_s03_browser.spec.js",
    "test_s05_browser.spec.js",
    "test_s06_browser.spec.js",
    "test_s07_browser.spec.js",
    "test_t01_react.spec.js",
    "test_t02_react.spec.js",
  ],
  fullyParallel: false,
  workers: 1,
  timeout: 30_000,
  expect: {
    timeout: 5_000,
  },
  outputDir: "/tmp/xiaopeng-task4-s01-playwright-artifacts",
  reporter: "line",
  use: {
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    video: "off",
  },
});
