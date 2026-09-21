"use client";

import { useState } from "react";
import { fetchApi } from "@/lib/api";
import { useRouter } from "next/navigation";
import { ShieldAlert } from "lucide-react";

export default function OperatorLogin() {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [totp, setTotp] = useState("000000");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const router = useRouter();

  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);

    try {
      const codeToSend = totp.trim() || "000000";
      const data = await fetchApi("/auth/login", {
        method: "POST",
        body: JSON.stringify({ username: username.trim(), password, totp_code: codeToSend }),
      });
      // Store CSRF token in localStorage for easy access
      if (data.csrf_token) {
        localStorage.setItem("csrf_token", data.csrf_token);
      }
      router.push("/operator");
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex flex-col items-center justify-center w-full max-w-md mx-auto mt-20">
      <div className="w-full bg-zinc-900 border border-red-900/50 rounded-lg shadow-2xl p-8">
        <div className="flex justify-center mb-6 text-red-500">
          <ShieldAlert className="w-16 h-16" />
        </div>
        <h2 className="text-2xl font-bold text-center text-zinc-100 mb-8 tracking-wider">
          RESTRICTED ACCESS
        </h2>

        {error && (
          <div className="bg-red-950/50 border border-red-900 text-red-400 px-4 py-3 rounded mb-6 text-sm">
            {error}
          </div>
        )}

        <form onSubmit={handleLogin} className="space-y-5">
          <div>
            <label className="block text-xs font-mono text-zinc-400 mb-1">OPERATOR ID</label>
            <input
              type="text"
              className="w-full bg-zinc-950 border border-zinc-800 rounded px-3 py-2 text-zinc-200 focus:outline-none focus:border-red-500 font-mono transition-colors"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
            />
          </div>
          <div>
            <label className="block text-xs font-mono text-zinc-400 mb-1">PASSWORD</label>
            <input
              type="password"
              className="w-full bg-zinc-950 border border-zinc-800 rounded px-3 py-2 text-zinc-200 focus:outline-none focus:border-red-500 font-mono transition-colors"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </div>
          <div>
            <label className="block text-xs font-mono text-zinc-400 mb-1">TOTP CODE (IF ENROLLED)</label>
            <input
              type="text"
              className="w-full bg-zinc-950 border border-zinc-800 rounded px-3 py-2 text-zinc-200 focus:outline-none focus:border-red-500 font-mono transition-colors"
              value={totp}
              onChange={(e) => setTotp(e.target.value)}
            />
          </div>

          <button
            type="submit"
            disabled={loading}
            className="w-full bg-red-600 hover:bg-red-500 text-white font-bold py-3 rounded uppercase tracking-widest mt-4 transition-colors disabled:opacity-50"
          >
            {loading ? "AUTHENTICATING..." : "AUTHORIZE"}
          </button>
        </form>
      </div>
    </div>
  );
}
