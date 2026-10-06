// Where the server-side proxies reach the API: the host port by default (pnpm dev/start on the host);
// the compose `web` service sets API_ORIGIN=http://api:8000 at build and run time (rewrites are baked
// into the build).
const API_ORIGIN = process.env.API_ORIGIN || "http://127.0.0.1:8000";

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  allowedDevOrigins: ["127.0.0.1", "localhost", "192.168.1.203", "192.168.1.*"],
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
