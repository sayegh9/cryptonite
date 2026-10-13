"use client";

import React, { useEffect, useState } from "react";
import { X, ExternalLink, Activity, BarChart2 } from "lucide-react";
import { CoinProfile } from "@/lib/types";
import { fetchApi } from "@/lib/api";

interface Props {
  symbol: string | null;
  onClose: () => void;
}

export const CoinProfileModal: React.FC<Props> = ({ symbol, onClose }) => {
  const [profile, setProfile] = useState<CoinProfile | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [descExpanded, setDescExpanded] = useState(false);
  const [isVisible, setIsVisible] = useState(false);

  useEffect(() => {
    if (symbol) {
      setIsVisible(true);
      setLoading(true);
      setError(null);
      setProfile(null);
      setDescExpanded(false);

      fetchApi(`/market/profile/${symbol}`)
        .then((data) => {
          setProfile(data);
        })
        .catch((err) => {
          setError(err.message || "Failed to load profile");
        })
        .finally(() => {
          setLoading(false);
        });
    } else {
      setIsVisible(false);
    }
  }, [symbol]);

  // Handle escape key
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape" && symbol) {
        onClose();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [symbol, onClose]);

  if (!symbol && !isVisible) return null;

  const isUp = profile?.change_24h_pct ? profile.change_24h_pct >= 0 : true;

  return (
    <>
      {/* Backdrop */}
      <div 
        className={`fixed inset-0 z-40 bg-black/50 backdrop-blur-sm transition-opacity duration-300 ${isVisible ? "opacity-100" : "opacity-0 pointer-events-none"}`}
        onClick={onClose}
      />
      
      {/* Sheet */}
      <div 
        className={`fixed top-0 right-0 z-50 h-full w-80 bg-zinc-950 border-l border-white/5 text-zinc-100 flex flex-col shadow-2xl transition-transform duration-300 ease-in-out transform ${isVisible ? "translate-x-0" : "translate-x-full"}`}
      >
        <div className="flex items-center justify-between p-4 border-b border-white/5">
          <div className="flex items-center space-x-3">
            {profile?.image_url ? (
              <img 
                src={profile.image_url} 
                alt={profile.name || symbol || ""} 
                className="w-8 h-8 rounded-full"
                onError={(e) => { e.currentTarget.style.display = 'none'; }}
              />
            ) : (
              <div className="w-8 h-8 rounded-full bg-zinc-800 flex items-center justify-center font-bold text-xs">
                {(symbol || "").substring(0, 1)}
              </div>
            )}
            <div>
              <div className="font-bold text-sm">{profile?.name || (symbol || "").replace(".BINANCE", "")}</div>
              <div className="text-xs text-zinc-500 font-mono">{symbol}</div>
            </div>
          </div>
          <button onClick={onClose} aria-label="Close" className="p-1 hover:bg-zinc-800 rounded transition-colors text-zinc-400 hover:text-white">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-4 space-y-6">
          {loading ? (
            <div className="space-y-4 animate-pulse">
              <div className="h-8 bg-zinc-900 rounded w-1/2"></div>
              <div className="h-4 bg-zinc-900 rounded w-full"></div>
              <div className="h-4 bg-zinc-900 rounded w-3/4"></div>
              <div className="grid grid-cols-2 gap-4 mt-6">
                <div className="h-12 bg-zinc-900 rounded"></div>
                <div className="h-12 bg-zinc-900 rounded"></div>
              </div>
            </div>
          ) : error ? (
            <div className="text-red-400 text-sm font-mono p-4 border border-red-900/50 bg-red-950/20 rounded">
              {error}
            </div>
          ) : profile ? (
            <>
              {/* Price Row */}
              <div className="flex flex-col space-y-1">
                <span className="text-2xs text-zinc-500 uppercase tracking-widest font-mono">Live Price</span>
                <div className="flex items-end space-x-3">
                  <span className="text-2xl font-bold font-mono">
                    {profile.last_price != null ? `$${profile.last_price.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 6 })}` : "—"}
                  </span>
                  <div className={`flex items-center space-x-1 text-sm font-mono pb-1 ${isUp ? "text-emerald-400" : "text-red-400"}`}>
                    <span>{profile.change_24h_pct == null ? "" : isUp ? "▲" : "▼"}</span>
                    <span>{profile.change_24h_pct != null ? `${Math.abs(profile.change_24h_pct).toFixed(2)}%` : "—"}</span>
                  </div>
                </div>
              </div>

              {/* 24h Stats */}
              <div className="grid grid-cols-3 gap-2 border-t border-b border-white/5 py-4">
                <div className="flex flex-col space-y-1">
                  <span className="text-2xs text-zinc-500 uppercase tracking-widest font-mono">24h High</span>
                  <span className="text-xs font-mono">{profile.high_24h != null ? `$${profile.high_24h.toLocaleString()}` : "—"}</span>
                </div>
                <div className="flex flex-col space-y-1">
                  <span className="text-2xs text-zinc-500 uppercase tracking-widest font-mono">24h Low</span>
                  <span className="text-xs font-mono">{profile.low_24h != null ? `$${profile.low_24h.toLocaleString()}` : "—"}</span>
                </div>
                <div className="flex flex-col space-y-1">
                  <span className="text-2xs text-zinc-500 uppercase tracking-widest font-mono">24h Vol</span>
                  <span className="text-xs font-mono">{profile.volume_24h_quote != null ? `$${(profile.volume_24h_quote / 1000000).toFixed(1)}M` : "—"}</span>
                </div>
              </div>

              {/* Market Stats */}
              <div className="space-y-3">
                <span className="text-2xs text-zinc-500 uppercase tracking-widest font-mono">Market Stats</span>
                <div className="grid grid-cols-2 gap-y-3 gap-x-4 text-xs font-mono">
                  <div className="flex flex-col">
                    <span className="text-zinc-500">Market Cap</span>
                    <span>{profile.market_cap_usd ? `$${(profile.market_cap_usd / 1e9).toFixed(2)}B` : "-"}</span>
                  </div>
                  <div className="flex flex-col">
                    <span className="text-zinc-500">Circ. Supply</span>
                    <span>{profile.circulating_supply ? profile.circulating_supply.toLocaleString(undefined, { maximumFractionDigits: 0 }) : "-"}</span>
                  </div>
                  <div className="flex flex-col">
                    <span className="text-zinc-500">Max Supply</span>
                    <span>{profile.max_supply ? profile.max_supply.toLocaleString(undefined, { maximumFractionDigits: 0 }) : "∞"}</span>
                  </div>
                  <div className="flex flex-col">
                    <span className="text-zinc-500">ATH</span>
                    <span>{profile.ath_usd ? `$${profile.ath_usd.toLocaleString()}` : "-"}</span>
                    {profile.ath_date && <span className="text-2xs text-zinc-600">{new Date(profile.ath_date).toLocaleDateString()}</span>}
                  </div>
                </div>
              </div>

              {/* Categories */}
              {profile.categories && profile.categories.length > 0 && (
                <div className="flex flex-wrap gap-1.5">
                  {profile.categories.map((c, i) => (
                    <span key={i} className="px-2 py-0.5 bg-zinc-900 border border-white/10 rounded text-2xs text-zinc-300">
                      {c}
                    </span>
                  ))}
                </div>
              )}

              {/* Description */}
              {profile.description && (
                <div className="space-y-1 border-t border-white/5 pt-4">
                  <span className="text-2xs text-zinc-500 uppercase tracking-widest font-mono">About</span>
                  <p 
                    className={`text-xs text-zinc-400 leading-relaxed cursor-pointer ${descExpanded ? "" : "line-clamp-4"}`}
                    onClick={() => setDescExpanded(!descExpanded)}
                    dangerouslySetInnerHTML={{ __html: profile.description }}
                  />
                  {!descExpanded && (
                    <button onClick={() => setDescExpanded(true)} className="text-2xs text-cyan-500 hover:text-cyan-400">
                      Read more
                    </button>
                  )}
                </div>
              )}

              {/* Links */}
              {profile.links && (Object.values(profile.links).some(l => l)) && (
                <div className="space-y-2 border-t border-white/5 pt-4">
                  <span className="text-2xs text-zinc-500 uppercase tracking-widest font-mono">Links</span>
                  <div className="flex flex-wrap gap-3 text-xs">
                    {profile.links.homepage && (
                      <a href={profile.links.homepage} target="_blank" rel="noreferrer" className="flex items-center space-x-1 text-cyan-500 hover:text-cyan-400">
                        <ExternalLink className="w-3 h-3" /><span>Website</span>
                      </a>
                    )}
                    {profile.links.twitter && (
                      <a href={`https://twitter.com/${profile.links.twitter}`} target="_blank" rel="noreferrer" className="flex items-center space-x-1 text-cyan-500 hover:text-cyan-400">
                        <ExternalLink className="w-3 h-3" /><span>Twitter</span>
                      </a>
                    )}
                    {profile.links.github && (
                      <a href={profile.links.github} target="_blank" rel="noreferrer" className="flex items-center space-x-1 text-cyan-500 hover:text-cyan-400">
                        <ExternalLink className="w-3 h-3" /><span>GitHub</span>
                      </a>
                    )}
                  </div>
                </div>
              )}
            </>
          ) : null}
        </div>

        {/* Quick Actions */}
        <div className="p-4 border-t border-white/5 grid grid-cols-2 gap-3 bg-zinc-950">
          <button 
            onClick={() => {
              if (symbol) {
                window.dispatchEvent(new CustomEvent("cryptonite:select-symbol", { detail: { symbol } }));
                onClose();
              }
            }}
            className="flex items-center justify-center space-x-2 px-3 py-2 bg-zinc-900 hover:bg-zinc-800 border border-white/10 rounded text-xs font-bold transition-colors"
          >
            <BarChart2 className="w-4 h-4" />
            <span>Open Chart</span>
          </button>
          <button 
            onClick={onClose}
            className="flex items-center justify-center space-x-2 px-3 py-2 bg-cyan-900/30 hover:bg-cyan-900/50 text-cyan-400 border border-cyan-800/50 rounded text-xs font-bold transition-colors"
          >
            <Activity className="w-4 h-4" />
            <span>View Signals</span>
          </button>
        </div>
      </div>
    </>
  );
};
