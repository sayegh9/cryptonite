"use client";

import { useState } from "react";
import { fetchApi } from "@/lib/api";
import { Save, Plus, X, ListFilter } from "lucide-react";

export function ScannerBuilder({ onCreated }: { onCreated: () => void }) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [instruments, setInstruments] = useState("BTCUSDT,ETHUSDT,SOLUSDT");
  const [rules, setRules] = useState([{ name: "rsi_oversold", parameters: { threshold: 30, lookback: 14 } }]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const handleAddRule = () => {
    setRules([...rules, { name: "volume_spike", parameters: { threshold: 2.5, lookback: 20 } }]);
  };

  const handleRemoveRule = (index: number) => {
    setRules(rules.filter((_, i) => i !== index));
  };

  const handleRuleChange = (index: number, field: string, value: any) => {
    const newRules = [...rules];
    if (field === "name") {
      newRules[index].name = value;
    } else {
      newRules[index].parameters = { ...newRules[index].parameters, [field]: value };
    }
    setRules(newRules);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError("");

    try {
      const universe = instruments.split(",").map(i => i.trim()).filter(i => i);
      await fetchApi("/scanners", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": localStorage.getItem("csrf_token") ?? "",
        },
        body: JSON.stringify({
          name,
          description,
          universe,
          rules,
        }),
      });
      onCreated();
    } catch (err: any) {
      setError(err.message || "Failed to create scanner specification");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-5 mt-4">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-sm font-bold text-zinc-200 flex items-center space-x-2">
          <ListFilter className="w-4 h-4 text-cyan-400" />
          <span>Scanner Studio: Create Custom Spec</span>
        </h3>
      </div>

      {error && <div className="bg-red-950/40 border border-red-900 text-red-400 text-xs p-3 rounded mb-4">{error}</div>}

      <form onSubmit={handleSubmit} className="space-y-4">
        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="block text-xs font-mono text-zinc-400 mb-1">Scanner Name</label>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="w-full bg-zinc-950 border border-zinc-800 rounded px-3 py-2 text-sm text-zinc-200 focus:border-cyan-500 focus:outline-none"
              placeholder="e.g. Oversold Tech"
              required
            />
          </div>
          <div>
            <label className="block text-xs font-mono text-zinc-400 mb-1">Description</label>
            <input
              type="text"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              className="w-full bg-zinc-950 border border-zinc-800 rounded px-3 py-2 text-sm text-zinc-200 focus:border-cyan-500 focus:outline-none"
              placeholder="Brief description..."
              required
            />
          </div>
        </div>

        <div>
          <label className="block text-xs font-mono text-zinc-400 mb-1">Universe (comma-separated)</label>
          <input
            type="text"
            value={instruments}
            onChange={(e) => setInstruments(e.target.value)}
            className="w-full bg-zinc-950 border border-zinc-800 rounded px-3 py-2 text-sm text-zinc-200 focus:border-cyan-500 focus:outline-none font-mono uppercase"
            placeholder="BTCUSDT, ETHUSDT"
            required
          />
        </div>

        <div>
          <div className="flex items-center justify-between mb-2">
            <label className="block text-xs font-mono text-zinc-400">Rules</label>
            <button
              type="button"
              onClick={handleAddRule}
              className="flex items-center space-x-1 text-xs text-cyan-400 hover:text-cyan-300"
            >
              <Plus className="w-3 h-3" />
              <span>Add Rule</span>
            </button>
          </div>
          
          <div className="space-y-2">
            {rules.map((rule, idx) => (
              <div key={idx} className="flex items-center space-x-2 bg-zinc-950 border border-zinc-800 rounded p-2">
                <select
                  value={rule.name}
                  onChange={(e) => handleRuleChange(idx, "name", e.target.value)}
                  className="flex-1 bg-zinc-900 border border-zinc-700 rounded px-2 py-1.5 text-xs text-zinc-200 focus:border-cyan-500 focus:outline-none"
                >
                  <option value="rsi_oversold">RSI Oversold</option>
                  <option value="rsi_overbought">RSI Overbought</option>
                  <option value="ema_cross_above">EMA Cross Above</option>
                  <option value="ema_cross_below">EMA Cross Below</option>
                  <option value="volume_spike">Volume Spike</option>
                </select>
                
                <div className="flex-1 flex space-x-2">
                  <input
                    type="number"
                    placeholder="Lookback"
                    value={rule.parameters.lookback || ""}
                    onChange={(e) => handleRuleChange(idx, "lookback", Number(e.target.value))}
                    className="w-1/2 bg-zinc-900 border border-zinc-700 rounded px-2 py-1.5 text-xs font-mono focus:border-cyan-500 focus:outline-none"
                    required
                  />
                  <input
                    type="number"
                    step="0.1"
                    placeholder="Threshold"
                    value={rule.parameters.threshold || ""}
                    onChange={(e) => handleRuleChange(idx, "threshold", Number(e.target.value))}
                    className="w-1/2 bg-zinc-900 border border-zinc-700 rounded px-2 py-1.5 text-xs font-mono focus:border-cyan-500 focus:outline-none"
                    required
                  />
                </div>
                
                <button
                  type="button"
                  onClick={() => handleRemoveRule(idx)}
                  className="p-1.5 text-zinc-500 hover:text-red-400 transition-colors"
                >
                  <X className="w-4 h-4" />
                </button>
              </div>
            ))}
          </div>
        </div>

        <button
          type="submit"
          disabled={loading || rules.length === 0}
          className="w-full mt-2 flex items-center justify-center space-x-2 bg-cyan-600 hover:bg-cyan-500 text-white py-2.5 rounded font-bold uppercase transition-colors text-sm"
        >
          <Save className="w-4 h-4" />
          <span>{loading ? "SAVING..." : "SAVE SCANNER SPEC"}</span>
        </button>
      </form>
    </div>
  );
}
