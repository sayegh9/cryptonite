"use client";

import { useState } from "react";
import { fetchApi } from "@/lib/api";
import { ShieldCheck, Lock, User, KeyRound, X } from "lucide-react";

export function OperatorLoginModal({ 
  onSuccess, 
  onClose 
}: { 
  onSuccess: (csrfToken: string) => void;
  onClose?: () => void;
}) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [totpCode, setTotpCode] = useState("000000");

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError("");

    try {
      const codeToSend = totpCode.trim() || "000000";
      const res = await fetchApi("/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: username.trim(), password, totp_code: codeToSend }),
      });
      if (res.success && res.csrf_token) {
        localStorage.setItem("csrf_token", res.csrf_token);
        if (typeof window !== "undefined") {
          window.dispatchEvent(new CustomEvent("cryptonite:auth-success", { detail: { csrfToken: res.csrf_token } }));
        }
        onSuccess(res.csrf_token);
      } else {
        setError("Invalid credentials.");
      }
    } catch (err: any) {
      setError(err.message || "Authentication failed. Please try again.");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm">
      <div className="bg-zinc-950 border border-zinc-800 w-full max-w-sm rounded-xl shadow-2xl overflow-hidden flex flex-col p-6 relative">
        {onClose && (
          <button
            onClick={onClose}
            className="absolute top-4 right-4 text-zinc-500 hover:text-zinc-300 transition-colors p-1"
            aria-label="Close"
          >
            <X className="w-5 h-5" />
          </button>
        )}
        <div className="flex flex-col items-center mb-6">
          <div className="p-3 bg-cyan-950/30 rounded-full mb-3">
            <ShieldCheck className="w-8 h-8 text-cyan-400" />
          </div>
          <h2 className="text-lg font-bold text-zinc-100 uppercase tracking-widest">Operator Auth</h2>
          <p className="text-xs text-zinc-500 mt-1">Authorized personnel only</p>
        </div>

        {error && (
          <div className="mb-4 bg-red-950/40 border border-red-900 text-red-400 text-xs p-3 rounded text-center">
            {error}
          </div>
        )}

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-xs font-mono text-zinc-400 mb-1">Username</label>
            <div className="relative">
              <User className="absolute left-3 top-2 w-4 h-4 text-zinc-500" />
              <input
                type="text"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                className="w-full bg-zinc-900 border border-zinc-800 rounded pl-9 pr-3 py-2 text-sm text-zinc-200 focus:border-cyan-500 focus:outline-none"
                required
              />
            </div>
          </div>
          <div>
            <label className="block text-xs font-mono text-zinc-400 mb-1">Password</label>
            <div className="relative">
              <Lock className="absolute left-3 top-2 w-4 h-4 text-zinc-500" />
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="w-full bg-zinc-900 border border-zinc-800 rounded pl-9 pr-3 py-2 text-sm text-zinc-200 focus:border-cyan-500 focus:outline-none"
                required
              />
            </div>
          </div>
          <div>
            <label className="block text-xs font-mono text-zinc-400 mb-1">TOTP Code (if registered)</label>
            <div className="relative">
              <KeyRound className="absolute left-3 top-2 w-4 h-4 text-zinc-500" />
              <input
                type="text"
                value={totpCode}
                onChange={(e) => setTotpCode(e.target.value)}
                placeholder="000000"
                className="w-full bg-zinc-900 border border-zinc-800 rounded pl-9 pr-3 py-2 text-sm text-zinc-200 focus:border-cyan-500 focus:outline-none"
              />
            </div>
          </div>

          <button
            type="submit"
            disabled={loading}
            className="w-full mt-2 px-4 py-2.5 bg-cyan-600 hover:bg-cyan-500 text-zinc-950 text-sm font-bold uppercase rounded transition-colors"
          >
            {loading ? "Authenticating..." : "Login"}
          </button>
        </form>
      </div>
    </div>
  );
}
