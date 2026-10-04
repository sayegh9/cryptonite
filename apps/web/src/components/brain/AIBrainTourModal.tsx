"use client";

import React, { useState } from "react";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { BrainCircuit, Search, ShieldCheck, Zap, ArrowRight, ArrowLeft, CheckCircle2 } from "lucide-react";

interface AIBrainTourModalProps {
  isOpen: boolean;
  onClose: () => void;
  /** Queue a real debate on `symbol` (the same trigger call as the toolbar button). */
  onRunDebate?: () => void;
  symbol?: string;
}

export function AIBrainTourModal({ isOpen, onClose, onRunDebate, symbol }: AIBrainTourModalProps) {
  const [step, setStep] = useState<number>(1);
  const totalSteps = 3;

  const handleNext = () => {
    if (step < totalSteps) {
      setStep(step + 1);
    } else {
      handleComplete();
    }
  };

  const handleBack = () => {
    if (step > 1) {
      setStep(step - 1);
    }
  };

  const handleComplete = () => {
    try {
      localStorage.setItem("cryptonite:ai-brain-tour-seen", "true");
    } catch {}
    onClose();
  };

  const handleRunDebate = () => {
    handleComplete();
    if (onRunDebate) {
      onRunDebate();
    }
  };

  return (
    <Dialog open={isOpen} onOpenChange={(open) => !open && handleComplete()}>
      <DialogContent className="w-[calc(100vw-2rem)] sm:max-w-xl md:max-w-2xl max-h-[90vh] overflow-y-auto bg-zinc-950 border border-zinc-800 text-zinc-100 p-0 shadow-2xl rounded-xl">
        {/* Top Accent Header */}
        <div className="h-1.5 w-full bg-gradient-to-r from-cyan-500 via-purple-500 to-emerald-500 flex-none" />

        <div className="p-4 sm:p-6 space-y-4">
          <DialogHeader className="space-y-1">
            <div className="flex items-center justify-between text-2xs font-mono text-zinc-500">
              <span className="uppercase tracking-widest text-cyan-400 font-bold">First-Time User Guide</span>
              <span>Step {step} of {totalSteps}</span>
            </div>
            <DialogTitle className="text-lg sm:text-xl font-bold tracking-tight text-white flex items-center space-x-2">
              <BrainCircuit className="w-5 h-5 text-cyan-400 flex-none" />
              <span>
                {step === 1 && "Welcome to the AI Brain Hub"}
                {step === 2 && "How the Multi-Agent Fleet Works"}
                {step === 3 && "You Hold the Keys (Action Inbox)"}
              </span>
            </DialogTitle>
          </DialogHeader>

          {/* Step 1: The Orchestrator Concept */}
          {step === 1 && (
            <div className="space-y-3.5 text-xs sm:text-sm text-zinc-300 leading-relaxed">
              <p>
                Cryptonite features a fleet of specialized quantitative AI agents. Instead of switching between complex menus, the <strong className="text-cyan-400">Lead Orchestrator</strong> acts as your single point of contact.
              </p>
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-2.5 my-3">
                <div className="p-3 bg-zinc-900/70 border border-zinc-800 rounded-lg text-center">
                  <BrainCircuit className="w-5 h-5 text-cyan-400 mx-auto mb-1.5" />
                  <div className="font-bold text-xs text-zinc-200">1. Single Prompt</div>
                  <div className="text-2xs text-zinc-400 mt-1 leading-normal">Ask questions or type commands in natural language.</div>
                </div>
                <div className="p-3 bg-zinc-900/70 border border-zinc-800 rounded-lg text-center">
                  <Search className="w-5 h-5 text-purple-400 mx-auto mb-1.5" />
                  <div className="font-bold text-xs text-zinc-200">2. Auto-Dispatch</div>
                  <div className="text-2xs text-zinc-400 mt-1 leading-normal">Specialists gather market data, debate alpha & check risk.</div>
                </div>
                <div className="p-3 bg-zinc-900/70 border border-zinc-800 rounded-lg text-center">
                  <ShieldCheck className="w-5 h-5 text-emerald-400 mx-auto mb-1.5" />
                  <div className="font-bold text-xs text-zinc-200">3. Actionable Brief</div>
                  <div className="text-2xs text-zinc-400 mt-1 leading-normal">Receive concise 3-bullet cards with 1-click approvals.</div>
                </div>
              </div>
              <div className="p-3 bg-cyan-950/40 border border-cyan-800/50 rounded-lg text-xs text-cyan-300 flex items-start space-x-2">
                <span className="font-bold flex-none">Safe by Default:</span>
                <span>All actions in this tab run in Simulation Mode. Real funds are never traded without explicit operator authentication.</span>
              </div>
            </div>
          )}

          {/* Step 2: Multi-Agent Fleet & Adversarial Debate */}
          {step === 2 && (
            <div className="space-y-3.5 text-xs sm:text-sm text-zinc-300 leading-relaxed">
              <p>
                When a high-probability market setup is found, our agents conduct an <strong className="text-purple-400">Adversarial Consensus Debate</strong> to stress-test the idea before it ever reaches your inbox.
              </p>
              <div className="space-y-2 font-mono text-xs my-2">
                <div className="p-2.5 bg-zinc-900 border border-zinc-800 rounded-lg flex items-center justify-between gap-2">
                  <div className="flex items-center space-x-2 flex-none">
                    <span className="w-2 h-2 rounded-full bg-cyan-400 flex-none" />
                    <span className="text-zinc-200 font-bold">Market Scout</span>
                  </div>
                  <span className="text-2xs text-zinc-400 truncate text-right">Pulls orderbook depth, funding & volume</span>
                </div>
                <div className="p-2.5 bg-zinc-900 border border-zinc-800 rounded-lg flex items-center justify-between gap-2">
                  <div className="flex items-center space-x-2 flex-none">
                    <span className="w-2 h-2 rounded-full bg-purple-400 flex-none" />
                    <span className="text-zinc-200 font-bold">Bull vs. Bear Debate</span>
                  </div>
                  <span className="text-2xs text-zinc-400 truncate text-right">Aggressively stress-tests trade thesis</span>
                </div>
                <div className="p-2.5 bg-zinc-900 border border-zinc-800 rounded-lg flex items-center justify-between gap-2">
                  <div className="flex items-center space-x-2 flex-none">
                    <span className="w-2 h-2 rounded-full bg-amber-400 flex-none" />
                    <span className="text-zinc-200 font-bold">Risk Guardian (CRO)</span>
                  </div>
                  <span className="text-2xs text-zinc-400 truncate text-right">Enforces max 25% risk & stop-loss bounds</span>
                </div>
              </div>
              <p className="text-xs text-zinc-400">
                You can watch live debates occur in real-time or let the system filter out low-conviction noise automatically.
              </p>
            </div>
          )}

          {/* Step 3: Action Inbox & Outbox Integration */}
          {step === 3 && (
            <div className="space-y-3.5 text-xs sm:text-sm text-zinc-300 leading-relaxed">
              <p>
                Approved trade ideas appear in the <strong className="text-emerald-400">Action Inbox</strong> on the right. No reading 5-page logs required!
              </p>
              <div className="p-3.5 bg-zinc-900/90 border border-zinc-800 rounded-lg space-y-2.5">
                <div className="flex items-center justify-between text-xs font-mono">
                  <span className="text-emerald-400 font-bold flex items-center space-x-1.5">
                    <span className="w-2 h-2 rounded-full bg-zinc-500" />
                    <span>Verdict &amp; conviction (example layout)</span>
                  </span>
                  <span className="text-zinc-400 font-bold">&lt;symbol&gt; · &lt;side&gt;</span>
                </div>
                <ul className="text-xs space-y-1 text-zinc-300 list-disc list-inside">
                  <li><strong>Thesis:</strong> the portfolio manager's reasoning from the debate.</li>
                  <li><strong>Size:</strong> a % of paper equity (1–25%), shown in USD only when equity is known.</li>
                </ul>
                <div className="pt-2 border-t border-zinc-800 flex flex-wrap items-center justify-between gap-2">
                  <span className="text-2xs font-mono text-zinc-400">Interactive Sizing Slider</span>
                  <div className="flex items-center space-x-2">
                    <span className="px-2 py-0.5 bg-emerald-950 text-emerald-400 border border-emerald-800 rounded text-2xs font-mono font-bold">
                      1-Click Approve
                    </span>
                    <span className="px-2 py-0.5 bg-zinc-800 text-zinc-400 rounded text-2xs font-mono">
                      Reject
                    </span>
                  </div>
                </div>
              </div>
              <p className="text-xs text-zinc-400">
                Clicking <strong>Approve</strong> safely stages the order in the PostgreSQL Transactional Outbox for NautilusTrader execution.
              </p>
            </div>
          )}

          {/* Bottom Progress and Navigation Controls */}
          <div className="pt-3 border-t border-zinc-800 flex flex-wrap items-center justify-between gap-3">
            {/* Step Dots */}
            <div className="flex items-center space-x-1.5">
              {[1, 2, 3].map((s) => (
                <div
                  key={s}
                  onClick={() => setStep(s)}
                  className={`h-1.5 rounded-full cursor-pointer transition-all ${
                    step === s ? "w-6 bg-cyan-400" : "w-1.5 bg-zinc-700 hover:bg-zinc-500"
                  }`}
                />
              ))}
            </div>

            <div className="flex flex-wrap items-center gap-2">
              {step > 1 && (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={handleBack}
                  className="bg-zinc-900 border-zinc-800 text-zinc-300 hover:bg-zinc-800 h-8 text-xs font-mono"
                >
                  <ArrowLeft className="w-3 h-3 mr-1" /> Back
                </Button>
              )}

              {step < totalSteps ? (
                <Button
                  size="sm"
                  onClick={handleNext}
                  className="bg-cyan-600 hover:bg-cyan-500 text-zinc-950 font-bold h-8 text-xs font-mono"
                >
                  Next <ArrowRight className="w-3 h-3 ml-1" />
                </Button>
              ) : (
                <div className="flex flex-wrap items-center gap-2">
                  <Button
                    size="sm"
                    onClick={handleRunDebate}
                    className="bg-purple-600 hover:bg-purple-500 text-white font-bold h-8 text-xs font-mono flex items-center space-x-1"
                  >
                    <Zap className="w-3 h-3" />
                    <span>Run a real debate on {symbol || "the active symbol"}</span>
                  </Button>
                  <Button
                    size="sm"
                    onClick={handleComplete}
                    className="bg-cyan-600 hover:bg-cyan-500 text-zinc-950 font-bold h-8 text-xs font-mono flex items-center space-x-1"
                  >
                    <CheckCircle2 className="w-3 h-3" />
                    <span>Start Using AI Brain</span>
                  </Button>
                </div>
              )}
            </div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
