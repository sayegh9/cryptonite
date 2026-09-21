/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  allowedDevOrigins: ["127.0.0.1", "localhost", "192.168.1.203", "192.168.1.*"],
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
        source: "/api/:path*",
        destination: "http://127.0.0.1:8000/api/:path*",
      },
    ];
  },
};

export default nextConfig;
