"use client";

import { useEffect } from "react";
import { useBrainStore } from "@/lib/stores/useBrainStore";
import { isBrainStreaming, loadBrainHistory, sendBrainMessage } from "./client";
import { BRAIN_ASK_EVENT, BRAIN_OPEN_TRACE_EVENT } from "./events";

let hydrated = false;

/**
 * Mount once at the page root: rehydrate the persisted Brain store after the first render (so the
 * server and client HTML match), restore the server-side session, and route the typed Brain events.
 */
export function useBrainSetup(onAsk?: () => void): void {
  useEffect(() => {
    const restore = async () => {
      if (!hydrated) {
        hydrated = true;
        await useBrainStore.persist.rehydrate();
      }
      const { sessionId, historyLoadedFor } = useBrainStore.getState();
      if (sessionId && historyLoadedFor !== sessionId && !isBrainStreaming()) await loadBrainHistory(sessionId);
    };
    void restore();
  }, []);

  useEffect(() => {
    const onBrainAsk = (e: WindowEventMap["cryptonite:brain-ask"]) => {
      const prompt = e.detail?.prompt?.trim();
      if (!prompt) return;
      onAsk?.();
      if (e.detail.send && !isBrainStreaming()) void sendBrainMessage(prompt);
      else useBrainStore.getState().setDraft(prompt);
    };
    const onOpenTrace = (e: WindowEventMap["cryptonite:brain-open-trace"]) => {
      if (e.detail?.traceId) useBrainStore.getState().openTrace(e.detail.traceId);
    };
    window.addEventListener(BRAIN_ASK_EVENT, onBrainAsk);
    window.addEventListener(BRAIN_OPEN_TRACE_EVENT, onOpenTrace);
    return () => {
      window.removeEventListener(BRAIN_ASK_EVENT, onBrainAsk);
      window.removeEventListener(BRAIN_OPEN_TRACE_EVENT, onOpenTrace);
    };
  }, [onAsk]);
}
