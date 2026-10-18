import type { VersionedMetrics } from "@/lib/backtestMetrics";

export interface Bar {
  time: number; // Unix timestamp in seconds (for Lightweight Charts)
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  bar_open_ns: number;
  bar_close_inclusive_ns: number;
  interval_end_exclusive_ns: number;
}

export interface Instrument {
  id: string;
  symbol: string;
  venue: string;
  market_type: string;
  price_precision: number;
  size_precision: number;
}

export interface OpenKlineTick {
  event_id: string;
  instrument_id: string;
  timeframe: string;
  update_sequence: number;
  bar_open_ns: number;
  bar_close_inclusive_ns: number;
  interval_end_exclusive_ns: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface ClosedBarCommittedEvent {
  event_id: string;
  instrument_id: string;
  timeframe: string;
  bar_open_ns: number; // the batch's first bar OPEN
  bar_close_inclusive_ns: number;
  interval_end_exclusive_ns: number; // the batch's last bar close
  last_bar_open_ns: number;
  bar_count: number;
  // final values of the batch's last bar, as decimal strings
  open: string;
  high: string;
  low: string;
  close: string;
  volume: string;
}

export interface SignalCandidate {
  id: string;
  scan_run_id: string;
  spec_version_id: string;
  instrument_id: string;
  timeframe: string;
  bar_open_ns: number;
  bar_close_inclusive_ns: number;
  interval_end_exclusive_ns: number;
  matched_rules: string[];
  metrics: Record<string, any>;
  suggested_entry: number;
  suggested_stop: number;
  suggested_target: number;
  created_at: string;
}

export interface ScanSpecVersion {
  id: string;
  spec_id: string;
  version_num: number;
  spec_hash: string;
  universe: {
    instrument_ids: string[];
    timeframes: string[];
  };
  rules: any[];
  created_at: string;
}

export interface ScanSpec {
  id: string;
  slug: string;
  name: string;
  description?: string;
  active_version_id?: string;
  active_version?: ScanSpecVersion;
  is_archived: boolean;
  created_at: string;
  updated_at: string;
}

export interface ScanJob {
  id: string;
  request_id: string;
  spec_version_id: string;
  status: "PENDING" | "RUNNING" | "COMPLETED" | "FAILED";
  scan_run_id?: string;
  candidate_count?: number;
  error_message?: string;
  created_at: string;
  completed_at?: string;
}

export interface Strategy {
  id: string;
  name: string;
  slug: string;
  description?: string;
  author?: string;
  active_version_id: string;
  version_num: number;
  code_hash: string;
  default_parameters: Record<string, any>;
  parameter_schema: Record<string, any>;
  thesis?: string;
  category?: string;
  market_regime?: string;
  timeframe_default?: string;
}

export interface BacktestMetrics extends VersionedMetrics {
  total_return: number;
  cagr?: number | null;
  sharpe_ratio?: number | null;
  sortino_ratio?: number | null;
  max_drawdown: number;
  profit_factor?: number | null;
  win_rate: number;
  total_trades: number;
  winning_trades: number;
  losing_trades: number;
  avg_trade_pnl: number;
  max_consecutive_losses: number;
}

export interface DownsampledEquityPoint {
  ts: number; // Unix timestamp ms
  equity: number;
  drawdown: number;
}

export interface BacktestResult {
  metrics: BacktestMetrics;
  downsampled_equity: DownsampledEquityPoint[];
  monthly_returns: Record<string, Record<string, number>>;
  catalog_manifest: any[];
  artifact_manifest: Record<string, any>;
}

export interface BacktestRun {
  id: string;
  strategy_slug: string;
  strategy_name?: string;
  instrument_id: string;
  bar_type: string;
  timeframe: string;
  parameters: Record<string, any>;
  warmup_start_ns?: number;
  evaluation_start_ns?: number;
  evaluation_end_ns?: number;
  execution_profile: string;
  run_config_hash: string;
  status: "PENDING" | "CLAIMED" | "RUNNING" | "COMPLETED" | "FAILED";
  error_message?: string | null;
  created_at: string;
  started_at?: string | null;
  completed_at?: string | null;
  results?: BacktestResult | null;
  metrics?: BacktestMetrics | null;
}

export interface BacktestTrade {
  position_id: string;
  instrument_id: string;
  side: string;
  ts_opened_ns: number;
  ts_closed_ns: number;
  entry_price: number;
  exit_price: number;
  quantity: number;
  realized_pnl: number;
  realized_return: number;
}

export interface MarketTicker24h {
  symbol: string;
  last_price: number;
  price_change_pct: number;
  high_24h: number;
  low_24h: number;
  volume: number;
  quote_volume: number;
}

export interface CoinProfile {
  symbol: string;
  name?: string;
  description?: string;
  categories?: string[];
  image_url?: string;
  market_cap_usd?: number;
  circulating_supply?: number;
  max_supply?: number;
  ath_usd?: number;
  ath_date?: string;
  last_price?: number;
  change_24h_pct?: number;
  high_24h?: number;
  low_24h?: number;
  volume_24h_quote?: number;
  links?: {
    homepage?: string;
    twitter?: string;
    github?: string;
  };
}

export interface UserAlert {
  id: string;
  instrument_id: string;
  condition: 'above' | 'below';
  threshold: number;
  is_active: boolean;
  triggered_at?: string | null;
  created_at: string;
}

export interface MarketOverviewTicker {
  symbol: string;
  last_price: number;
  change_pct: number;
  volume_quote: number;
}
