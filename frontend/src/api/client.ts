const API = "/api";

export type RunProgress = {
  run_id: string;
  kind: string;
  status: string;
  total: number;
  completed: number;
  failed: number;
  pct: number;
  run_group: string | null;
  recent: LiveResult[];
  current_job?: { symbol: string; timeframe: string; year?: number | null; started_at: string } | null;
  last_activity_at?: string;
  seconds_since_activity?: number;
  elapsed_seconds?: number;
  worker_alive?: boolean;
  health?: "ok" | "working" | "running" | "stalled" | "dead";
  is_stalled?: boolean;
  is_dead?: boolean;
  error?: string;
};

export type LiveResult = {
  strategy?: string;
  symbol?: string;
  timeframe?: string;
  status?: string;
  error?: string;
  metrics?: Record<string, number | boolean>;
  bars?: number;
};

export async function fetchHealth() {
  const r = await fetch(`${API}/health`);
  return r.json();
}

export async function fetchCache() {
  const r = await fetch(`${API}/data/cache`);
  return r.json();
}

export async function startDukascopyDownload(
  start = "2020-01-01",
  end = "2026-12-31",
  timeframes = ["15m", "1h", "4h", "1wk"]
) {
  const r = await fetch(`${API}/data/download/dukascopy`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ start, end, timeframes }),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function startDownload(force = false) {
  const r = await fetch(`${API}/data/download`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ all: true, force }),
  });
  return r.json();
}

export async function fetchStrategies() {
  const r = await fetch(`${API}/strategies`);
  return r.json();
}

export async function fetchLeaderboard(metric = "sharpe", runGroup?: string) {
  const q = new URLSearchParams({ metric, limit: "50" });
  if (runGroup) q.set("run_group", runGroup);
  const r = await fetch(`${API}/leaderboard?${q}`);
  return r.json();
}

export async function startMass(workers = 8) {
  const r = await fetch(`${API}/mass/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ workers }),
  });
  return r.json();
}

export async function fetchRun(runId: string): Promise<RunProgress> {
  const r = await fetch(`${API}/runs/${runId}`);
  return r.json();
}

export function subscribeRun(
  runId: string,
  onEvent: (data: Record<string, unknown>) => void,
  onError?: (err: Event) => void
): EventSource {
  const es = new EventSource(`${API}/runs/${runId}/stream`);
  es.onmessage = (msg) => {
    try {
      onEvent(JSON.parse(msg.data));
    } catch {
      /* ignore parse errors */
    }
  };
  es.onerror = (ev) => {
    onError?.(ev);
  };
  return es;
}

// --- Paper / monitor (Phase 5) ---

export type PaperSession = {
  id: string;
  strategy: string;
  symbol: string;
  timeframe: string;
  status: string;
  last_bar_ts: string | null;
  updated_at: string;
  killed?: boolean;
  kill_reason?: string;
};

export type MonitorStatus = {
  session_id: string;
  strategy: string;
  symbol: string;
  timeframe: string;
  status: string;
  health: string;
  killed: boolean;
  kill_reason: string;
  equity: number;
  open_positions: number;
  data_age_minutes?: number;
  baseline_metrics: Record<string, number>;
  divergence: Record<string, number>;
};

export type AlertRow = {
  id: number;
  session_id: string;
  created_at: string;
  severity: string;
  code: string;
  message: string;
  detail: Record<string, unknown>;
};

export type DecisionRow = {
  id: number;
  session_id: string;
  created_at: string;
  event_type: string;
  action: string;
  reason: string;
  bar_index: number | null;
  detail: Record<string, unknown>;
};

export type AlertConfig = {
  enabled: boolean;
  webhook_url: string;
  slack_webhook_url: string;
  min_severity: string;
  env_webhook?: boolean;
  env_slack?: boolean;
};

export async function fetchPaperSessions(limit = 20): Promise<PaperSession[]> {
  const r = await fetch(`${API}/paper/sessions?limit=${limit}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function createPaperSession(strategy: string, symbol: string, timeframe: string) {
  const r = await fetch(`${API}/paper/sessions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ strategy, symbol, timeframe }),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json() as Promise<{ session_id: string }>;
}

export async function tickPaperSession(sessionId: string) {
  const r = await fetch(`${API}/paper/sessions/${sessionId}/tick`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_data: true }),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function fetchPaperMonitor(sessionId: string): Promise<MonitorStatus> {
  const r = await fetch(`${API}/paper/sessions/${sessionId}/monitor`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function fetchPaperAlerts(sessionId: string, limit = 30): Promise<AlertRow[]> {
  const r = await fetch(`${API}/paper/sessions/${sessionId}/alerts?limit=${limit}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function fetchPaperDecisions(sessionId: string, limit = 40): Promise<DecisionRow[]> {
  const r = await fetch(`${API}/paper/sessions/${sessionId}/decisions?limit=${limit}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function killPaperSession(sessionId: string, reason = "manual") {
  const r = await fetch(`${API}/paper/sessions/${sessionId}/kill`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reason }),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function resumePaperSession(sessionId: string) {
  const r = await fetch(`${API}/paper/sessions/${sessionId}/resume`, { method: "POST" });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function fetchAlertConfig(): Promise<AlertConfig> {
  const r = await fetch(`${API}/alerts/config`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function saveAlertConfig(cfg: Partial<AlertConfig>) {
  const r = await fetch(`${API}/alerts/config`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(cfg),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function sendAlertTest(sessionId = "test") {
  const r = await fetch(`${API}/alerts/test`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId }),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json() as Promise<{ sent: boolean; reason?: string; targets?: unknown[] }>;
}

// --- Revalidation (Phase 6) ---

export type RevalidateReport = {
  verdict: string;
  reasons: string[];
  baseline: { metrics: Record<string, number>; period: Record<string, string> };
  recent: { metrics: Record<string, number>; period: Record<string, string> };
  deltas: Record<string, number>;
  capital: {
    action: string;
    current_capital: number;
    recommended_capital: number;
    blockers: string[];
  };
};

export type RevalidateHistoryRow = {
  id: string;
  created_at: string;
  strategy: string;
  symbol: string;
  timeframe: string;
  verdict: string;
  paper_session_id: string | null;
};

export async function runRevalidate(body: {
  strategy: string;
  symbol: string;
  timeframe: string;
  recent_months?: number;
  paper_session_id?: string;
}) {
  const r = await fetch(`${API}/revalidate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...body, save: true }),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json() as Promise<RevalidateReport>;
}

export async function fetchRevalidateHistory(
  strategy?: string,
  symbol?: string,
  limit = 20
): Promise<RevalidateHistoryRow[]> {
  const q = new URLSearchParams({ limit: String(limit) });
  if (strategy) q.set("strategy", strategy);
  if (symbol) q.set("symbol", symbol);
  const r = await fetch(`${API}/revalidate?${q}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

// --- Screen pipeline ---

export type ScreenCandidate = {
  strategy: string;
  symbol: string;
  timeframe: string;
  passed: boolean;
  best_params: Record<string, unknown>;
  oos_metrics: Record<string, number>;
  gate_reasons: string[];
  rank_score: number;
  paper_session_id?: string;
};

export type ScreenReport = {
  run_id: string;
  total: number;
  promoted_count: number;
  rejected_count: number;
  error_count: number;
  promoted: ScreenCandidate[];
  rejected: ScreenCandidate[];
  errors: ScreenCandidate[];
};

export type ScreenHistoryRow = {
  id: string;
  created_at: string;
  promoted_count: number;
  total: number;
};

export async function runScreen(body: {
  strategies?: string[];
  symbols?: string[];
  timeframes?: string[];
  workers?: number;
  max_combos?: number;
  max_points?: number;
  min_oos_sharpe?: number;
  create_paper?: boolean;
  save?: boolean;
}): Promise<ScreenReport> {
  const r = await fetch(`${API}/screen`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function fetchScreenHistory(limit = 20): Promise<ScreenHistoryRow[]> {
  const r = await fetch(`${API}/screen?limit=${limit}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}
