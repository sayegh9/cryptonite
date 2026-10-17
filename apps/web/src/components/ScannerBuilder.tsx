"use client";

import { useState } from "react";
import { fetchApi } from "@/lib/api";
import { Save, Plus, X, ListFilter, Sparkles } from "lucide-react";

interface BuilderCondition {
  category: "trend" | "trigger" | "confirmation" | "liquidity" | "risk";
  field: string;
  operator: string;
  value: string;
  lookback: number;
}

const SWING_LIQUID_12 = [
  "BTCUSDT.BINANCE",
  "ETHUSDT.BINANCE",
  "SOLUSDT.BINANCE",
  "BNBUSDT.BINANCE",
  "XRPUSDT.BINANCE",
  "DOGEUSDT.BINANCE",
  "ADAUSDT.BINANCE",
  "AVAXUSDT.BINANCE",
  "LINKUSDT.BINANCE",
  "SUIUSDT.BINANCE",
  "NEARUSDT.BINANCE",
  "WLDUSDT.BINANCE",
];

interface SwingPresetDef {
  slug: string;
  name: string;
  description: string;
  instruments: string;
  timeframes: string;
  minRiskReward: number;
  stopAtrMultiplier: number;
  conditions: BuilderCondition[];
}

const SWING_PRESETS: Record<string, SwingPresetDef> = {
  "swing-trend-pullback-1h": {
    slug: "swing-trend-pullback-1h",
    name: "Swing Trend Pullback 1H/4H",
    description: "1H swing trend pullback setup requiring HTF trend confluence (4H Close > EMA50) with RSI pullback bounce and volume.",
    instruments: SWING_LIQUID_12.join(", "),
    timeframes: "15m, 1h, 4h",
    minRiskReward: 1.8,
    stopAtrMultiplier: 2.0,
    conditions: [
      { category: "trend", field: "htf_close", operator: "gt", value: "htf_ema_50", lookback: 0 },
      { category: "trend", field: "close", operator: "gt", value: "ema_50", lookback: 0 },
      { category: "trigger", field: "rsi", operator: "cross_above", value: "40", lookback: 1 },
      { category: "confirmation", field: "close", operator: "gt", value: "open", lookback: 0 },
      { category: "confirmation", field: "volume_ratio", operator: "gte", value: "0.9", lookback: 0 },
    ],
  },
  "swing-sniper-pullback-15m": {
    slug: "swing-sniper-pullback-15m",
    name: "Swing Sniper 15m/1H/4H",
    description: "15m sniper entry into 1H/4H swing uptrend (HTF Close > EMA50) with lower Bollinger Band oversold bounce and volume.",
    instruments: SWING_LIQUID_12.join(", "),
    timeframes: "15m, 1h, 4h",
    minRiskReward: 1.8,
    stopAtrMultiplier: 2.0,
    conditions: [
      { category: "trend", field: "htf_close", operator: "gt", value: "htf_ema_50", lookback: 0 },
      { category: "trigger", field: "close", operator: "cross_above", value: "bb_lower", lookback: 1 },
      { category: "confirmation", field: "close", operator: "gt", value: "open", lookback: 0 },
      { category: "confirmation", field: "volume_ratio", operator: "gte", value: "0.9", lookback: 0 },
    ],
  },
  "swing-squeeze-breakout-1h": {
    slug: "swing-squeeze-breakout-1h",
    name: "Swing Volatility Breakout 1H/4H",
    description: "1H volatility breakout closing above upper Bollinger Band with HTF trend alignment (4H Close > EMA50) and volume expansion.",
    instruments: SWING_LIQUID_12.join(", "),
    timeframes: "15m, 1h, 4h",
    minRiskReward: 1.8,
    stopAtrMultiplier: 2.0,
    conditions: [
      { category: "trend", field: "htf_close", operator: "gt", value: "htf_ema_50", lookback: 0 },
      { category: "trend", field: "htf_rsi", operator: "gt", value: "50", lookback: 0 },
      { category: "trigger", field: "close", operator: "cross_above", value: "bb_upper", lookback: 1 },
      { category: "confirmation", field: "volume_ratio", operator: "gte", value: "1.2", lookback: 0 },
    ],
  },
};

const AVAILABLE_FIELDS = [
  "close",
  "open",
  "high",
  "low",
  "volume_ratio",
  "rsi",
  "ema_20",
  "ema_50",
  "ema_200",
  "bb_upper",
  "bb_lower",
  "htf_close",
  "htf_ema_50",
  "htf_rsi",
];

export function ScannerBuilder({ onCreated }: { onCreated: () => void }) {
  const [slug, setSlug] = useState("swing-trend-pullback-1h");
  const [name, setName] = useState("Swing Trend Pullback 1H/4H");
  const [description, setDescription] = useState("1H swing trend pullback setup requiring HTF trend confluence (4H Close > EMA50) with RSI pullback bounce and volume.");
  const [instruments, setInstruments] = useState(SWING_LIQUID_12.join(", "));
  const [timeframes, setTimeframes] = useState("15m, 1h, 4h");
  const [minRiskReward, setMinRiskReward] = useState(1.8);
  const [stopAtrMultiplier, setStopAtrMultiplier] = useState(2.0);
  const [conditions, setConditions] = useState<BuilderCondition[]>([
    { category: "trend", field: "htf_close", operator: "gt", value: "htf_ema_50", lookback: 0 },
    { category: "trend", field: "close", operator: "gt", value: "ema_50", lookback: 0 },
    { category: "trigger", field: "rsi", operator: "cross_above", value: "40", lookback: 1 },
    { category: "confirmation", field: "close", operator: "gt", value: "open", lookback: 0 },
    { category: "confirmation", field: "volume_ratio", operator: "gte", value: "0.9", lookback: 0 },
  ]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const handleLoadPreset = (presetKey: string) => {
    const preset = SWING_PRESETS[presetKey];
    if (!preset) return;
    setSlug(preset.slug);
    setName(preset.name);
    setDescription(preset.description);
    setInstruments(preset.instruments);
    setTimeframes(preset.timeframes);
    setMinRiskReward(preset.minRiskReward);
    setStopAtrMultiplier(preset.stopAtrMultiplier);
    setConditions(preset.conditions);
  };

  const handleAddCondition = () => {
    setConditions([
      ...conditions,
      { category: "trend", field: "htf_close", operator: "gt", value: "htf_ema_50", lookback: 0 },
    ]);
  };

  const handleRemoveCondition = (index: number) => {
    setConditions(conditions.filter((_, i) => i !== index));
  };

  const handleConditionChange = (index: number, field: keyof BuilderCondition, value: any) => {
    const updated = [...conditions];
    updated[index] = { ...updated[index], [field]: value };
    // Automatically set lookback to 1 on crossover operators if lookback is 0
    if (field === "operator" && (value === "cross_above" || value === "cross_below") && updated[index].lookback === 0) {
      updated[index].lookback = 1;
    }
    setConditions(updated);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError("");

    try {
      const instrument_ids = instruments
        .split(",")
        .map((i) => i.trim().toUpperCase())
        .filter(Boolean)
        .map((i) => (i.includes(".") ? i : `${i}.BINANCE`));

      const timeframesList = timeframes
        .split(",")
        .map((t) => t.trim().toLowerCase())
        .filter(Boolean);

      const groupsMap: Record<string, any[]> = {};
      for (const c of conditions) {
        if (!groupsMap[c.category]) groupsMap[c.category] = [];
        const isNumeric = !isNaN(Number(c.value)) && c.value.trim() !== "";
        groupsMap[c.category].push({
          field: c.field,
          operator: c.operator,
          value: isNumeric ? Number(c.value) : c.value.trim(),
          lookback: Number(c.lookback) || (c.operator.includes("cross") ? 1 : 0),
        });
      }

      const rules = Object.entries(groupsMap).map(([category, conds]) => ({
        category,
        logic: "AND",
        conditions: conds,
      }));

      const finalSlug = slug.trim() || name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/(^-|-$)/g, "");

      const payload = {
        slug: finalSlug,
        name: name.trim(),
        description: description.trim() || undefined,
        universe: {
          instrument_ids: instrument_ids.length > 0 ? instrument_ids : SWING_LIQUID_12,
          timeframes: timeframesList.length > 0 ? timeframesList : ["15m", "1h", "4h"],
        },
        rules,
        risk: {
          min_risk_reward: Number(minRiskReward) || 1.8,
          stop_atr_multiplier: Number(stopAtrMultiplier) || 2.0,
          fallback_stop_atr_mult: Number(stopAtrMultiplier) || 2.0,
        },
      };

      await fetchApi("/scanners", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": localStorage.getItem("csrf_token") ?? "",
        },
        body: JSON.stringify(payload),
      });

      onCreated();
    } catch (err: any) {
      setError(err.message || "Failed to create scanner specification");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-5 mt-4 font-mono text-xs">
      <div className="flex flex-wrap items-center justify-between gap-3 mb-4 border-b border-zinc-800 pb-3">
        <h3 className="text-sm font-bold text-zinc-200 flex items-center space-x-2">
          <ListFilter className="w-4 h-4 text-cyan-400" />
          <span>Scanner Studio: Create Custom Spec</span>
        </h3>

        {/* 1-Click Load Swing Preset Selector */}
        <div className="flex items-center space-x-2">
          <Sparkles className="w-3.5 h-3.5 text-amber-400" />
          <label htmlFor="swing-preset-selector" className="text-2xs text-zinc-400">
            Load Swing Preset:
          </label>
          <select
            id="swing-preset-selector"
            aria-label="Load Swing Preset"
            defaultValue=""
            onChange={(e) => {
              if (e.target.value) handleLoadPreset(e.target.value);
            }}
            className="bg-zinc-950 border border-zinc-700 rounded px-2.5 py-1 text-2xs text-cyan-300 focus:border-cyan-500 focus:outline-none"
          >
            <option value="" disabled>
              -- Select a preset to load --
            </option>
            <option value="swing-trend-pullback-1h">Swing Trend Pullback 1H/4H</option>
            <option value="swing-sniper-pullback-15m">Swing Sniper 15m/1H/4H</option>
            <option value="swing-squeeze-breakout-1h">Swing Volatility Breakout 1H/4H</option>
          </select>
        </div>
      </div>

      {error && <div className="bg-red-950/40 border border-red-900 text-red-400 text-xs p-3 rounded mb-4">{error}</div>}

      <form onSubmit={handleSubmit} className="space-y-4">
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          <div>
            <label className="block text-2xs text-zinc-400 mb-1">Slug (unique key)</label>
            <input
              type="text"
              value={slug}
              onChange={(e) => setSlug(e.target.value)}
              className="w-full bg-zinc-950 border border-zinc-800 rounded px-3 py-1.5 text-xs text-zinc-200 focus:border-cyan-500 focus:outline-none"
              placeholder="e.g. swing-trend-pullback-1h"
              required
            />
          </div>
          <div>
            <label className="block text-2xs text-zinc-400 mb-1">Scanner Name</label>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="w-full bg-zinc-950 border border-zinc-800 rounded px-3 py-1.5 text-xs text-zinc-200 focus:border-cyan-500 focus:outline-none"
              placeholder="e.g. Swing Trend Pullback (1H/4H)"
              required
            />
          </div>
          <div>
            <label className="block text-2xs text-zinc-400 mb-1">Description</label>
            <input
              type="text"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              className="w-full bg-zinc-950 border border-zinc-800 rounded px-3 py-1.5 text-xs text-zinc-200 focus:border-cyan-500 focus:outline-none"
              placeholder="Brief description..."
              required
            />
          </div>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <div>
            <label className="block text-2xs text-zinc-400 mb-1">Universe Symbols (comma-separated)</label>
            <input
              type="text"
              value={instruments}
              onChange={(e) => setInstruments(e.target.value)}
              className="w-full bg-zinc-950 border border-zinc-800 rounded px-3 py-1.5 text-xs text-zinc-200 focus:border-cyan-500 focus:outline-none uppercase"
              placeholder="BTCUSDT.BINANCE, ETHUSDT.BINANCE"
              required
            />
          </div>
          <div>
            <label className="block text-2xs text-zinc-400 mb-1">Timeframes (comma-separated, e.g. 15m, 1h, 4h)</label>
            <input
              type="text"
              value={timeframes}
              onChange={(e) => setTimeframes(e.target.value)}
              className="w-full bg-zinc-950 border border-zinc-800 rounded px-3 py-1.5 text-xs text-zinc-200 focus:border-cyan-500 focus:outline-none lowercase"
              placeholder="15m, 1h, 4h"
              required
            />
          </div>
        </div>

        <div className="grid grid-cols-2 gap-3 bg-zinc-950/60 p-2.5 rounded border border-zinc-800">
          <div>
            <label className="block text-2xs text-zinc-400 mb-1">Min Risk/Reward (R:R)</label>
            <input
              type="number"
              step="0.1"
              min="1.0"
              value={minRiskReward}
              onChange={(e) => setMinRiskReward(Number(e.target.value))}
              className="w-full bg-zinc-900 border border-zinc-700 rounded px-2.5 py-1 text-xs text-zinc-200 focus:border-cyan-500 focus:outline-none"
              required
            />
          </div>
          <div>
            <label className="block text-2xs text-zinc-400 mb-1">Stop ATR Multiplier</label>
            <input
              type="number"
              step="0.1"
              min="0.5"
              value={stopAtrMultiplier}
              onChange={(e) => setStopAtrMultiplier(Number(e.target.value))}
              className="w-full bg-zinc-900 border border-zinc-700 rounded px-2.5 py-1 text-xs text-zinc-200 focus:border-cyan-500 focus:outline-none"
              required
            />
          </div>
        </div>

        <div>
          <div className="flex items-center justify-between mb-2">
            <label className="block text-2xs uppercase tracking-wider text-zinc-400">
              Rule Conditions (grouped into RuleGroups by category)
            </label>
            <button
              type="button"
              onClick={handleAddCondition}
              className="flex items-center space-x-1 text-xs text-cyan-400 hover:text-cyan-300"
            >
              <Plus className="w-3 h-3" />
              <span>Add Condition</span>
            </button>
          </div>

          <div className="space-y-2">
            {conditions.map((cond, idx) => (
              <div key={idx} className="flex flex-wrap items-center gap-2 bg-zinc-950 border border-zinc-800 rounded p-2">
                <select
                  value={cond.category}
                  onChange={(e) => handleConditionChange(idx, "category", e.target.value)}
                  className="bg-zinc-900 border border-zinc-700 rounded px-2 py-1 text-xs text-zinc-300 focus:border-cyan-500 focus:outline-none"
                >
                  <option value="trend">trend</option>
                  <option value="trigger">trigger</option>
                  <option value="confirmation">confirmation</option>
                  <option value="liquidity">liquidity</option>
                  <option value="risk">risk</option>
                </select>

                <select
                  value={cond.field}
                  onChange={(e) => handleConditionChange(idx, "field", e.target.value)}
                  className="bg-zinc-900 border border-zinc-700 rounded px-2 py-1 text-xs text-cyan-300 font-semibold focus:border-cyan-500 focus:outline-none"
                >
                  {AVAILABLE_FIELDS.map((f) => (
                    <option key={f} value={f}>
                      {f}
                    </option>
                  ))}
                </select>

                <select
                  value={cond.operator}
                  onChange={(e) => handleConditionChange(idx, "operator", e.target.value)}
                  className="bg-zinc-900 border border-zinc-700 rounded px-2 py-1 text-xs text-amber-400 font-semibold focus:border-cyan-500 focus:outline-none"
                >
                  <option value="gt">&gt; (gt)</option>
                  <option value="gte">&ge; (gte)</option>
                  <option value="lt">&lt; (lt)</option>
                  <option value="lte">&le; (lte)</option>
                  <option value="cross_above">cross_above</option>
                  <option value="cross_below">cross_below</option>
                </select>

                <input
                  type="text"
                  placeholder="Value or column (e.g. htf_ema_50, 40)"
                  value={cond.value}
                  onChange={(e) => handleConditionChange(idx, "value", e.target.value)}
                  className="flex-1 min-w-[120px] bg-zinc-900 border border-zinc-700 rounded px-2 py-1 text-xs text-zinc-100 focus:border-cyan-500 focus:outline-none"
                  required
                />

                <div className="flex items-center space-x-1">
                  <span className="text-2xs text-zinc-500">LB:</span>
                  <input
                    type="number"
                    min="0"
                    placeholder="Lookback"
                    value={cond.lookback}
                    onChange={(e) => handleConditionChange(idx, "lookback", Number(e.target.value))}
                    className="w-16 bg-zinc-900 border border-zinc-700 rounded px-2 py-1 text-xs text-center focus:border-cyan-500 focus:outline-none"
                    required
                  />
                </div>

                <button
                  type="button"
                  onClick={() => handleRemoveCondition(idx)}
                  className="p-1.5 text-zinc-500 hover:text-red-400 transition-colors"
                  aria-label="Remove condition"
                >
                  <X className="w-4 h-4" />
                </button>
              </div>
            ))}
          </div>
        </div>

        <button
          type="submit"
          disabled={loading || conditions.length === 0}
          className="w-full mt-2 flex items-center justify-center space-x-2 bg-cyan-600 hover:bg-cyan-500 text-zinc-950 py-2.5 rounded font-bold uppercase transition-colors text-sm disabled:opacity-50"
        >
          <Save className="w-4 h-4" />
          <span>{loading ? "SAVING..." : "SAVE SCANNER SPEC"}</span>
        </button>
      </form>
    </div>
  );
}
