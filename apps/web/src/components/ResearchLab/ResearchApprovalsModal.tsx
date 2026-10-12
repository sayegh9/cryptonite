"use client";

import { useEffect, useState } from "react";
import { XCircle, RefreshCw } from "lucide-react";

interface ResearchApproval {
  id: string;
  kind: "DRAFT_STRATEGY" | "QUEUE_RESEARCH";
  status: string;
  summary: string;
  payload: Record<string, any>;
  requested_by: string;
  created_at: string;
}

/** Research work that waits for the operator: a drafted strategy (nothing is saved or backtested until it is
 *  approved) and an idea the Brain wants researched (nothing is queued until it is approved). */
export function ResearchApprovalsModal({ onClose, onDecided }: { onClose: () => void; onDecided?: () => void }) {
  const [approvals, setApprovals] = useState<ResearchApproval[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch("/api/v1/research/approvals?status=PENDING", { credentials: "include" });
      if (!res.ok) {
        setError(`Could not load approvals (HTTP ${res.status})`);
        return;
      }
      setApprovals((await res.json()).approvals ?? []);
    } catch (e: any) {
      setError("Could not reach the API: " + (e?.message ?? "unknown error"));
    } finally {
      setLoading(false);
    }
  };

  const decide = async (id: string, decision: "APPROVE" | "REJECT") => {
    setBusyId(id);
    setError(null);
    try {
      const res = await fetch(`/api/v1/research/approvals/${id}/decision`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": localStorage.getItem("csrf_token") ?? "" },
        body: JSON.stringify({ decision }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        setError(typeof body.detail === "string" ? body.detail : `Decision failed (HTTP ${res.status})`);
        return;
      }
      setApprovals((prev) => prev.filter((a) => a.id !== id));
      onDecided?.();
    } catch (e: any) {
      setError("Could not reach the API: " + (e?.message ?? "unknown error"));
    } finally {
      setBusyId(null);
    }
  };

  useEffect(() => {
    load();
  }, []);

  return (
    <div className="fixed inset-0 bg-black/75 backdrop-blur-sm z-50 flex items-center justify-center p-4 font-mono" onClick={onClose}>
      <div
        className="w-full max-w-3xl bg-zinc-950 border border-zinc-800 rounded-xl shadow-2xl overflow-hidden flex flex-col h-[80vh]"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-label="Research approvals"
      >
        <div className="p-4 border-b border-zinc-800 flex justify-between items-center bg-zinc-900/60">
          <div>
            <h2 className="font-bold text-white text-sm">RESEARCH APPROVALS</h2>
            <p className="text-2xs text-zinc-400">Nothing here runs until you approve it.</p>
          </div>
          <div className="flex items-center space-x-2">
            <button onClick={load} className="p-1.5 bg-zinc-900 hover:bg-zinc-800 text-zinc-400 rounded border border-zinc-800" title="Refresh">
              <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`} />
            </button>
            <button onClick={onClose} className="text-zinc-500 hover:text-white p-1 rounded hover:bg-zinc-900" aria-label="Close approvals">
              <XCircle className="w-5 h-5" />
            </button>
          </div>
        </div>

        <div className="flex-1 overflow-y-auto p-4 space-y-3">
          {error && <div role="alert" className="text-rose-300 text-xs">{error}</div>}
          {!error && !loading && approvals.length === 0 && (
            <div className="text-xs text-zinc-500">No research waiting for approval.</div>
          )}
          {approvals.map((a) => (
            <div key={a.id} className="border border-zinc-800 rounded-lg bg-zinc-900/40 p-3 space-y-2">
              <div className="flex items-center justify-between">
                <span className="text-2xs font-bold text-cyan-300">
                  {a.kind === "DRAFT_STRATEGY" ? "DRAFTED STRATEGY" : "RESEARCH REQUEST"}
                </span>
                <span className="text-2xs text-zinc-500">{new Date(a.created_at).toLocaleString()}</span>
              </div>
              <div className="text-xs text-zinc-100">{a.summary}</div>
              {a.kind === "DRAFT_STRATEGY" && a.payload.draft && (
                <details className="text-2xs text-zinc-300">
                  <summary className="cursor-pointer text-zinc-400">Show the code</summary>
                  <div className="text-zinc-400 mt-1">{a.payload.draft.description}</div>
                  <pre className="mt-1 max-h-64 overflow-auto bg-zinc-950 border border-zinc-800 rounded p-2 whitespace-pre-wrap">
                    {a.payload.draft.python_code}
                  </pre>
                </details>
              )}
              {a.kind === "QUEUE_RESEARCH" && (
                <div className="text-2xs text-zinc-400">
                  {a.payload.instrument_id} · {a.payload.timeframe} · requested by {a.requested_by}
                </div>
              )}
              <div className="flex justify-end space-x-2">
                <button
                  disabled={busyId === a.id}
                  onClick={() => decide(a.id, "REJECT")}
                  className="px-3 py-1.5 bg-zinc-900 hover:bg-zinc-800 border border-zinc-800 text-zinc-300 rounded text-xs font-bold disabled:opacity-40"
                >
                  Reject
                </button>
                <button
                  disabled={busyId === a.id}
                  onClick={() => decide(a.id, "APPROVE")}
                  className="px-3 py-1.5 bg-cyan-700 hover:bg-cyan-600 text-white rounded text-xs font-bold disabled:opacity-40"
                >
                  Approve
                </button>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
