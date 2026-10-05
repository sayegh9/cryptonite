import { test as base, expect } from "@playwright/test";

/**
 * Backend-offline fallback for the CI smoke subset (`pnpm e2e:smoke`): every API call a spec does not stub
 * gets a 503, and the :8000 stream socket is mocked with no messages, so the page sees the same offline
 * state in CI and on a machine where the dev backend happens to be running. Context routes run after the
 * spec's own page.route stubs, so a spec's stubs always win.
 */
const isBackend = (url: URL) => url.port === "8000" || /^\/(api|agent-api)\//.test(url.pathname);

export const test = base.extend({
  context: async ({ context }, use) => {
    await context.route(isBackend, (route) =>
      route.fulfill({ status: 503, json: { detail: "backend offline (e2e smoke)" } }),
    );
    await context.routeWebSocket(/:8000\//, () => {
      // Mocked and silent: no server connection, no messages.
    });
    await use(context);
  },
});

export { expect };
