/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  allowedDevOrigins: ["127.0.0.1", "localhost"],
  async rewrites() {
    return [
      {
        source: "/api/bars",
        destination: "http://127.0.0.1:8000/api/v1/bars",
      },
      {
        source: "/api/signals",
        destination: "http://127.0.0.1:8000/api/v1/signals",
      },
      {
        source: "/api/v1/agents/:path*",
        destination: "http://127.0.0.1:8085/api/v1/agents/:path*",
      },
      {
        source: "/api/:path*",
        destination: "http://127.0.0.1:8000/api/:path*",
      },
    ];
  },
};

export default nextConfig;
