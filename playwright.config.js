const { defineConfig } = require("@playwright/test");

module.exports = defineConfig({
  testDir: "./tests",
  testMatch: "test_s01_browser.spec.js",
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
