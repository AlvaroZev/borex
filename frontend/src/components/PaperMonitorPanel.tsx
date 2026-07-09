import { useCallback, useEffect, useState } from "react";
import {
  AlertConfig,
  AlertRow,
  DecisionRow,
  MonitorStatus,
  PaperSession,
  createPaperSession,
  fetchAlertConfig,
  fetchPaperAlerts,
  fetchPaperDecisions,
  fetchPaperMonitor,
  fetchPaperSessions,
  killPaperSession,
  resumePaperSession,
  saveAlertConfig,
  sendAlertTest,
  tickPaperSession,
} from "../api/client";

type Props = {
  strategies: { name: string }[];
};

function healthClass(health: string): string {
  if (health === "killed" || health === "dead" || health === "liquidated") return "health-dead";
  if (health === "stale_data" || health === "divergence_warn" || health === "stalled") return "health-stalled";
  if (health === "ok" || health === "running" || health === "working") return "health-ok";
  return "muted";
}

export default function PaperMonitorPanel({ strategies }: Props) {
  const [sessions, setSessions] = useState<PaperSession[]>([]);
  const [selectedId, setSelectedId] = useState<string>("");
  const [monitor, setMonitor] = useState<MonitorStatus | null>(null);
  const [alerts, setAlerts] = useState<AlertRow[]>([]);
  const [decisions, setDecisions] = useState<DecisionRow[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const [alertCfg, setAlertCfg] = useState<AlertConfig | null>(null);
  const [webhookUrl, setWebhookUrl] = useState("");
  const [slackUrl, setSlackUrl] = useState("");
  const [alertEnabled, setAlertEnabled] = useState(false);
  const [testResult, setTestResult] = useState("");

  const [newStrategy, setNewStrategy] = useState("sma_cross");
  const [newSymbol, setNewSymbol] = useState("EURUSD=X");
  const [newTf, setNewTf] = useState("1h");

  const loadSessions = useCallback(async () => {
    const rows = await fetchPaperSessions();
    setSessions(rows);
    if (rows.length && !selectedId) setSelectedId(rows[0].id);
  }, [selectedId]);

  const loadMonitor = useCallback(async (id: string) => {
    if (!id) return;
    const [mon, al, dec] = await Promise.all([
      fetchPaperMonitor(id),
      fetchPaperAlerts(id),
      fetchPaperDecisions(id),
    ]);
    setMonitor(mon);
    setAlerts(al);
    setDecisions(dec);
  }, []);

  const loadAlertSettings = useCallback(async () => {
    const cfg = await fetchAlertConfig();
    setAlertCfg(cfg);
    setWebhookUrl(cfg.webhook_url || "");
    setSlackUrl(cfg.slack_webhook_url || "");
    setAlertEnabled(cfg.enabled);
  }, []);

  useEffect(() => {
    loadSessions().catch((e) => setError(String(e)));
    loadAlertSettings().catch(() => {});
  }, [loadSessions, loadAlertSettings]);

  useEffect(() => {
    if (!selectedId) return;
    loadMonitor(selectedId).catch((e) => setError(String(e)));
    const t = setInterval(() => loadMonitor(selectedId).catch(() => {}), 15000);
    return () => clearInterval(t);
  }, [selectedId, loadMonitor]);

  async function onTick() {
    if (!selectedId) return;
    setBusy(true);
    setError("");
    try {
      await tickPaperSession(selectedId);
      await loadMonitor(selectedId);
      await loadSessions();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function onKill() {
    if (!selectedId) return;
    setBusy(true);
    try {
      await killPaperSession(selectedId);
      await loadMonitor(selectedId);
      await loadSessions();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function onResume() {
    if (!selectedId) return;
    setBusy(true);
    try {
      await resumePaperSession(selectedId);
      await loadMonitor(selectedId);
      await loadSessions();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function onSaveAlerts() {
    setBusy(true);
    setTestResult("");
    try {
      await saveAlertConfig({
        enabled: alertEnabled,
        webhook_url: webhookUrl,
        slack_webhook_url: slackUrl,
        min_severity: alertCfg?.min_severity || "warn",
      });
      await loadAlertSettings();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function onTestAlert() {
    setBusy(true);
    setTestResult("");
    try {
      const res = await sendAlertTest(selectedId || "test");
      setTestResult(res.sent ? "Test alert sent" : `Not sent: ${res.reason || JSON.stringify(res)}`);
    } catch (e) {
      setTestResult(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function onCreateSession() {
    setBusy(true);
    setError("");
    try {
      const s = await createPaperSession(newStrategy, newSymbol, newTf);
      await loadSessions();
      setSelectedId(s.session_id);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card paper-panel">
      <div className="live-header">
        <h3>Paper trading monitor</h3>
        <span className="muted">{sessions.length} session(s)</span>
      </div>

      <div className="form-row">
        <select value={selectedId} onChange={(e) => setSelectedId(e.target.value)}>
          <option value="">Select session…</option>
          {sessions.map((s) => (
            <option key={s.id} value={s.id}>
              {s.id.slice(0, 8)} — {s.strategy} {s.symbol} {s.timeframe} ({s.status})
            </option>
          ))}
        </select>
        <button onClick={onTick} disabled={!selectedId || busy}>
          Tick
        </button>
        <button onClick={onKill} disabled={!selectedId || busy}>
          Kill
        </button>
        <button onClick={onResume} disabled={!selectedId || busy}>
          Resume
        </button>
      </div>

      <div className="form-row muted" style={{ fontSize: "0.85rem", marginTop: "0.5rem" }}>
        <select value={newStrategy} onChange={(e) => setNewStrategy(e.target.value)}>
          {strategies.map((s) => (
            <option key={s.name} value={s.name}>
              {s.name}
            </option>
          ))}
        </select>
        <input value={newSymbol} onChange={(e) => setNewSymbol(e.target.value)} placeholder="Symbol" />
        <select value={newTf} onChange={(e) => setNewTf(e.target.value)}>
          {["1h", "4h", "1d", "15m"].map((tf) => (
            <option key={tf} value={tf}>
              {tf}
            </option>
          ))}
        </select>
        <button onClick={onCreateSession} disabled={busy}>
          New session
        </button>
      </div>

      {monitor && (
        <>
          <div className={`health-banner ${healthClass(monitor.health)}`}>
            <strong>Health: {monitor.health}</strong>
            {monitor.killed && <span> — kill-switch: {monitor.kill_reason}</span>}
            <span className="muted">
              {" "}
              · equity ${monitor.equity} · {monitor.open_positions} open
            </span>
          </div>
          <div className="status-row">
            <span>Return delta: {monitor.divergence?.return_delta_pct?.toFixed(1)}%</span>
            <span>Baseline Sharpe: {monitor.baseline_metrics?.sharpe?.toFixed(2)}</span>
            <span>Data age: {monitor.data_age_minutes ?? "—"} min</span>
          </div>
        </>
      )}

      <div className="live-grid">
        <div>
          <h4>Alerts</h4>
          <div className="feed">
            {alerts.length === 0 && <p className="muted">No alerts yet</p>}
            {alerts.map((a) => (
              <div key={a.id} className={`feed-row ${a.severity === "critical" ? "fail" : "skip"}`}>
                <span>{a.code}</span>
                <span className="muted">{a.message.slice(0, 40)}</span>
                <span>{a.severity}</span>
              </div>
            ))}
          </div>
        </div>
        <div>
          <h4>Decision log</h4>
          <div className="feed">
            {decisions.length === 0 && <p className="muted">No decisions yet</p>}
            {decisions.map((d) => (
              <div key={d.id} className="feed-row ok">
                <span>{d.event_type}</span>
                <span className="muted">{d.reason || d.action}</span>
                <span>{d.bar_index ?? "—"}</span>
              </div>
            ))}
          </div>
        </div>
      </div>

      <div className="alert-config" style={{ marginTop: "1rem" }}>
        <h4>Alert delivery (webhook / Slack)</h4>
        <p className="muted" style={{ fontSize: "0.85rem" }}>
          New alerts are POSTed to these URLs. Set{" "}
          <code>BOREX_WEBHOOK_URL</code> or <code>BOREX_SLACK_WEBHOOK_URL</code> env to override.
        </p>
        {alertCfg?.env_webhook && <p className="health-stalled">Env BOREX_WEBHOOK_URL active</p>}
        <label className="checkbox-row">
          <input type="checkbox" checked={alertEnabled} onChange={(e) => setAlertEnabled(e.target.checked)} />
          Enable outbound alerts
        </label>
        <input
          className="text-input"
          placeholder="Generic webhook URL (JSON POST)"
          value={webhookUrl}
          onChange={(e) => setWebhookUrl(e.target.value)}
        />
        <input
          className="text-input"
          placeholder="Slack incoming webhook URL"
          value={slackUrl}
          onChange={(e) => setSlackUrl(e.target.value)}
        />
        <div className="form-row">
          <button onClick={onSaveAlerts} disabled={busy}>
            Save alert config
          </button>
          <button onClick={onTestAlert} disabled={busy}>
            Send test alert
          </button>
        </div>
        {testResult && <p className="muted">{testResult}</p>}
      </div>
    </div>
  );
}
