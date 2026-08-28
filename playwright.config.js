const { defineConfig } = require("@playwright/test");

module.exports = defineConfig({
  testDir: "./tests",
  testMatch: [
    "test_s05_browser.spec.js",
    "test_s06_browser.spec.js",
    "test_s10_react.spec.js",
    "test_s11_react.spec.js",
    "test_t01_react.spec.js",
    "test_t02_react.spec.js",
    "test_t03_react.spec.js",
    "test_t06_react.spec.js",
    "test_t07_react.spec.js",
    "test_t08_react.spec.js",
    "test_t14_s12_react.spec.js",
    "test_t15_react.spec.js",
    "test_t16_react.spec.js",
    "test_t17_react.spec.js",
    "test_t54_prior_artifact.spec.js",
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
