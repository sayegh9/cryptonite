import { execSync } from "node:child_process";

// Where the server-side proxies reach the API: the host port by default (pnpm dev/start on the host);
// the compose `web` service sets API_ORIGIN=http://api:8000 at build and run time (rewrites are baked
// into the build).
const API_ORIGIN = process.env.API_ORIGIN || "http://127.0.0.1:8000";

function gitShortSha() {
  try {
    return execSync("git rev-parse --short HEAD", { stdio: ["ignore", "pipe", "ignore"] }).toString().trim();
  } catch {
    return null; // the Docker build context has no .git
  }
}

// Which build this is: baked into the client bundle and served by /build-id, so a tab loaded before a deploy
// sees the server move on and asks for a reload (StaleBuildBanner). Unique per build, even of the same commit.
const BUILD_ID = process.env.CRYPTONITE_BUILD_ID || `${gitShortSha() ?? "build"}-${Date.now().toString(36)}`;

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  allowedDevOrigins: ["127.0.0.1", "localhost", "192.168.1.203", "192.168.1.*"],
  env: {
    CRYPTONITE_BUILD_ID: BUILD_ID,
  },
  async rewrites() {
    return [
      {
        source: "/api/bars",
        destination: `${API_ORIGIN}/api/v1/bars`,
      },
      {
        source: "/api/signals",
        destination: `${API_ORIGIN}/api/v1/signals`,
      },
      {
        source: "/api/:path*",
        destination: `${API_ORIGIN}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
