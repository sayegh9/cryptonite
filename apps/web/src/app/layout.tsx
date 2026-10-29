import type { Metadata, Viewport } from "next";
import "./globals.css";
import localFont from "next/font/local";
import { cn } from "@/lib/utils";
import { Toaster } from "@/components/ui/sonner";
import { CommandPalette } from "@/components/CommandPalette";

const geist = localFont({
  src: "../../public/fonts/geist-latin.woff2",
  variable: "--font-sans",
});

export const metadata: Metadata = {
  title: "Cryptonite Terminal | Market Intelligence & Polars Scanner",
  description: "Self-hosted crypto intelligence, multi-chart terminal, and vectorized Polars market scanner.",
  applicationName: "Cryptonite",
};

// Brand Obsidian (--brand-obsidian); the favicon, apple-icon and manifest icons live next to this file.
export const viewport: Viewport = {
  themeColor: "#0a120e", // ds-allow-hardcode: meta tag value, CSS variables don't apply here
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className={cn("dark", "font-sans", geist.variable)}>
      <body className="bg-zinc-950 text-zinc-100 min-h-screen antialiased overflow-hidden">
        {children}
        <Toaster theme="dark" position="bottom-right" />
        <CommandPalette />
      </body>
    </html>
  );
}
