import type { BrainStreamEvent } from "./types";

const KNOWN = new Set(["session", "token", "tool_call", "widget", "done", "error"]);

/** Parse one SSE frame ("event: x\ndata: {...}"). Unknown or malformed frames return null. */
export function parseSseFrame(frame: string): BrainStreamEvent | null {
  let event = "message";
  const data: string[] = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  }
  if (!KNOWN.has(event) || data.length === 0) return null;
  try {
    return { type: event, data: JSON.parse(data.join("\n")) } as BrainStreamEvent;
  } catch {
    return null;
  }
}

/** Read a fetch Response body as SSE and call `onEvent` for each typed event. */
export async function readSse(res: Response, onEvent: (ev: BrainStreamEvent) => void): Promise<void> {
  if (!res.body) return;
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
    let cut = buffer.indexOf("\n\n");
    while (cut !== -1) {
      const ev = parseSseFrame(buffer.slice(0, cut));
      buffer = buffer.slice(cut + 2);
      if (ev) onEvent(ev);
      cut = buffer.indexOf("\n\n");
    }
  }
  const tail = parseSseFrame(buffer.trim());
  if (tail) onEvent(tail);
}
