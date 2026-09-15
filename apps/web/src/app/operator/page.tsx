"use client";

import { useEffect, useState } from "react";
import { fetchApi } from "@/lib/api";
import { useRouter } from "next/navigation";
import { KillSwitchPanel } from "@/components/operator/KillSwitchPanel";
import { ReconciliationLedger } from "@/components/operator/ReconciliationLedger";

import { StrategyInstancesPanel } from "@/components/operator/StrategyInstancesPanel";

export default function OperatorDashboard() {
  const [loading, setLoading] = useState(true);
  const [username, setUsername] = useState("");
  const [executionMode, setExecutionMode] = useState("UNKNOWN");
  const router = useRouter();

  useEffect(() => {
    // Check if authenticated
    fetchApi("/auth/me")
      .then((data) => {
        if (!data.authenticated) {
          router.push("/operator/login");
        } else {
          setUsername(data.username);
          setLoading(false);
          fetchApi("/control/status").then(res => setExecutionMode(res.execution_mode)).catch(() => {});
          if (data.csrf_token) {
            localStorage.setItem("csrf_token", data.csrf_token);
          }
        }
      })
      .catch(() => {
        router.push("/operator/login");
      });
  }, [router]);

  if (loading) {
    return <div className="p-8 text-zinc-400 font-mono animate-pulse">Verifying operator session...</div>;
  }

  return (
    <div className="max-w-6xl mx-auto space-y-6">
      <div className="flex items-center justify-between mb-8">
        <h1 className="text-2xl font-bold text-zinc-100 tracking-wide flex items-center space-x-3">
          <span>Dashboard Overview</span>
          <span className={`text-[10px] px-2 py-1 rounded font-mono font-bold uppercase tracking-widest ${executionMode === 'LIVE_TESTNET' ? 'bg-rose-950 text-rose-400 border border-rose-800 animate-pulse' : 'bg-emerald-950 text-emerald-400 border border-emerald-800'}`}>
            {executionMode === 'LIVE_TESTNET' ? 'LIVE (TESTNET)' : 'MOCK ENGINE'}
          </span>
        </h1>
        <div className="bg-zinc-900 border border-zinc-800 px-3 py-1.5 rounded text-xs font-mono text-zinc-400">
          OPERATOR: <span className="text-cyan-400 font-bold">{username}</span>
        </div>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-3 gap-6">
        {/* Left Column: Kill Switch & Active Strategies */}
        <div className="xl:col-span-1 space-y-6">
          <KillSwitchPanel />
          <StrategyInstancesPanel />
        </div>

        {/* Right Column: Ledger / Diagnostics */}
        <div className="xl:col-span-2 space-y-6">
          <ReconciliationLedger />
          
          <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-5">
            <h3 className="text-xs font-mono uppercase tracking-wider text-zinc-400 mb-3">
              Environment & Infrastructure Telemetry
            </h3>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs font-mono">
              <div className="bg-zinc-950 p-2.5 rounded border border-zinc-800/80">
                <span className="text-zinc-500 block text-[10px]">ENVIRONMENT</span>
                <span className="text-emerald-400 font-bold">BINANCE DEMO</span>
              </div>
              <div className="bg-zinc-950 p-2.5 rounded border border-zinc-800/80">
                <span className="text-zinc-500 block text-[10px]">LOGICAL ACCOUNT</span>
                <span className="text-zinc-200 font-bold">DEMO-ACCOUNT-001</span>
              </div>
              <div className="bg-zinc-950 p-2.5 rounded border border-zinc-800/80">
                <span className="text-zinc-500 block text-[10px]">ADVISORY LOCK</span>
                <span className="text-cyan-400 font-bold">ACTIVE (PG 16)</span>
              </div>
              <div className="bg-zinc-950 p-2.5 rounded border border-zinc-800/80">
                <span className="text-zinc-500 block text-[10px]">SESSION STORAGE</span>
                <span className="text-emerald-400 font-bold">REDIS 7 (HTTPONLY)</span>
              </div>
            </div>
            <div className="mt-4 pt-3 border-t border-zinc-800/60 flex items-center justify-between text-xs text-zinc-500">
              <span>Cryptonite Operator Control Plane • v0.2.0</span>
              <a
                href="/"
                className="text-cyan-400 hover:text-cyan-300 transition-colors font-medium flex items-center space-x-1"
              >
                <span>Switch to Trading Terminal &rarr;</span>
              </a>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
