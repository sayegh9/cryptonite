import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

/**
 * The build this server runs (`CRYPTONITE_BUILD_ID`, baked in by next.config.mjs). A tab compares it with the build it
 * loaded and asks for a reload when they differ (StaleBuildBanner). Outside /api so the API proxy never takes it.
 */
export function GET() {
  return NextResponse.json(
    { build_id: process.env.CRYPTONITE_BUILD_ID ?? null },
    { headers: { "cache-control": "no-store" } },
  );
}
