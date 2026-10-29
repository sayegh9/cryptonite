import type { MetadataRoute } from "next";

// PWA manifest. Icons are the Crystal Candle small cut on Brand Obsidian (docs/brand/).
export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "Cryptonite",
    short_name: "Cryptonite",
    description: "Self-hosted crypto trading workstation with AI agents and deterministic risk controls.",
    start_url: "/",
    display: "standalone",
    background_color: "#0a120e", // ds-allow-hardcode: manifest value, CSS variables don't apply here
    theme_color: "#0a120e", // ds-allow-hardcode: manifest value, CSS variables don't apply here
    icons: [
      { src: "/icon-192.png", sizes: "192x192", type: "image/png" },
      { src: "/icon-512.png", sizes: "512x512", type: "image/png" },
      { src: "/maskable-512.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
    ],
  };
}
