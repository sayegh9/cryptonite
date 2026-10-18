import React, { useState, useEffect, useRef, useId } from "react";
import { AlertTriangle, Clock, Lock, Sparkles, Sliders } from "lucide-react";
import { cn } from "@/lib/utils";
import { fetchApi } from "@/lib/api";
import { toast } from "sonner";
import { Button } from "../../ui/button";
import { TierBadge } from "./TierBadge";
import { ApplyModeBadge } from "./ApplyModeBadge";

export interface RegistryEntry {
  key: string;
  label: string;
  default: string;
  minimum: string | null;
  maximum: string | null;
  apply: "hot" | "restart" | "deploy";
  tier: string;
  tighter?: "lower" | "higher";
  writable: boolean;
  adr: string;
  store: string;
  note: string;
  decimals: number;
  value: string | null;
  value_source: "api" | "service" | "code" | "default";
  effective: string | null;
}

export interface SettingCardProps {
  entry: RegistryEntry;
  allEntries?: RegistryEntry[];
  onSaved: () => void;
  highlight?: boolean;
  className?: string;
}

const MIN_REASON_CHARS = 5;

const QUICK_REASONS = [
  "Tighten risk threshold",
  "Restore default value",
  "Volatility adjustment",
  "Operator policy update",
];

/** Converts seconds to human-readable duration string (e.g. 300 -> 5m, 43200 -> 12h, 86400 -> 24h) */
export function formatDuration(seconds: number | string): string {
  const s = Number(seconds);
  if (isNaN(s) || s < 0) return "";
  if (s === 0) return "0s";

  const days = Math.floor(s / 86400);
  const hours = Math.floor((s % 86400) / 3600);
  const minutes = Math.floor((s % 3600) / 60);
  const remSeconds = Math.floor(s % 60);

  const parts: string[] = [];
  if (days > 0) parts.push(`${days}d`);
  if (hours > 0) parts.push(`${hours}h`);
  if (minutes > 0) parts.push(`${minutes}m`);
  if (remSeconds > 0 && parts.length === 0) parts.push(`${remSeconds}s`);

  return parts.join(" ");
}

/** Formats a numeric value with human-readable duration if the key ends with `_s` */
function formatValueDisplay(key: string, val: string | null): string {
  if (val === null) return "not set";
  if (key.endsWith("_s")) {
    const dur = formatDuration(val);
    return dur ? `${val}s (${dur})` : `${val}s`;
  }
  return val;
}

/** Returns decimal validation regex according to precision */
function decimalPattern(decimals: number): RegExp {
  return decimals === 0 ? /^\d+$/ : new RegExp(`^\\d+(\\.\\d{1,${decimals}})?$`);
}

/** Returns numeric step string for inputs and sliders */
function stepFor(decimals: number): string {
  return (1 / 10 ** decimals).toFixed(decimals);
}

export const SettingCard: React.FC<SettingCardProps> = ({
  entry,
  allEntries,
  onSaved,
  highlight = false,
  className,
}) => {
  const cardRef = useRef<HTMLDivElement>(null);
  const inputId = useId();
  const sliderId = useId();
  const reasonId = useId();
  const totpId = useId();
  const errorId = useId();

  const current = entry.value ?? entry.default;
  const [amount, setAmount] = useState<string>(current);
  const [reason, setReason] = useState<string>("");
  const [totp, setTotp] = useState<string>("");
  const [saving, setSaving] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  // Sync amount with current value when entry updates
  useEffect(() => {
    setAmount(current);
  }, [current]);

  // Scroll into view when highlighted
  useEffect(() => {
    if (highlight && cardRef.current) {
      cardRef.current.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }, [highlight]);

  // Numeric and bounds validation
  const next = Number(amount);
  const hasValidFormat = decimalPattern(entry.decimals).test(amount.trim());
  const minBoundOk = entry.minimum === null || next >= Number(entry.minimum);
  const maxBoundOk = entry.maximum === null || next <= Number(entry.maximum);
  const inBounds = hasValidFormat && minBoundOk && maxBoundOk;
  const changed = inBounds && next !== Number(current);

  // Loosening detection & TOTP requirement
  const isLoosening =
    changed && (entry.tighter === "higher" ? next < Number(current) : next > Number(current));
  const needsTotp = entry.tier === "TOTP" || (entry.tier === "T/L" && isLoosening);

  // Cross-field exit rule checking (ADR 0007)
  const getExitVal = (k: string): number => {
    if (k === entry.key) {
      return isNaN(next) ? Number(current) : next;
    }
    const other = allEntries?.find((e) => e.key === k);
    return Number(other?.value ?? other?.default ?? 0);
  };

  let crossFieldWarning: string | null = null;
  if (entry.key.startsWith("exit.") && allEntries && allEntries.length > 0) {
    const stopLoss = getExitVal("exit.stop_loss_pct");
    const debateDrawdown = getExitVal("exit.debate_drawdown_pct");
    const trailActivate = getExitVal("exit.trail_activate_pct");
    const trail = getExitVal("exit.trail_pct");

    if (debateDrawdown >= stopLoss) {
      crossFieldWarning = `Cross-field rule violation: exit.debate_drawdown_pct (${debateDrawdown}%) must be strictly below exit.stop_loss_pct (${stopLoss}%). Exit debate must trigger before hard stop.`;
    } else if (trail > trailActivate) {
      crossFieldWarning = `Cross-field rule violation: exit.trail_pct (${trail}%) must not exceed exit.trail_activate_pct (${trailActivate}%). Trailing give-back cannot exceed activation gain.`;
    }
  }

  // Save capability
  const canSave =
    changed &&
    !crossFieldWarning &&
    reason.trim().length >= MIN_REASON_CHARS &&
    (!needsTotp || /^\d{6}$/.test(totp.trim())) &&
    !saving;

  const handleSave = async () => {
    setSaving(true);
    setError(null);
    try {
      await fetchApi(`/settings/registry/${entry.key}`, {
        method: "PUT",
        headers: { "X-CSRF-Token": localStorage.getItem("csrf_token") || "" },
        body: JSON.stringify({
          value: amount.trim(),
          reason: reason.trim(),
          totp_code: needsTotp ? totp.trim() : null,
        }),
      });
      toast.success(`${entry.label} saved successfully`);
      setReason("");
      setTotp("");
      onSaved();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err || "The change was not saved.");
      setError(msg.replace(/^TOTP_REQUIRED:\s*/, ""));
    } finally {
      setSaving(false);
    }
  };

  // Bounds display text
  const boundsParts: string[] = [];
  if (entry.minimum !== null) boundsParts.push(`min ${entry.minimum}`);
  if (entry.maximum !== null) boundsParts.push(`max ${entry.maximum}`);
  const boundsStr = boundsParts.join(" · ");

  // Presets generation
  const presets: { label: string; value: string }[] = [];
  presets.push({ label: `Default (${entry.default})`, value: entry.default });

  if (entry.key.endsWith("_s")) {
    const candidateSeconds = [60, 300, 900, 1800, 3600, 14400, 43200, 86400];
    const minVal = entry.minimum !== null ? Number(entry.minimum) : 0;
    const maxVal = entry.maximum !== null ? Number(entry.maximum) : Infinity;

    candidateSeconds.forEach((sec) => {
      if (sec >= minVal && sec <= maxVal && String(sec) !== entry.default) {
        presets.push({
          label: `${sec}s (${formatDuration(sec)})`,
          value: String(sec),
        });
      }
    });
  } else if (entry.decimals <= 2 && entry.minimum !== null && entry.maximum !== null) {
    const candidateVals = [1, 2, 3, 5, 10, 15, 20, 25];
    const minVal = Number(entry.minimum);
    const maxVal = Number(entry.maximum);

    candidateVals.forEach((v) => {
      const valStr = v.toFixed(entry.decimals);
      if (v >= minVal && v <= maxVal && valStr !== entry.default) {
        presets.push({ label: `${valStr}`, value: valStr });
      }
    });
  }

  // Check if effective differs from stored
  const effectiveDiffers =
    entry.effective !== null &&
    entry.value !== null &&
    entry.effective.trim() !== entry.value.trim();

  return (
    <article
      ref={cardRef}
      data-testid={`setting-card-${entry.key}`}
      className={cn(
        "rounded-xl border border-white/10 bg-zinc-950/60 p-4 sm:p-5 text-zinc-100 shadow-sm transition-all duration-200",
        highlight && "ring-2 ring-cyan-500/80 border-cyan-500/80 shadow-lg shadow-cyan-950/50",
        className
      )}
    >
      {/* Header: Label, Key, and Metadata Badges */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <h4 className="text-base font-semibold tracking-tight text-zinc-100">
            {entry.label}
          </h4>
          <div className="flex flex-wrap items-center gap-2">
            <span className="rounded border border-white/10 bg-zinc-900/80 px-2 py-0.5 font-mono text-xs text-zinc-300">
              {entry.key}
            </span>
            <TierBadge tier={entry.tier} tighter={entry.tighter} />
            <ApplyModeBadge apply={entry.apply} />
            <span
              className="rounded border border-white/10 bg-zinc-900/60 px-1.5 py-0.5 text-xs text-zinc-400"
              title="Architecture Decision Record"
            >
              ADR {entry.adr}
            </span>
            <span
              className="rounded border border-white/10 bg-zinc-900/60 px-1.5 py-0.5 font-mono text-xs text-zinc-400"
              title="Storage location"
            >
              {entry.store}
            </span>
          </div>
        </div>

        {/* Current & Effective Values Preview */}
        <div className="text-right">
          <div className="font-mono text-base font-bold text-cyan-300">
            {formatValueDisplay(entry.key, entry.value ?? entry.default)}
          </div>
          <div className="text-xs text-zinc-400">
            default {entry.default}
            {boundsStr ? ` · ${boundsStr}` : ""}
          </div>
          {entry.effective !== null && (
            <div
              className={cn(
                "mt-0.5 inline-flex items-center gap-1 font-mono text-xs",
                effectiveDiffers
                  ? "rounded border border-amber-500/40 bg-amber-950/50 px-1.5 py-0.5 text-amber-200"
                  : "text-zinc-400"
              )}
              title={
                effectiveDiffers
                  ? "The service is currently applying this value, which differs from the stored configuration."
                  : "The value the running service reports it applies."
              }
            >
              {effectiveDiffers && (
                <AlertTriangle className="size-3 text-amber-400 shrink-0" aria-hidden="true" />
              )}
              <span>applied now {formatValueDisplay(entry.key, entry.effective)}</span>
            </div>
          )}
        </div>
      </div>

      {/* Note description if provided */}
      {entry.note && (
        <p className="mt-2 text-xs leading-relaxed text-zinc-400 border-l-2 border-white/10 pl-2.5">
          {entry.note}
        </p>
      )}

      {/* Interactive Editor (When Writable) */}
      {entry.writable && (
        <div className="mt-4 rounded-lg border border-white/10 bg-zinc-900/40 p-3 sm:p-4 space-y-3.5">
          {/* Top row: Number Input + Range Slider (if bounds exist) */}
          <div className="space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <label htmlFor={inputId} className="text-xs font-medium text-zinc-300 flex items-center gap-1.5">
                <Sliders className="size-3.5 text-cyan-400" aria-hidden="true" />
                <span>New value</span>
                {boundsStr && <span className="font-mono text-[11px] text-zinc-400">({boundsStr})</span>}
              </label>

              {entry.key.endsWith("_s") && hasValidFormat && (
                <span className="font-mono text-xs text-cyan-300 flex items-center gap-1">
                  <Clock className="size-3 text-cyan-400" aria-hidden="true" />
                  <span>{formatDuration(amount)}</span>
                </span>
              )}
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-12 gap-3 items-center">
              {/* Synchronized Slider (when min & max present) */}
              {entry.minimum !== null && entry.maximum !== null ? (
                <>
                  <div className="sm:col-span-7 flex items-center gap-2">
                    <span className="font-mono text-[11px] text-zinc-400">{entry.minimum}</span>
                    <input
                      id={sliderId}
                      type="range"
                      aria-label={`${entry.label} range slider`}
                      min={entry.minimum}
                      max={entry.maximum}
                      step={stepFor(entry.decimals)}
                      value={hasValidFormat ? amount : current}
                      onChange={(e) => setAmount(e.target.value)}
                      className="w-full accent-cyan-400 bg-zinc-950 h-1.5 rounded cursor-pointer"
                    />
                    <span className="font-mono text-[11px] text-zinc-400">{entry.maximum}</span>
                  </div>
                  <div className="sm:col-span-5">
                    <input
                      id={inputId}
                      type="number"
                      inputMode="decimal"
                      step={stepFor(entry.decimals)}
                      min={entry.minimum ?? undefined}
                      max={entry.maximum ?? undefined}
                      value={amount}
                      aria-invalid={amount.trim() !== "" && !inBounds}
                      aria-errormessage={amount.trim() !== "" && !inBounds ? errorId : undefined}
                      onChange={(e) => setAmount(e.target.value)}
                      className="w-full rounded border border-white/10 bg-zinc-950 px-2.5 py-1.5 font-mono text-sm text-zinc-100 placeholder-zinc-600 focus:border-cyan-500 focus:outline-none focus:ring-1 focus:ring-cyan-500"
                    />
                  </div>
                </>
              ) : (
                <div className="sm:col-span-12">
                  <input
                    id={inputId}
                    type="number"
                    inputMode="decimal"
                    step={stepFor(entry.decimals)}
                    min={entry.minimum ?? undefined}
                    max={entry.maximum ?? undefined}
                    value={amount}
                    aria-invalid={amount.trim() !== "" && !inBounds}
                    aria-errormessage={amount.trim() !== "" && !inBounds ? errorId : undefined}
                    onChange={(e) => setAmount(e.target.value)}
                    className="w-full sm:w-48 rounded border border-white/10 bg-zinc-950 px-2.5 py-1.5 font-mono text-sm text-zinc-100 placeholder-zinc-600 focus:border-cyan-500 focus:outline-none focus:ring-1 focus:ring-cyan-500"
                  />
                </div>
              )}
            </div>

            {/* Quick Preset Chips */}
            {presets.length > 0 && (
              <div className="flex flex-wrap items-center gap-1.5 pt-1">
                <span className="text-[11px] text-zinc-400 flex items-center gap-1 mr-1">
                  <Sparkles className="size-3 text-zinc-400" aria-hidden="true" />
                  <span>Presets:</span>
                </span>
                {presets.slice(0, 6).map((preset) => (
                  <button
                    key={preset.value}
                    type="button"
                    onClick={() => setAmount(preset.value)}
                    className={cn(
                      "rounded border px-2 py-0.5 font-mono text-[11px] min-h-[24px] inline-flex items-center transition-colors cursor-pointer",
                      amount === preset.value
                        ? "border-cyan-500/60 bg-cyan-950/60 text-cyan-200"
                        : "border-white/10 bg-zinc-950/60 text-zinc-400 hover:border-white/20 hover:text-zinc-200"
                    )}
                  >
                    {preset.label}
                  </button>
                ))}
              </div>
            )}
          </div>

          {/* Real-time Inline Cross-Field Warning */}
          {crossFieldWarning && (
            <div
              role="alert"
              className="rounded-lg border border-red-500/50 bg-red-950/40 p-2.5 text-xs text-red-200 flex items-start gap-2 shadow-sm"
            >
              <AlertTriangle className="size-4 text-red-400 shrink-0 mt-0.5" aria-hidden="true" />
              <span>{crossFieldWarning}</span>
            </div>
          )}

          {/* Validation Error if out of bounds or bad format */}
          {amount.trim() !== "" && !inBounds && (
            <p id={errorId} role="alert" className="text-xs text-red-300">
              Enter a valid number{boundsStr ? ` (${boundsStr})` : ""} with at most {entry.decimals} decimal places.
            </p>
          )}

          {/* Audited Reason Input & Quick-fill Reason Pills */}
          <div className="space-y-1.5">
            <div className="flex flex-wrap items-center justify-between gap-1">
              <label htmlFor={reasonId} className="text-xs text-zinc-300">
                Reason (audited, at least {MIN_REASON_CHARS} characters)
              </label>
              <span className="text-[11px] font-mono text-zinc-400">
                {reason.trim().length}/{MIN_REASON_CHARS} min
              </span>
            </div>
            <input
              id={reasonId}
              type="text"
              maxLength={300}
              placeholder="e.g. Tighten risk threshold for increased volatility"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              className="w-full rounded border border-white/10 bg-zinc-950 px-2.5 py-1.5 text-xs text-zinc-100 placeholder-zinc-600 focus:border-cyan-500 focus:outline-none focus:ring-1 focus:ring-cyan-500"
            />
            {/* Quick Reason Pills */}
            <div className="flex flex-wrap gap-1.5 pt-0.5">
              {QUICK_REASONS.map((qr) => (
                <button
                  key={qr}
                  type="button"
                  onClick={() => setReason(qr)}
                  className="rounded border border-white/10 bg-zinc-950/70 px-2 py-0.5 text-[11px] min-h-[24px] inline-flex items-center text-zinc-400 hover:border-cyan-500/40 hover:text-cyan-200 transition-colors cursor-pointer"
                >
                  + {qr}
                </button>
              ))}
            </div>
          </div>

          {/* Conditional TOTP 6-digit Input */}
          {needsTotp && (
            <div className="rounded-lg border border-amber-500/30 bg-amber-950/20 p-3 space-y-1.5">
              <label htmlFor={totpId} className="flex items-center gap-1.5 text-xs font-medium text-amber-200">
                <Lock className="size-3.5 text-amber-400" aria-hidden="true" />
                <span>
                  Your TOTP code (
                  {entry.tier === "TOTP"
                    ? "TOTP tier requires 2FA for all writes"
                    : "loosening risk parameters requires 2FA"}
                  )
                </span>
              </label>
              <input
                id={totpId}
                inputMode="numeric"
                autoComplete="one-time-code"
                maxLength={6}
                placeholder="6-digit code"
                value={totp}
                onChange={(e) => setTotp(e.target.value.replace(/\D/g, ""))}
                className="w-40 rounded border border-amber-500/40 bg-zinc-950 px-2.5 py-1.5 font-mono text-sm tracking-widest text-amber-100 placeholder-zinc-700 focus:border-amber-400 focus:outline-none focus:ring-1 focus:ring-amber-400"
              />
            </div>
          )}

          {/* API Error Box */}
          {error && (
            <div role="alert" className="rounded border border-red-500/40 bg-red-950/50 p-2 text-xs text-red-200">
              {error}
            </div>
          )}

          {/* Save Action Button */}
          <div className="flex items-center justify-between pt-1">
            <span className="text-[11px] text-zinc-400">
              {isLoosening && (
                <span className="text-amber-300 font-medium">Loosening change detected</span>
              )}
            </span>

            <Button
              onClick={() => void handleSave()}
              disabled={!canSave}
              className={cn(
                "font-bold uppercase tracking-wider text-xs px-4 py-2 transition-all cursor-pointer",
                needsTotp
                  ? "bg-amber-500 text-zinc-950 hover:bg-amber-400 disabled:opacity-40"
                  : "bg-cyan-500 text-zinc-950 hover:bg-cyan-400 disabled:opacity-40"
              )}
            >
              {saving ? "Saving…" : needsTotp ? "Save with TOTP" : "Save"}
            </Button>
          </div>
        </div>
      )}
    </article>
  );
};

export default SettingCard;
