import { defineConfig, devices } from "@playwright/test";

const CI = !!process.env.CI;
const PORT = process.env.PLAYWRIGHT_PORT || "3000";
const BASE_URL = `http://127.0.0.1:${PORT}`;

export default defineConfig({
  testDir: "./e2e",
  timeout: 30000,
  expect: {
    timeout: 5000,
  },
  fullyParallel: false,
  workers: 1,
  // In CI a committed test.only would silently shrink the gate; fail instead.
  forbidOnly: CI,
  retries: 0,
  reporter: CI ? [["github"], ["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: BASE_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: {
    command: `./node_modules/.bin/next start -p ${PORT}`,
    url: BASE_URL,
    reuseExistingServer: !CI && !process.env.PLAYWRIGHT_PORT,
    timeout: 30000,
  },
});
