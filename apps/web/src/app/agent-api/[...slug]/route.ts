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

    // Allow long-running agent reasoning up to 5 minutes; a client that goes away (Stop button,
    // closed tab) aborts the upstream call too, which cancels a streaming Brain turn.
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 300_000);
    request.signal.addEventListener("abort", () => controller.abort(), { once: true });

    const backendRes = await fetch(targetUrl, {
      method: "POST",
      headers,
      body,
      signal: controller.signal,
    });

    // Server-Sent Events (Brain stream): pass the body through unbuffered
    if (backendRes.ok && backendRes.body && (backendRes.headers.get("content-type") || "").startsWith("text/event-stream")) {
      const upstream = backendRes.body;
      const relay = new ReadableStream<Uint8Array>({
        async start(ctrl) {
          const reader = upstream.getReader();
          try {
            for (;;) {
              const { done, value } = await reader.read();
              if (done) break;
              ctrl.enqueue(value);
            }
            ctrl.close();
          } catch (err) {
            ctrl.error(err);
          } finally {
            clearTimeout(timeoutId);
          }
        },
        cancel() {
          clearTimeout(timeoutId);
          controller.abort();
        },
      });
      return new NextResponse(relay, {
        status: 200,
        headers: {
          "content-type": "text/event-stream",
          "cache-control": "no-cache, no-transform",
          "x-accel-buffering": "no",
        },
      });
    }

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

export async function GET(
  request: NextRequest,
  context: { params: Promise<{ slug: string[] }> }
) {
  const { slug } = await context.params;
  const path = slug.join("/");
  
  // Extract query parameters
  const url = new URL(request.url);
  const targetUrl = `http://127.0.0.1:8000/api/v1/agents/${path}${url.search}`;

  try {
    const headers = new Headers();
    const cookie = request.headers.get("cookie");
    if (cookie) headers.set("cookie", cookie);
    
    // For GET requests, we don't strictly need CSRF or body handling
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 60_000);

    const backendRes = await fetch(targetUrl, {
      method: "GET",
      headers,
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
    console.error(`[Agent Route Proxy] GET Error forwarding to ${targetUrl}:`, error);
    return NextResponse.json(
      { detail: error.name === "AbortError" ? "Agent request timed out" : error.message },
      { status: 504 }
    );
  }
}
