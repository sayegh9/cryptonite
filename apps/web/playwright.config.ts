import { defineConfig, devices } from "@playwright/test";

const CI = !!process.env.CI;

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
    baseURL: "http://127.0.0.1:3000",
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
    command: "pnpm --pm-on-fail=ignore start",
    url: "http://127.0.0.1:3000",
    reuseExistingServer: !CI,
    timeout: 30000,
  },
});
