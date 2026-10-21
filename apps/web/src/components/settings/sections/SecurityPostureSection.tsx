"use client";

import React from "react";
import { ShieldCheck, Lock, CheckCircle2, AlertTriangle, Fingerprint } from "lucide-react";
import type { RegistryEntry } from "../controls/SettingCard";
import { SettingList } from "../controls/SettingList";
import type { EnvHealth } from "../types";

export interface SecurityPostureSectionProps {
  entries: RegistryEntry[];
  envHealth: EnvHealth | null;
  onReload: () => Promise<void>;
  highlightedKey?: string;
}

export const SecurityPostureSection: React.FC<SecurityPostureSectionProps> = ({
  entries,
  envHealth,
  onReload,
  highlightedKey,
}) => {
  const authKeys = [
    "auth.session_ttl_s",
    "auth.idle_timeout_s",
    "auth.lockout_s",
    "auth.max_login_attempts",
  ];

  const authEntries = authKeys
    .map((k) => entries.find((e) => e.key === k))
    .filter((e): e is RegistryEntry => Boolean(e));

  return (
    <div className="space-y-8">
      {/* Header */}
      <div className="border-b border-white/10 pb-4">
        <h2 className="text-xl font-bold tracking-tight text-white flex items-center gap-2">
          <Fingerprint className="w-5 h-5 text-cyan-400" aria-hidden="true" />
          Security Governance & Session Policy
        </h2>
        <p className="text-xs text-zinc-400 mt-1 leading-relaxed">
          Operator 2FA enforcement, brute-force lockout backoff rules, cookie flags, and environment safety postures (ADR 0007).
        </p>
      </div>

      {/* Runtime Session & Lockout Setting Cards */}
      <section className="space-y-4">
        <div>
          <h3 className="text-sm font-bold uppercase tracking-wider text-cyan-400 flex items-center gap-2">
            <Lock className="w-4 h-4" aria-hidden="true" />
            Runtime Session Lifetimes & Lockout Thresholds
          </h3>
          <p className="text-xs text-zinc-400 mt-0.5">
            Hot-applied runtime rules. Tightening rules (shortening sessions or lengthening lockouts) is free; loosening requires operator TOTP.
          </p>
        </div>

        <SettingList
          entries={authEntries}
          allEntries={entries}
          onSaved={() => void onReload()}
          highlightedKey={highlightedKey}
        />
      </section>

      {/* Security Posture & 2FA Flags */}
      <section className="space-y-4">
        <div>
          <h3 className="text-sm font-bold uppercase tracking-wider text-purple-400 flex items-center gap-2">
            <ShieldCheck className="w-4 h-4" aria-hidden="true" />
            Environment Authentication Posture & Transport Flags
          </h3>
          <p className="text-xs text-zinc-400 mt-0.5">
            Immutable server environment variables read at boot. Modifying requires editing host <span className="font-mono text-zinc-300">.env</span> and restarting containers.
          </p>
        </div>

        <div className="grid grid-cols-1 @3xl:grid-cols-3 gap-4">
          {/* Card 1: Dev TOTP Login */}
          <div className="rounded-xl border border-white/10 bg-zinc-950/60 p-4 space-y-3 flex flex-col justify-between">
            <div>
              <div className="flex items-center justify-between">
                <span className="font-mono text-[11px] text-zinc-400">ALLOW_DEV_TOTP</span>
                <span
                  className={`text-[10px] font-bold px-2 py-0.5 rounded border uppercase tracking-wider ${
                    envHealth?.dev_totp_login
                      ? "border-amber-500/40 bg-amber-950/40 text-amber-300"
                      : "border-emerald-500/40 bg-emerald-950/40 text-emerald-300"
                  }`}
                >
                  {envHealth?.dev_totp_login ? "Active (Dev Mode)" : "Strict (Enforced)"}
                </span>
              </div>
              <h4 className="text-xs font-semibold text-zinc-200 mt-2">
                Operator Login Static 2FA Bypass
              </h4>
              <p className="text-[11px] text-zinc-400 mt-1 leading-relaxed">
                When enabled in non-production environments, allows code <span className="font-mono text-zinc-200">000000</span> for local developer testing. Automatically blocked in production.
              </p>
            </div>
            <div className="pt-2 border-t border-white/5 text-[10px] text-zinc-400">
              {envHealth?.dev_totp_login ? (
                <span className="text-amber-400 flex items-center gap-1">
                  <AlertTriangle className="w-3 h-3" aria-hidden="true" /> Enabled for local development
                </span>
              ) : (
                <span className="text-emerald-400 flex items-center gap-1">
                  <CheckCircle2 className="w-3 h-3" aria-hidden="true" /> Hardware/App TOTP strictly required
                </span>
              )}
            </div>
          </div>

          {/* Card 2: Dev TOTP Approvals */}
          <div className="rounded-xl border border-white/10 bg-zinc-950/60 p-4 space-y-3 flex flex-col justify-between">
            <div>
              <div className="flex items-center justify-between">
                <span className="font-mono text-[11px] text-zinc-400">AI_ALLOW_DEV_TOTP</span>
                <span
                  className={`text-[10px] font-bold px-2 py-0.5 rounded border uppercase tracking-wider ${
                    envHealth?.dev_totp_approvals
                      ? "border-red-500/40 bg-red-950/40 text-red-300"
                      : "border-emerald-500/40 bg-emerald-950/40 text-emerald-300"
                  }`}
                >
                  {envHealth?.dev_totp_approvals ? "Bypassed (Dev)" : "Strict 2FA"}
                </span>
              </div>
              <h4 className="text-xs font-semibold text-zinc-200 mt-2">
                AI Mandate & Route Approval 2FA
              </h4>
              <p className="text-[11px] text-zinc-400 mt-1 leading-relaxed">
                Governs whether AI route alterations and trade mandate approvals require genuine time-based cryptographic OTP generation.
              </p>
            </div>
            <div className="pt-2 border-t border-white/5 text-[10px] text-zinc-400">
              {envHealth?.dev_totp_approvals ? (
                <span className="text-red-400 flex items-center gap-1">
                  <AlertTriangle className="w-3 h-3" aria-hidden="true" /> Test OTP enabled in environment
                </span>
              ) : (
                <span className="text-emerald-400 flex items-center gap-1">
                  <CheckCircle2 className="w-3 h-3" aria-hidden="true" /> Cryptographic TOTP enforced
                </span>
              )}
            </div>
          </div>

          {/* Card 3: Secure Cookie */}
          <div className="rounded-xl border border-white/10 bg-zinc-950/60 p-4 space-y-3 flex flex-col justify-between">
            <div>
              <div className="flex items-center justify-between">
                <span className="font-mono text-[11px] text-zinc-400">SECURE_COOKIE</span>
                <span
                  className={`text-[10px] font-bold px-2 py-0.5 rounded border uppercase tracking-wider ${
                    envHealth?.secure_cookie
                      ? "border-emerald-500/40 bg-emerald-950/40 text-emerald-300"
                      : "border-zinc-700 bg-zinc-900/60 text-zinc-400"
                  }`}
                >
                  {envHealth?.secure_cookie ? "HTTPS Only (Secure)" : "HTTP / Localhost"}
                </span>
              </div>
              <h4 className="text-xs font-semibold text-zinc-200 mt-2">
                HTTP-Only Cookie SameSite Policy
              </h4>
              <p className="text-[11px] text-zinc-400 mt-1 leading-relaxed">
                Ensures operator session tokens are protected by <span className="font-mono text-zinc-200">Secure</span> and <span className="font-mono text-zinc-200">SameSite=Lax</span> flags against cross-site leakage.
              </p>
            </div>
            <div className="pt-2 border-t border-white/5 text-[10px] text-zinc-400">
              {envHealth?.secure_cookie ? (
                <span className="text-emerald-400 flex items-center gap-1">
                  <CheckCircle2 className="w-3 h-3" aria-hidden="true" /> Secure cookie transmission active
                </span>
              ) : (
                <span className="text-zinc-400 flex items-center gap-1">
                  Local HTTP development mode
                </span>
              )}
            </div>
          </div>
        </div>
      </section>
    </div>
  );
};
