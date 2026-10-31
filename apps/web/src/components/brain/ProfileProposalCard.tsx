"use client";

import { useCallback, useEffect, useState } from "react";
import { btnPrimary, btnSecondary, TotpDialog } from "@/components/autotrade/Dialogs";
import { createProfile, getEstimate, setProfileMode, toApiError } from "@/lib/autotrade/client";
import { errorCopy, fmtUsd, MARKET_LABEL, MODE_LABEL } from "@/lib/autotrade/copy";
import { createBody, draftFromProposal } from "@/lib/autotrade/presets";
import type { ApiError, CreatedProfile } from "@/lib/autotrade/types";
import type { ProfileProposalWidget } from "@/lib/brain/types";
import { dispatchSwitchWorkspace } from "@/lib/navigation";
import { useAutoTradeStore } from "@/lib/stores/useAutoTradeStore";

type SwitchState = "ON" | "OFF" | "UNKNOWN";
type CodeStep = "create" | "raise" | null;

/** The operator reviews or creates a tool-produced profile through the existing Auto-Trade API. */
export function ProfileProposalCard({ widget }: { widget: ProfileProposalWidget }) {
  const p = widget.proposal;
  const [switchState, setSwitchState] = useState<SwitchState>("UNKNOWN");
  const [codeStep, setCodeStep] = useState<CodeStep>(null);
  const [pending, setPending] = useState(false);
  const [totpError, setTotpError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [created, setCreated] = useState<CreatedProfile | null>(null);
  const [raised, setRaised] = useState(false);

  const refreshSwitch = useCallback(async (): Promise<SwitchState> => {
    try {
      const estimate = await getEstimate(p.coins, p.style, p.direction);
      setSwitchState(estimate.master_switch);
      return estimate.master_switch;
    } catch {
      setSwitchState("UNKNOWN");
      return "UNKNOWN";
    }
  }, [p.coins, p.style, p.direction]);

  useEffect(() => {
    if (p.mode !== "ASK") void refreshSwitch();
  }, [p.mode, refreshSwitch]);

  const review = () => {
    useAutoTradeStore.getState().setWizardDraft(draftFromProposal(p));
    dispatchSwitchWorkspace({ mode: "autotrade", view: "new" });
  };

  const openProfile = () => {
    if (created) dispatchSwitchWorkspace({ mode: "autotrade", id: created.id });
  };

  const submitCreate = async (code: string) => {
    setPending(true);
    setTotpError(null);
    setActionError(null);
    try {
      // C6: read the current switch before creation; creation itself remains available when it is off.
      if (p.mode !== "ASK") await refreshSwitch();
      const result = await createProfile(createBody(draftFromProposal(p)), code);
      setCreated(result);
      if (result.started && p.mode !== "ASK") {
        const currentSwitch = await refreshSwitch();
        setCodeStep(currentSwitch === "ON" ? "raise" : null);
      } else {
        setCodeStep(null);
      }
    } catch (caught) {
      const err = toApiError(caught);
      if (err.code === "TOTP_LOCKED" || err.code === "TOTP_REQUIRED" || err.code === "TOTP_REUSED") {
        setTotpError(errorCopy(err));
      } else {
        setCodeStep(null);
        setActionError(`Couldn't create ${p.name}. ${errorCopy(err)}`);
      }
    } finally {
      setPending(false);
    }
  };

  const submitRaise = async (code: string) => {
    if (!created) return;
    setPending(true);
    setTotpError(null);
    try {
      const currentSwitch = await refreshSwitch();
      if (currentSwitch !== "ON") {
        setCodeStep(null);
        return;
      }
      await setProfileMode(created.id, p.mode, code);
      setRaised(true);
      setCodeStep(null);
    } catch (caught) {
      const err: ApiError = toApiError(caught);
      if (err.code === "TOTP_LOCKED" || err.code === "TOTP_REQUIRED" || err.code === "TOTP_REUSED") {
        setTotpError(errorCopy(err));
      } else {
        setCodeStep(null);
        setActionError(`Created ${p.name}. It remains in Ask me: ${errorCopy(err)}`);
      }
    } finally {
      setPending(false);
    }
  };

  const statusText = created
    ? !created.started
      ? `Created ${p.name}, but it couldn't start: ${errorCopy({ code: created.start_error?.code, message: created.start_error?.detail ?? "Start refused." })} Review the profile and try Start again.`
      : raised
        ? `${p.name} is now in ${MODE_LABEL[p.mode]}.`
        : p.mode !== "ASK" && switchState === "OFF"
          ? `Created ${p.name}. It remains in Ask me while the auto-trade switch is off.`
          : p.mode !== "ASK" && switchState === "UNKNOWN"
            ? `Created ${p.name}. It remains in Ask me. Check the auto-trade switch before raising the mode.`
          : p.mode !== "ASK" && codeStep === "raise"
            ? `Created ${p.name}. Started in Ask me. Enter a second TOTP code to switch to ${MODE_LABEL[p.mode]}.`
            : `Created ${p.name}. Started in Ask me. Dry run is ${p.dry_run ? "on" : "off"}.`
    : widget.status === "INVALID"
      ? "This proposal needs changes before it can be created."
      : "This is a proposal. No profile exists yet.";

  const detailRows = [
    ["Capital budget", fmtUsd(p.capital_budget_usd)],
    ["Risk per trade", `${p.max_risk_per_trade_pct}%`],
    ["Maximum leverage", `${p.max_leverage}×`],
    ["Daily loss limit", `${p.daily_loss_limit_pct}%${p.daily_loss_defaulted ? " (default: 3 × risk per trade, capped at maximum drawdown)" : ""}`],
    ["Maximum drawdown", `${p.max_drawdown_pct}%`],
    ["Position limit", `${p.max_position_pct}% · ${p.max_open_positions} open positions`],
    ["Exits", `Take profit ${p.exit_tp_r}R; ${p.exit_tp1_scale_pct}% at TP1${p.exit_trail_activate_pct ? `; trail at ${p.exit_trail_activate_pct}% by ${p.exit_trail_pct}%` : ""}${p.exit_max_hold_hours ? `; maximum ${p.exit_max_hold_hours} hours` : ""}`],
    ["Mode", MODE_LABEL[p.mode]],
    ["Dry run", p.dry_run ? "Orders are simulated." : "Off"],
  ] as const;

  return (
    <section aria-label={`Profile proposal for ${p.name}`} className="w-full max-w-[720px] min-w-0 rounded-xl border border-zinc-700 bg-zinc-900 p-4 text-zinc-100">
      <h3 className="text-sm font-semibold">Profile proposal</h3>
      <p className="mt-1 break-words text-base font-medium">{p.name}</p>
      <p className="mt-1 break-words text-xs text-zinc-300">
        {p.coins.join(" and ")} · {p.markets.map((market) => MARKET_LABEL[market]).join(", ")} · {p.style === "SWING" ? "Swing" : "Intraday"} · {p.direction === "BOTH" ? "Long and short" : p.direction === "LONG" ? "Long" : "Short"}
      </p>
      <dl className="mt-3 grid min-w-0 grid-cols-1 gap-x-5 gap-y-2 text-xs sm:grid-cols-2">
        {detailRows.map(([label, value]) => (
          <div key={label} className="min-w-0">
            <dt className="text-zinc-400">{label}</dt>
            <dd className="break-words text-zinc-100">{value}</dd>
          </div>
        ))}
      </dl>
      {widget.errors.length > 0 && (
        <ul aria-label="Proposal errors" className="mt-3 list-disc space-y-1 pl-5 text-xs text-red-300">
          {widget.errors.map((code, index) => <li key={`${code}-${index}`}>{errorCopy({ code, message: code })}</li>)}
        </ul>
      )}
      {widget.warnings.length > 0 && (
        <ul aria-label="Proposal warnings" className="mt-3 list-disc space-y-1 pl-5 text-xs text-amber-300">
          {widget.warnings.map((warning, index) => <li key={`${warning}-${index}`} className="break-words">{warning}</li>)}
        </ul>
      )}
      {!created && widget.status === "PROPOSED" && p.mode !== "ASK" && (
        <p className="mt-3 text-xs text-amber-300">
          {switchState === "OFF" ? "Auto-trade switch off. New entries require approval. This profile will stay in Ask me until the switch is on."
            : switchState === "UNKNOWN" ? "Auto-trade switch status unavailable. Check it before raising the mode."
              : `Creating starts this profile in Ask me. A second TOTP code is needed to switch to ${MODE_LABEL[p.mode]}.`}
        </p>
      )}
      <p role={widget.status === "INVALID" || (created !== null && !created.started) ? "alert" : "status"} className="mt-3 text-xs text-zinc-200">{statusText}</p>
      {actionError && <p role="alert" className="mt-2 text-xs text-red-300">{actionError}</p>}
      <div className="mt-4 flex flex-col gap-2 sm:flex-row">
        {!created ? (
          <>
            <button type="button" className={btnSecondary} onClick={review}>Review in wizard</button>
            {widget.status === "PROPOSED" && <button type="button" className={btnPrimary} onClick={() => setCodeStep("create")}>Create profile</button>}
          </>
        ) : (
          <>
            <button type="button" className={btnSecondary} onClick={openProfile}>Open profile</button>
            {created.started && !raised && p.mode !== "ASK" && switchState === "ON" && codeStep === null && (
              <button type="button" className={btnPrimary} onClick={() => setCodeStep("raise")}>Set {MODE_LABEL[p.mode]}</button>
            )}
            {created.started && !raised && p.mode !== "ASK" && switchState === "UNKNOWN" && (
              <button type="button" className={btnSecondary} onClick={() => void refreshSwitch()}>Check switch</button>
            )}
          </>
        )}
      </div>
      <TotpDialog
        key={codeStep ?? "closed"}
        open={codeStep !== null}
        change={codeStep === "raise" ? `Raising ${p.name} to ${MODE_LABEL[p.mode]}` : `Creating ${p.name}`}
        confirmLabel={pending ? codeStep === "raise" ? "Setting mode…" : "Creating profile…" : codeStep === "raise" ? `Set ${MODE_LABEL[p.mode]}` : "Create profile"}
        pending={pending}
        error={totpError}
        onSubmit={(code) => { if (codeStep === "raise") void submitRaise(code); else void submitCreate(code); }}
        onClose={() => setCodeStep(null)}
      />
    </section>
  );
}
