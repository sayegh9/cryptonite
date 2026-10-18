import React from "react";
import { CheckCircle2, AlertTriangle, ShieldAlert, Zap } from "lucide-react";
import { cn } from "@/lib/utils";

export interface ExitValuesSet {
  stop_loss_pct: number;
  debate_drawdown_pct: number;
  trail_activate_pct: number;
  trail_pct: number;
  funding_spike?: number;
}

export interface ExitConstraintGaugeProps {
  values: ExitValuesSet;
  appliedValues?: Partial<ExitValuesSet>;
  onQuickSelect?: (key: string) => void;
  className?: string;
}

interface RuleCheck {
  id: string;
  label: string;
  valid: boolean;
  message: string;
}

export const ExitConstraintGauge: React.FC<ExitConstraintGaugeProps> = ({
  values,
  appliedValues,
  onQuickSelect,
  className,
}) => {
  // ADR 0007 Cross-field rules validation
  const ruleDebateBeforeStop: RuleCheck = {
    id: "debate_before_stop",
    label: "Exit debate must fire before hard stop loss",
    valid: values.debate_drawdown_pct < values.stop_loss_pct,
    message:
      values.debate_drawdown_pct < values.stop_loss_pct
        ? `Debate fires at -${values.debate_drawdown_pct}%, before hard stop at -${values.stop_loss_pct}%.`
        : `Violation: debate_drawdown_pct (${values.debate_drawdown_pct}%) must be strictly less than stop_loss_pct (${values.stop_loss_pct}%).`,
  };

  const ruleTrailWithinActivation: RuleCheck = {
    id: "trail_within_activation",
    label: "Trailing give-back cannot exceed activation gain",
    valid: values.trail_pct <= values.trail_activate_pct,
    message:
      values.trail_pct <= values.trail_activate_pct
        ? `Trail give-back is ${values.trail_pct}%, safely within +${values.trail_activate_pct}% activation.`
        : `Violation: trail_pct (${values.trail_pct}%) exceeds trail_activate_pct (${values.trail_activate_pct}%). Stop triggers below entry.`,
  };

  const rules: RuleCheck[] = [ruleDebateBeforeStop, ruleTrailWithinActivation];
  const allValid = rules.every((r) => r.valid);
  const violations = rules.filter((r) => !r.valid);

  // Timeline coordinate math: range [-20, +20], span 40
  const MIN_SCALE = -20;
  const MAX_SCALE = 20;
  const SPAN = MAX_SCALE - MIN_SCALE; // 40

  const toPct = (val: number): number => {
    const clamped = Math.max(MIN_SCALE, Math.min(MAX_SCALE, val));
    return ((clamped - MIN_SCALE) / SPAN) * 100;
  };

  // Positions on scale
  const posHardStop = toPct(-values.stop_loss_pct);
  const posDebate = toPct(-values.debate_drawdown_pct);
  const posEntry = toPct(0); // 50%
  const posTrailActivate = toPct(values.trail_activate_pct);
  const trailFloorValue = values.trail_activate_pct - values.trail_pct;
  const posTrailFloor = toPct(trailFloorValue);

  // Applied positions if provided
  const appliedHardStop =
    appliedValues?.stop_loss_pct !== undefined
      ? toPct(-appliedValues.stop_loss_pct)
      : undefined;
  const appliedDebate =
    appliedValues?.debate_drawdown_pct !== undefined
      ? toPct(-appliedValues.debate_drawdown_pct)
      : undefined;
  const appliedTrailActivate =
    appliedValues?.trail_activate_pct !== undefined
      ? toPct(appliedValues.trail_activate_pct)
      : undefined;

  // Scale ticks
  const ticks = [-20, -15, -10, -5, 0, 5, 10, 15, 20];

  return (
    <div
      role="region"
      aria-label="Exit constraint timeline and rule validator"
      className={cn(
        "rounded-xl border border-white/10 bg-zinc-950/70 p-4 sm:p-5 text-zinc-100 shadow-md backdrop-blur-sm",
        className
      )}
    >
      {/* Header with Title and Overall Status Badge */}
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/10 pb-4">
        <div>
          <h3 className="text-sm font-semibold tracking-wide text-zinc-100 flex items-center gap-2">
            <span>Exit Constraint Lifecycle</span>
            <span className="text-xs font-mono font-normal text-zinc-400">
              (-20% Drawdown → +20% Profit)
            </span>
          </h3>
          <p className="mt-0.5 text-xs text-zinc-400">
            ADR 0007 real-time rule engine &amp; position exit boundary preview
          </p>
        </div>

        {allValid ? (
          <div
            role="status"
            className="inline-flex items-center gap-1.5 rounded-full border border-emerald-500/40 bg-emerald-950/60 px-3 py-1 text-xs font-semibold text-emerald-200 shadow-sm"
          >
            <CheckCircle2 className="size-4 text-emerald-400 shrink-0" aria-hidden="true" />
            <span>All exit rules valid</span>
          </div>
        ) : (
          <div
            role="alert"
            className="inline-flex items-center gap-1.5 rounded-full border border-red-500/50 bg-red-950/70 px-3 py-1 text-xs font-bold text-red-200 shadow-sm animate-pulse"
          >
            <AlertTriangle className="size-4 text-red-400 shrink-0" aria-hidden="true" />
            <span>
              {violations.length} {violations.length === 1 ? "Rule Violation" : "Rule Violations"}
            </span>
          </div>
        )}
      </div>

      {/* Alert Box for Violations */}
      {!allValid && (
        <div
          role="alert"
          aria-live="assertive"
          className="mt-4 rounded-lg border border-red-500/40 bg-red-950/50 p-3.5 text-xs text-red-200 shadow-inner"
        >
          <div className="flex items-start gap-2.5">
            <ShieldAlert className="size-5 shrink-0 text-red-400 mt-0.5" aria-hidden="true" />
            <div className="space-y-1.5 flex-1">
              <div className="font-semibold text-red-100">
                Invalid Exit Constraint Configuration (ADR 0007)
              </div>
              <ul className="list-disc pl-4 space-y-1 text-red-200/90 font-mono">
                {violations.map((v) => (
                  <li key={v.id}>{v.message}</li>
                ))}
              </ul>
              <div className="text-[11px] text-red-300/80 pt-1">
                Please adjust the thresholds below to ensure safe trade exits before saving.
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Visual Timeline Track */}
      <div className="mt-7 mb-6 px-3">
        {/* Track container */}
        <div className="relative h-12 w-full flex items-center">
          {/* Background track gradient: Loss zone (red/rose) | Profit zone (emerald/cyan) */}
          <div className="absolute inset-x-0 h-3 rounded-full bg-zinc-900 border border-white/10 overflow-hidden flex">
            {/* Loss zone */}
            <div className="w-1/2 h-full bg-gradient-to-r from-red-950/50 via-rose-950/30 to-zinc-900 border-r border-dashed border-zinc-700/80" />
            {/* Profit zone */}
            <div className="w-1/2 h-full bg-gradient-to-r from-zinc-900 via-emerald-950/30 to-teal-950/50" />
          </div>

          {/* Shaded Trailing Stop Give-Back Window (between activation and floor) */}
          <div
            className={cn(
              "absolute h-3 rounded-sm transition-all duration-300 opacity-60 border-y",
              values.trail_pct <= values.trail_activate_pct
                ? "bg-teal-500/25 border-teal-400/50"
                : "bg-red-500/30 border-red-400/60"
            )}
            style={{
              left: `${Math.min(posTrailFloor, posTrailActivate)}%`,
              width: `${Math.abs(posTrailActivate - posTrailFloor)}%`,
            }}
            title={`Trailing Stop Window: ${values.trail_pct}% give-back distance`}
          />

          {/* Applied ghost markers if applied differs from current */}
          {appliedHardStop !== undefined && Math.abs(appliedHardStop - posHardStop) > 0.5 && (
            <div
              className="absolute top-1 bottom-1 w-0.5 border-l-2 border-dashed border-rose-500/40 z-10 pointer-events-none"
              style={{ left: `${appliedHardStop}%` }}
              title={`Applied Hard Stop: -${appliedValues?.stop_loss_pct}%`}
            />
          )}
          {appliedDebate !== undefined && Math.abs(appliedDebate - posDebate) > 0.5 && (
            <div
              className="absolute top-1 bottom-1 w-0.5 border-l-2 border-dashed border-amber-500/40 z-10 pointer-events-none"
              style={{ left: `${appliedDebate}%` }}
              title={`Applied Debate Trigger: -${appliedValues?.debate_drawdown_pct}%`}
            />
          )}
          {appliedTrailActivate !== undefined && Math.abs(appliedTrailActivate - posTrailActivate) > 0.5 && (
            <div
              className="absolute top-1 bottom-1 w-0.5 border-l-2 border-dashed border-emerald-500/40 z-10 pointer-events-none"
              style={{ left: `${appliedTrailActivate}%` }}
              title={`Applied Trail Activation: +${appliedValues?.trail_activate_pct}%`}
            />
          )}

          {/* Marker 1: Hard Stop Loss */}
          <button
            type="button"
            onClick={() => onQuickSelect?.("exit.stop_loss_pct")}
            className="group absolute -translate-x-1/2 flex flex-col items-center z-20 focus:outline-none focus-visible:ring-2 focus-visible:ring-rose-400 rounded cursor-pointer"
            style={{ left: `${posHardStop}%` }}
            aria-label={`Hard Stop Loss: -${values.stop_loss_pct}%`}
          >
            <span className="text-[10px] font-mono font-bold text-rose-300 group-hover:text-rose-200 transition-colors bg-zinc-950/90 px-1 py-0.5 rounded border border-rose-500/40 -mt-6 whitespace-nowrap shadow-sm">
              Stop -{values.stop_loss_pct}%
            </span>
            <div className="size-4 rounded-full bg-rose-500 border-2 border-zinc-950 shadow-md group-hover:scale-125 transition-transform" />
          </button>

          {/* Marker 2: AI Exit Debate Trigger */}
          <button
            type="button"
            onClick={() => onQuickSelect?.("exit.debate_drawdown_pct")}
            className="group absolute -translate-x-1/2 flex flex-col items-center z-20 focus:outline-none focus-visible:ring-2 focus-visible:ring-amber-400 rounded cursor-pointer"
            style={{ left: `${posDebate}%` }}
            aria-label={`AI Exit Debate Trigger: -${values.debate_drawdown_pct}%`}
          >
            <span className="text-[10px] font-mono font-bold text-amber-300 group-hover:text-amber-200 transition-colors bg-zinc-950/90 px-1 py-0.5 rounded border border-amber-500/40 -mt-6 whitespace-nowrap shadow-sm">
              Debate -{values.debate_drawdown_pct}%
            </span>
            <div className="size-4 rounded-full bg-amber-400 border-2 border-zinc-950 shadow-md group-hover:scale-125 transition-transform" />
          </button>

          {/* Marker 3: Entry Position (0%) */}
          <div
            className="absolute -translate-x-1/2 flex flex-col items-center z-10 pointer-events-none"
            style={{ left: `${posEntry}%` }}
            aria-label="Entry position 0%"
          >
            <span className="text-[10px] font-mono font-semibold text-zinc-400 bg-zinc-950/90 px-1 rounded border border-zinc-700/60 -mt-5 whitespace-nowrap">
              Entry 0%
            </span>
            <div className="w-1 h-5 bg-zinc-300 rounded-full shadow" />
          </div>

          {/* Marker 4: Trailing Stop Floor at Activation */}
          <button
            type="button"
            onClick={() => onQuickSelect?.("exit.trail_pct")}
            className="group absolute -translate-x-1/2 flex flex-col items-center z-20 focus:outline-none focus-visible:ring-2 focus-visible:ring-teal-400 rounded cursor-pointer"
            style={{ left: `${posTrailFloor}%` }}
            aria-label={`Trailing Stop Floor: ${trailFloorValue > 0 ? `+${trailFloorValue.toFixed(2)}` : trailFloorValue.toFixed(2)}%`}
          >
            <div className="size-3.5 rounded-full bg-teal-400 border-2 border-zinc-950 shadow-md group-hover:scale-125 transition-transform" />
            <span className="text-[10px] font-mono font-medium text-teal-300 group-hover:text-teal-200 transition-colors bg-zinc-950/90 px-1 py-0.5 rounded border border-teal-500/40 mt-1 whitespace-nowrap shadow-sm">
              Floor {trailFloorValue > 0 ? `+${trailFloorValue.toFixed(1)}` : trailFloorValue.toFixed(1)}%
            </span>
          </button>

          {/* Marker 5: Trail Activation (+trail_activate_pct%) */}
          <button
            type="button"
            onClick={() => onQuickSelect?.("exit.trail_activate_pct")}
            className="group absolute -translate-x-1/2 flex flex-col items-center z-20 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-400 rounded cursor-pointer"
            style={{ left: `${posTrailActivate}%` }}
            aria-label={`Trail Activation: +${values.trail_activate_pct}%`}
          >
            <span className="text-[10px] font-mono font-bold text-emerald-300 group-hover:text-emerald-200 transition-colors bg-zinc-950/90 px-1 py-0.5 rounded border border-emerald-500/40 -mt-6 whitespace-nowrap shadow-sm">
              Trail +{values.trail_activate_pct}%
            </span>
            <div className="size-4 rounded-full bg-emerald-400 border-2 border-zinc-950 shadow-md group-hover:scale-125 transition-transform" />
          </button>
        </div>

        {/* Ruler Axis Ticks */}
        <div className="relative mt-2 h-4 w-full text-[10px] font-mono text-zinc-400">
          {ticks.map((tick) => {
            const pct = toPct(tick);
            return (
              <div
                key={tick}
                className="absolute -translate-x-1/2 flex flex-col items-center"
                style={{ left: `${pct}%` }}
              >
                <div className="w-px h-1.5 bg-zinc-700" />
                <span className={cn(tick === 0 && "text-zinc-200 font-bold")}>
                  {tick > 0 ? `+${tick}%` : `${tick}%`}
                </span>
              </div>
            );
          })}
        </div>
      </div>

      {/* Parameter Cards Grid / Quick Select Chips */}
      <div className="mt-4 grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-5 gap-2.5 pt-3 border-t border-white/5">
        {/* Hard Stop Card */}
        <button
          type="button"
          onClick={() => onQuickSelect?.("exit.stop_loss_pct")}
          aria-label={`Hard Stop Loss: ${values.stop_loss_pct}%, parameter exit.stop_loss_pct`}
          className="flex flex-col rounded-lg border border-rose-500/30 bg-rose-950/20 p-2.5 text-left transition-all hover:bg-rose-950/40 hover:border-rose-500/60 focus:outline-none focus-visible:ring-2 focus-visible:ring-rose-400 cursor-pointer"
        >
          <div className="flex items-center justify-between">
            <span className="text-[11px] font-medium text-rose-300">Hard Stop Loss</span>
            <span className="size-2 rounded-full bg-rose-500" aria-hidden="true" />
          </div>
          <div className="mt-1 font-mono text-base font-bold text-rose-100">
            {values.stop_loss_pct}%
          </div>
          <div className="text-[10px] text-zinc-400 font-mono truncate">
            exit.stop_loss_pct
          </div>
        </button>

        {/* Debate Trigger Card */}
        <button
          type="button"
          onClick={() => onQuickSelect?.("exit.debate_drawdown_pct")}
          aria-label={`AI Debate Trigger: ${values.debate_drawdown_pct}%, parameter exit.debate_drawdown_pct`}
          className={cn(
            "flex flex-col rounded-lg border p-2.5 text-left transition-all focus:outline-none focus-visible:ring-2 cursor-pointer",
            ruleDebateBeforeStop.valid
              ? "border-amber-500/30 bg-amber-950/20 hover:bg-amber-950/40 hover:border-amber-500/60 focus-visible:ring-amber-400"
              : "border-red-500 bg-red-950/40 animate-pulse focus-visible:ring-red-400"
          )}
        >
          <div className="flex items-center justify-between">
            <span className="text-[11px] font-medium text-amber-300">Debate Trigger</span>
            <span
              className={cn(
                "size-2 rounded-full",
                ruleDebateBeforeStop.valid ? "bg-amber-400" : "bg-red-500"
              )}
              aria-hidden="true"
            />
          </div>
          <div className="mt-1 font-mono text-base font-bold text-amber-100">
            {values.debate_drawdown_pct}%
          </div>
          <div className="text-[10px] text-zinc-400 font-mono truncate">
            exit.debate_drawdown_pct
          </div>
        </button>

        {/* Trail Activate Card */}
        <button
          type="button"
          onClick={() => onQuickSelect?.("exit.trail_activate_pct")}
          aria-label={`Trail Activate: +${values.trail_activate_pct}%, parameter exit.trail_activate_pct`}
          className="flex flex-col rounded-lg border border-emerald-500/30 bg-emerald-950/20 p-2.5 text-left transition-all hover:bg-emerald-950/40 hover:border-emerald-500/60 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-400 cursor-pointer"
        >
          <div className="flex items-center justify-between">
            <span className="text-[11px] font-medium text-emerald-300">Trail Activate</span>
            <span className="size-2 rounded-full bg-emerald-400" aria-hidden="true" />
          </div>
          <div className="mt-1 font-mono text-base font-bold text-emerald-100">
            +{values.trail_activate_pct}%
          </div>
          <div className="text-[10px] text-zinc-400 font-mono truncate">
            exit.trail_activate_pct
          </div>
        </button>

        {/* Trail Give-Back Card */}
        <button
          type="button"
          onClick={() => onQuickSelect?.("exit.trail_pct")}
          aria-label={`Trail Give-Back: ${values.trail_pct}%, parameter exit.trail_pct`}
          className={cn(
            "flex flex-col rounded-lg border p-2.5 text-left transition-all focus:outline-none focus-visible:ring-2 cursor-pointer",
            ruleTrailWithinActivation.valid
              ? "border-teal-500/30 bg-teal-950/20 hover:bg-teal-950/40 hover:border-teal-500/60 focus-visible:ring-teal-400"
              : "border-red-500 bg-red-950/40 animate-pulse focus-visible:ring-red-400"
          )}
        >
          <div className="flex items-center justify-between">
            <span className="text-[11px] font-medium text-teal-300">Trail Give-Back</span>
            <span
              className={cn(
                "size-2 rounded-full",
                ruleTrailWithinActivation.valid ? "bg-teal-400" : "bg-red-500"
              )}
              aria-hidden="true"
            />
          </div>
          <div className="mt-1 font-mono text-base font-bold text-teal-100">
            {values.trail_pct}%
          </div>
          <div className="text-[10px] text-zinc-400 font-mono truncate">
            exit.trail_pct
          </div>
        </button>

        {/* Funding Spike Card (Optional) */}
        {values.funding_spike !== undefined && (
          <button
            type="button"
            onClick={() => onQuickSelect?.("exit.funding_spike")}
            aria-label={`Funding Spike: ${values.funding_spike}, parameter exit.funding_spike`}
            className="flex flex-col rounded-lg border border-purple-500/30 bg-purple-950/20 p-2.5 text-left transition-all hover:bg-purple-950/40 hover:border-purple-500/60 focus:outline-none focus-visible:ring-2 focus-visible:ring-purple-400 cursor-pointer col-span-2 sm:col-span-4 lg:col-span-1"
          >
            <div className="flex items-center justify-between">
              <span className="text-[11px] font-medium text-purple-300">Funding Spike</span>
              <span className="size-2 rounded-full bg-purple-400" aria-hidden="true" />
            </div>
            <div className="mt-1 font-mono text-base font-bold text-purple-100">
              {values.funding_spike}
            </div>
            <div className="text-[10px] text-zinc-400 font-mono truncate">
              exit.funding_spike
            </div>
          </button>
        )}
      </div>

      {/* Applied Values Footnote if Divergent */}
      {appliedValues &&
        (appliedValues.stop_loss_pct !== undefined ||
          appliedValues.debate_drawdown_pct !== undefined ||
          appliedValues.trail_activate_pct !== undefined ||
          appliedValues.trail_pct !== undefined) && (
          <div className="mt-3 flex items-center gap-2 text-[11px] text-zinc-400 bg-zinc-900/40 rounded px-2.5 py-1.5 border border-white/5">
            <Zap className="size-3.5 text-sky-400 shrink-0" aria-hidden="true" />
            <span>
              Applied by exit monitor: Stop{" "}
              <strong className="text-zinc-200">
                -{appliedValues.stop_loss_pct ?? values.stop_loss_pct}%
              </strong>
              , Debate{" "}
              <strong className="text-zinc-200">
                -{appliedValues.debate_drawdown_pct ?? values.debate_drawdown_pct}%
              </strong>
              , Trail{" "}
              <strong className="text-zinc-200">
                +{appliedValues.trail_activate_pct ?? values.trail_activate_pct}%
              </strong>{" "}
              (Give-back{" "}
              <strong className="text-zinc-200">
                {appliedValues.trail_pct ?? values.trail_pct}%
              </strong>
              )
            </span>
          </div>
        )}
    </div>
  );
};

export default ExitConstraintGauge;
