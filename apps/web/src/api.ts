export type Session = {
  access_token: string;
  id_token: string;
  refresh_token?: string | null;
  expires_in?: number | null;
  token_type?: string;
};

export type Principal = {
  sub: string;
  username: string;
  email: string;
  groups: string[];
  is_admin: boolean;
};

export type Scenario = {
  scenario_id: string;
  name: string;
  description: string;
  objective: string;
  profile: string;
  read_only_tools: string[];
  restricted_actions: string[];
};

export type Summary = {
  run_id: string;
  scenario_id: string;
  created_at: string;
  state_revision: number;
  entity_counts: Record<string, number>;
  device_status_counts: Record<string, number>;
  vpn_status_counts: Record<string, number>;
  service_status_counts: Record<string, number>;
  incident_status_counts: Record<string, number>;
  faults: Record<string, string[]>;
  event_count: number;
  observation_count: number;
  operation_status_counts?: Record<string, number>;
  approval_status_counts?: Record<string, number>;
};

export type Device = { device_id: string; hostname: string; office: string; status: string; vpn_status: string; employee_id?: string };
export type Incident = { incident_id: string; title: string; severity: string; status: string; affected_device_ids?: string[] };
export type Service = { service_id: string; name: string; status: string; criticality: string; dependency_ids: string[] };

export type Snapshot = {
  run_id: string;
  scenario_id: string;
  summary: Summary;
  baseline_summary: Summary;
  state: { devices: Record<string, Device>; incidents: Record<string, Incident>; services: Record<string, Service>; [key: string]: Record<string, unknown> };
  faults: Record<string, string[]>;
  events: EventRecord[];
  observation_count: number;
};

export type Topology = { run_id: string; state_revision: number; services: Service[] };
export type Comparison = { before: { revision: number; summary: Summary }; after: { revision: number; summary: Summary }; changed: Record<string, Array<Record<string, unknown>>> };
export type EventRecord = { sequence: number; event_type: string; scenario_run_id: string; state_revision: number; occurred_at: string; payload: Record<string, unknown> };
export type EventPage = { events: EventRecord[]; next_sequence: number; latest_sequence: number; has_more: boolean };
export type Approval = { approval_id: string; operation_id: string; action: string; run_id: string; requested_by: string; target_ids: string[]; payload: Record<string, unknown>; parameters_hash: string; expires_at: string; status: string; decided_by?: string | null; decision_reason?: string | null; decided_at?: string | null; executed_at?: string | null; verified_at?: string | null; created_at: string };
export type HarnessRecommendation = { proposed_change: string; scope: string; approval_required: string; approval_required_yes: boolean; evidence: string; execution_status: string; actionable: boolean; structured: boolean };
export type HarnessResult = { text?: string; status?: string; succeeded?: boolean; recommendation?: HarnessRecommendation; tool_uses?: unknown[]; turn_count?: number; event_count?: number; events?: unknown[]; [key: string]: unknown };
export type EvaluationReport = { status: string; score: number; hard_gates_passed: boolean; metrics: { tool_call_count?: number; mutation_event_count?: number; recovery_event_count?: number; policy_violation_count?: number; [key: string]: unknown }; assertions: Array<{ name: string; passed: boolean; hard_gate: boolean; message: string; evidence: Record<string, unknown> }> };
export type PromptDraft = { name: string; description: string; version: string; updated_at?: string | null; system: string; user: string; model_id: string; max_tokens: number; temperature: number; input_variables: string[] };
export type PromptVersion = { version: string; arn: string; description: string; status?: string; created_at?: string | null; updated_at?: string | null };
export type ManagedPrompt = { key: string; label: string; description: string; arn?: string | null; runtime: boolean; active_version?: string | null; active_arn?: string | null; active_source?: string; default_version?: string | null; draft: PromptDraft; versions: PromptVersion[] };
export type PromptCatalog = { prompts: ManagedPrompt[] };

export const API_BASE = (import.meta.env.VITE_NORTHSTAR_LAMBDA_URL || window.location.origin).replace(/\/$/, "");
const SESSION_KEY = "northstar.cognito.session";

export function loadSession(): Session | null {
  try {
    const raw = window.localStorage.getItem(SESSION_KEY);
    return raw ? (JSON.parse(raw) as Session) : null;
  } catch { return null; }
}

export function saveSession(session: Session | null): void {
  if (session) window.localStorage.setItem(SESSION_KEY, JSON.stringify(session));
  else window.localStorage.removeItem(SESSION_KEY);
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) { super(message); this.name = "ApiError"; this.status = status; }
}

export async function request<T>(path: string, options: RequestInit = {}, session: Session | null = loadSession()): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body && !headers.has("content-type")) headers.set("content-type", "application/json");
  if (session?.id_token) headers.set("authorization", `Bearer ${session.id_token}`);
  const response = await fetch(`${API_BASE}${path}`, { ...options, headers });
  if (!response.ok) {
    const body = await response.text();
    let message = body || response.statusText;
    try { message = (JSON.parse(body) as { detail?: string }).detail || message; } catch { /* text response */ }
    throw new ApiError(response.status, message);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}
