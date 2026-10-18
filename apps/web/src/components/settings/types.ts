import type { RegistryEntry } from "./controls/SettingCard";
import type { SettingsSection } from "@/lib/navigation";

export type { RegistryEntry, SettingsSection };

export interface RouteFields {
  primary_model: string | null;
  fallback_model: string | null;
  fallback_policy: string | null;
  daily_cap_usd: string | null;
  max_output_tokens: number | null;
  thinking_budget: number | null;
}

export interface RouteView {
  role: string;
  protected: boolean;
  editable: boolean;
  local_allowed: boolean;
  default: RouteFields;
  effective: RouteFields;
  stored: RouteFields | null;
  overridden: boolean;
}

export interface AgentConfig {
  model_name: string | null;
  temperature: number | null;
  system_instructions: string | null;
}

export interface Warning {
  code: string;
  severity: "critical" | "warn" | "info";
  text: string;
}

export interface EnvHealth {
  gemini_key_set: boolean;
  binance_key_set: boolean;
  environment_mode: string;
  kill_switch_active: boolean;
  dev_totp_login: boolean;
  dev_totp_approvals: boolean;
  secure_cookie: boolean;
  shadow_allow_paid: boolean;
  warnings: Warning[];
}

export interface AuditEvent {
  event_id: string;
  username: string;
  action: string;
  ip_address: string | null;
  details: Record<string, unknown> | null;
  occurred_at: string;
}
