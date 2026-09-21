import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";

export async function POST(
  request: NextRequest,
  context: { params: Promise<{ slug: string[] }> }
) {
  const { slug } = await context.params;
  const path = slug.join("/");
  const targetUrl = `http://127.0.0.1:8000/api/v1/agents/${path}`;

  try {
    const body = await request.text();
    const headers = new Headers();

    const cookie = request.headers.get("cookie");
    if (cookie) headers.set("cookie", cookie);

    const csrf = request.headers.get("x-csrf-token");
    if (csrf) headers.set("x-csrf-token", csrf);

    headers.set("content-type", request.headers.get("content-type") || "application/json");

    // Allow long-running agent reasoning up to 5 minutes
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 300_000);

    const backendRes = await fetch(targetUrl, {
      method: "POST",
      headers,
      body,
      signal: controller.signal,
    });

    clearTimeout(timeoutId);

    const resText = await backendRes.text();
    return new NextResponse(resText, {
      status: backendRes.status,
      headers: {
        "content-type": backendRes.headers.get("content-type") || "application/json",
      },
    });
  } catch (error: any) {
    console.error(`[Agent Route Proxy] Error forwarding to ${targetUrl}:`, error);
    return NextResponse.json(
      { detail: error.name === "AbortError" ? "Agent reasoning timed out after 5 minutes" : error.message },
      { status: 504 }
    );
  }
}
