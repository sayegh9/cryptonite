import React, { useState, useEffect, useRef, useId } from "react";
import { AlertTriangle, Clock, Lock, Pencil, Sparkles, Sliders } from "lucide-react";
import { cn } from "@/lib/utils";
import { fetchApi } from "@/lib/api";
import { toast } from "sonner";
import { Button } from "../../ui/button";
import { TierBadge } from "./TierBadge";
import { ApplyModeBadge } from "./ApplyModeBadge";
import { STALE_BUILD_TITLE, useStaleBuild } from "@/lib/stores/useBuildStore";

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
  /** An enum stored as a number: the stored value and the label to show (the registry stays numeric) */
  choices?: { value: string; label: string }[];
}

export interface SettingCardProps {
  entry: RegistryEntry;
  allEntries?: RegistryEntry[];
  onSaved: () => void;
  highlight?: boolean;
  className?: string;
  /** Controlled editor state; a SettingList uses it so only one row of a group is open at a time. Uncontrolled when omitted. */
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
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
export function formatValueDisplay(key: string, val: string | null): string {
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

/** The value as an operator reads it: the choice label for an enum, otherwise the number (with a duration for `_s` keys) */
export function displayEntryValue(entry: RegistryEntry, val: string | null): string {
  if (val === null) return "not set";
  const choice = entry.choices?.find((c) => c.value === val);
  if (entry.choices && entry.choices.length > 0) return choice?.label ?? val;
  return formatValueDisplay(entry.key, val);
}

export const SettingCard: React.FC<SettingCardProps> = ({
  entry,
  allEntries,
  onSaved,
  highlight = false,
  className,
  open: openProp,
  onOpenChange,
}) => {
  const cardRef = useRef<HTMLElement>(null);
  const editorRef = useRef<HTMLDivElement>(null);
  const editBtnRef = useRef<HTMLButtonElement>(null);
  const noteRef = useRef<HTMLParagraphElement>(null);
  const focusOnOpen = useRef(false);
  const editorId = useId();
  const noteId = useId();
  const inputId = useId();
  const sliderId = useId();
  const reasonId = useId();
  const totpId = useId();
  const errorId = useId();

  const choices = entry.choices ?? [];
  const isChoice = choices.length > 0;
  const choiceLabel = (v: string | null): string => (v === null ? "not set" : (choices.find((c) => c.value === v)?.label ?? v));
  const current = entry.value ?? entry.default;
  const [amount, setAmount] = useState<string>(current);
  const [reason, setReason] = useState<string>("");
  const [totp, setTotp] = useState<string>("");
  const [saving, setSaving] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [innerOpen, setInnerOpen] = useState<boolean>(false);
  const [noteExpanded, setNoteExpanded] = useState<boolean>(false);
  const [noteClamped, setNoteClamped] = useState<boolean>(false);
  const open = entry.writable && (openProp ?? innerOpen);
  const showFullNote = noteExpanded || open;

  const setOpen = (next: boolean) => {
    if (openProp === undefined) setInnerOpen(next);
    onOpenChange?.(next);
  };

  /** Closing from inside the editor (Cancel, Escape, a saved change) hands focus back to the Edit button */
  const closeEditor = () => {
    setOpen(false);
    requestAnimationFrame(() => editBtnRef.current?.focus());
  };

  const toggleEditor = () => {
    if (open) {
      setOpen(false);
      return;
    }
    focusOnOpen.current = true;
    setOpen(true);
  };

  // Opening the editor from the Edit button moves focus to its first control
  useEffect(() => {
    if (!open || !focusOnOpen.current) return;
    focusOnOpen.current = false;
    const target = editorRef.current?.querySelector<HTMLElement>(
      'input[type="number"], button[role="radio"][aria-checked="true"], button[role="radio"]'
    );
    target?.focus();
  }, [open]);

  // Does the one-line description cut off? Only the collapsed row measures it.
  useEffect(() => {
    const el = noteRef.current;
    if (!el || showFullNote) return;
    const measure = () => setNoteClamped(el.scrollWidth > el.clientWidth + 1);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [showFullNote, entry.note]);
  // A TOTP change never runs from a tab older than the server; tightening (no TOTP) stays open
  const staleBuild = useStaleBuild();

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
    !(needsTotp && staleBuild) &&
    !saving;

  const handleCancel = () => {
    setAmount(current);
    setReason("");
    setTotp("");
    setError(null);
    closeEditor();
  };

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
      closeEditor();
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
  const boundsStr = isChoice ? "" : boundsParts.join(" · ");
  // The compact range shown beside the default on the collapsed row
  const rangeStr =
    isChoice || (entry.minimum === null && entry.maximum === null)
      ? ""
      : entry.minimum !== null && entry.maximum !== null
        ? `${entry.minimum}–${entry.maximum}`
        : boundsParts.join(" · ");

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

  const currentDisplay = displayEntryValue(entry, entry.value ?? entry.default);

  return (
    <article
      ref={cardRef}
      data-testid={`setting-card-${entry.key}`}
      data-open={open ? "true" : undefined}
      className={cn(
        "@container rounded-lg border bg-zinc-950/60 text-zinc-100 transition-colors duration-150",
        open ? "border-cyan-500/30 bg-zinc-900/50" : "border-white/10 hover:border-white/20",
        highlight && "ring-2 ring-cyan-500/80 border-cyan-500/80 shadow-lg shadow-cyan-950/50",
        className
      )}
    >
      {/* Collapsed row: what it is on the left, what it is now on the right */}
      <div className="grid gap-x-6 gap-y-3 p-3 @[40rem]:grid-cols-[minmax(0,1fr)_13rem_5.5rem] @[40rem]:items-center @xl:p-4">
        <div className="min-w-0 space-y-1.5">
          <h4 className="text-sm font-semibold leading-snug tracking-tight text-zinc-100 [overflow-wrap:anywhere]">
            {entry.label}
          </h4>
          <div className="flex min-w-0 flex-wrap items-center gap-1.5">
            <span
              title={entry.key}
              className="min-w-0 max-w-full truncate rounded border border-white/10 bg-zinc-900/80 px-1.5 py-0.5 font-mono text-2xs text-zinc-300"
            >
              {entry.key}
            </span>
            <TierBadge tier={entry.tier} tighter={entry.tighter} />
            <ApplyModeBadge apply={entry.apply} />
            <span
              className="rounded border border-white/10 bg-zinc-900/60 px-1.5 py-0.5 text-2xs text-zinc-400"
              title="Architecture Decision Record"
            >
              ADR {entry.adr}
            </span>
            <span
              className="max-w-full truncate rounded border border-white/10 bg-zinc-900/60 px-1.5 py-0.5 font-mono text-2xs text-zinc-400"
              title={`Stored in ${entry.store}`}
            >
              {entry.store}
            </span>
          </div>
          {entry.note && (
            <div className="min-w-0">
              <p
                ref={noteRef}
                id={noteId}
                className={cn(
                  "text-xs leading-relaxed text-zinc-400",
                  showFullNote ? "[overflow-wrap:anywhere]" : "truncate"
                )}
              >
                {entry.note}
              </p>
              {!open && (noteClamped || noteExpanded) && (
                <button
                  type="button"
                  aria-expanded={noteExpanded}
                  aria-controls={noteId}
                  onClick={() => setNoteExpanded((v) => !v)}
                  className="mt-0.5 rounded text-2xs text-cyan-300 hover:text-cyan-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-500 cursor-pointer"
                >
                  {noteExpanded ? "Show less" : "Show more"}
                </button>
              )}
            </div>
          )}
        </div>

        {/* Current value: stacked under the title below 640px of row width, a fixed column above it */}
        <div className="flex min-w-0 items-center justify-between gap-3 @[40rem]:contents">
          <div className="min-w-0 @[40rem]:text-right">
            <div
              data-testid={`setting-value-${entry.key}`}
              className="font-mono text-base font-semibold leading-tight text-cyan-300 [overflow-wrap:anywhere]"
            >
              {currentDisplay}
            </div>
            <div className="mt-0.5 text-2xs text-zinc-400 [overflow-wrap:anywhere]">
              default {isChoice ? choiceLabel(entry.default) : entry.default}
              {rangeStr ? ` · ${rangeStr}` : ""}
            </div>
            {entry.effective !== null && (
              <div
                className={cn(
                  "mt-1 inline-flex max-w-full items-center gap-1 font-mono text-2xs",
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
                <span className="[overflow-wrap:anywhere]">
                  applied now {displayEntryValue(entry, entry.effective)}
                </span>
              </div>
            )}
          </div>

          <div className="shrink-0 @[40rem]:justify-self-end">
            {entry.writable ? (
              <Button
                ref={editBtnRef}
                type="button"
                variant="outline"
                size="sm"
                aria-label={`Edit ${entry.label}`}
                aria-expanded={open}
                aria-controls={editorId}
                onClick={toggleEditor}
                className={cn(
                  "gap-1.5 border-white/15 bg-zinc-900/60 text-xs text-zinc-200 hover:bg-zinc-800 focus-visible:ring-2 focus-visible:ring-cyan-500 cursor-pointer",
                  open && "border-cyan-500/50 text-cyan-200"
                )}
              >
                <Pencil className="size-3" aria-hidden="true" />
                Edit
              </Button>
            ) : (
              <span className="inline-flex items-center gap-1 text-2xs text-zinc-400" title="Fixed in code; not editable here">
                <Lock className="size-3" aria-hidden="true" />
                Fixed
              </span>
            )}
          </div>
        </div>
      </div>

      {/* Inline editor (writable settings only). Kept in the DOM so aria-controls always resolves. */}
      {entry.writable && (
        <div
          ref={editorRef}
          id={editorId}
          role="group"
          aria-label={`Edit ${entry.label}`}
          hidden={!open}
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              e.stopPropagation();
              closeEditor();
            }
          }}
        >
          {open && (
            <div className="space-y-4 border-t border-white/10 bg-zinc-900/40 p-3 motion-safe:animate-in motion-safe:fade-in motion-safe:slide-in-from-top-1 motion-safe:duration-150 @xl:p-4">
              <div className="grid gap-5 @3xl:grid-cols-2 @3xl:gap-8">
                {/* Left: the new value */}
                <div className="min-w-0 space-y-3">
                  {isChoice && (
                    <div className="space-y-2">
                      <span id={`${inputId}-choices`} className="text-xs font-medium text-zinc-300">
                        New value
                      </span>
                      <div role="radiogroup" aria-labelledby={`${inputId}-choices`} className="flex flex-wrap gap-2">
                        {choices.map((choice) => (
                          <button
                            key={choice.value}
                            type="button"
                            role="radio"
                            aria-checked={amount === choice.value}
                            onClick={() => setAmount(choice.value)}
                            className={cn(
                              "rounded border px-3 py-1.5 font-mono text-xs min-h-[32px] inline-flex items-center transition-colors cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-500",
                              amount === choice.value
                                ? "border-cyan-500/60 bg-cyan-950/60 text-cyan-200"
                                : "border-white/10 bg-zinc-950/60 text-zinc-400 hover:border-white/20 hover:text-zinc-200"
                            )}
                          >
                            {choice.label}
                          </button>
                        ))}
                      </div>
                    </div>
                  )}

                  <div className={cn("space-y-3", isChoice && "hidden")}>
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <label htmlFor={inputId} className="flex items-center gap-1.5 text-xs font-medium text-zinc-300">
                        <Sliders className="size-3.5 text-cyan-400" aria-hidden="true" />
                        <span>New value</span>
                        {boundsStr && <span className="font-mono text-2xs text-zinc-400">({boundsStr})</span>}
                      </label>

                      {entry.key.endsWith("_s") && hasValidFormat && (
                        <span className="flex items-center gap-1 font-mono text-xs text-cyan-300">
                          <Clock className="size-3 text-cyan-400" aria-hidden="true" />
                          <span>{formatDuration(amount)}</span>
                        </span>
                      )}
                    </div>

                    <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
                      {entry.minimum !== null && entry.maximum !== null && (
                        <div className="flex min-w-[12rem] flex-1 items-center gap-2">
                          <span className="font-mono text-2xs text-zinc-400">{entry.minimum}</span>
                          <input
                            id={sliderId}
                            type="range"
                            aria-label={`${entry.label} range slider`}
                            min={entry.minimum}
                            max={entry.maximum}
                            step={stepFor(entry.decimals)}
                            value={hasValidFormat ? amount : current}
                            onChange={(e) => setAmount(e.target.value)}
                            className="h-1.5 w-full min-w-0 cursor-pointer rounded bg-zinc-950 accent-cyan-400 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-500"
                          />
                          <span className="font-mono text-2xs text-zinc-400">{entry.maximum}</span>
                        </div>
                      )}
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
                        className="w-36 rounded border border-white/10 bg-zinc-950 px-2.5 py-1.5 font-mono text-sm text-zinc-100 placeholder-zinc-600 focus:border-cyan-500 focus:outline-none focus:ring-1 focus:ring-cyan-500"
                      />
                    </div>

                    {presets.length > 0 && (
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className="mr-1 flex items-center gap-1 text-2xs text-zinc-400">
                          <Sparkles className="size-3 text-zinc-400" aria-hidden="true" />
                          <span>Presets:</span>
                        </span>
                        {presets.slice(0, 6).map((preset) => (
                          <button
                            key={preset.value}
                            type="button"
                            onClick={() => setAmount(preset.value)}
                            className={cn(
                              "rounded border px-2 py-0.5 font-mono text-2xs min-h-[24px] inline-flex items-center transition-colors cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-500",
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
                </div>

                {/* Right: why, and the second factor when loosening */}
                <div className="min-w-0 space-y-3">
                  <div className="space-y-1.5">
                    <div className="flex flex-wrap items-center justify-between gap-1">
                      <label htmlFor={reasonId} className="text-xs text-zinc-300">
                        Reason (audited, at least {MIN_REASON_CHARS} characters)
                      </label>
                      <span className="font-mono text-2xs text-zinc-400">
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
                    <div className="flex flex-wrap gap-1.5 pt-0.5">
                      {QUICK_REASONS.map((qr) => (
                        <button
                          key={qr}
                          type="button"
                          onClick={() => setReason(qr)}
                          className="inline-flex min-h-[24px] cursor-pointer items-center rounded border border-white/10 bg-zinc-950/70 px-2 py-0.5 text-2xs text-zinc-400 transition-colors hover:border-cyan-500/40 hover:text-cyan-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-500"
                        >
                          + {qr}
                        </button>
                      ))}
                    </div>
                  </div>

                  {needsTotp && (
                    <div className="space-y-1.5 rounded-lg border border-amber-500/30 bg-amber-950/20 p-3">
                      <label htmlFor={totpId} className="flex items-center gap-1.5 text-xs font-medium text-amber-200">
                        <Lock className="size-3.5 shrink-0 text-amber-400" aria-hidden="true" />
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
                </div>
              </div>

              {crossFieldWarning && (
                <div
                  role="alert"
                  className="flex items-start gap-2 rounded-lg border border-red-500/50 bg-red-950/40 p-2.5 text-xs text-red-200 shadow-sm"
                >
                  <AlertTriangle className="mt-0.5 size-4 shrink-0 text-red-400" aria-hidden="true" />
                  <span>{crossFieldWarning}</span>
                </div>
              )}

              {!isChoice && amount.trim() !== "" && !inBounds && (
                <p id={errorId} role="alert" className="text-xs text-red-300">
                  Enter a valid number{boundsStr ? ` (${boundsStr})` : ""} with at most {entry.decimals} decimal places.
                </p>
              )}

              {error && (
                <div role="alert" className="rounded border border-red-500/40 bg-red-950/50 p-2 text-xs text-red-200">
                  {error}
                </div>
              )}

              <div className="flex flex-wrap items-center justify-between gap-3 border-t border-white/5 pt-3">
                <span className="min-w-0 text-2xs text-zinc-400">
                  {isLoosening ? (
                    <span className="font-medium text-amber-300">Loosening change detected</span>
                  ) : (
                    <>
                      {entry.apply === "hot"
                        ? "Applies within seconds."
                        : entry.apply === "restart"
                          ? "Applies on the next service restart."
                          : "Applies on the next deploy."}{" "}
                      Esc closes without saving.
                    </>
                  )}
                </span>
                <div className="flex items-center gap-2">
                  <Button
                    type="button"
                    variant="ghost"
                    onClick={handleCancel}
                    className="cursor-pointer text-xs text-zinc-300 hover:bg-zinc-800 hover:text-zinc-100"
                  >
                    Cancel
                  </Button>
                  <Button
                    onClick={() => void handleSave()}
                    disabled={!canSave}
                    title={needsTotp && staleBuild ? STALE_BUILD_TITLE : undefined}
                    className={cn(
                      "cursor-pointer px-4 py-2 text-xs font-bold uppercase tracking-wider transition-all",
                      needsTotp
                        ? "bg-amber-500 text-zinc-950 hover:bg-amber-400 disabled:opacity-40"
                        : "bg-cyan-500 text-zinc-950 hover:bg-cyan-400 disabled:opacity-40"
                    )}
                  >
                    {saving ? "Saving…" : needsTotp ? "Save with TOTP" : "Save"}
                  </Button>
                </div>
              </div>
            </div>
          )}
        </div>
      )}
    </article>
  );
};

export default SettingCard;
