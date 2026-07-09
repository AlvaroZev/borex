import { useCallback, useEffect, useState } from "react";
import {
  RevalidateHistoryRow,
  RevalidateReport,
  fetchRevalidateHistory,
  runRevalidate,
} from "../api/client";

type Props = {
  strategies: { name: string }[];
  paperSessionId?: string;
};

function verdictClass(v: string): string {
  if (v === "healthy") return "health-ok";
  if (v === "warning") return "health-stalled";
  if (v === "decayed") return "health-dead";
  return "muted";
}

export default function RevalidatePanel({ strategies, paperSessionId }: Props) {
  const [strategy, setStrategy] = useState("sma_cross");
  const [symbol, setSymbol] = useState("EURUSD=X");
  const [timeframe, setTimeframe] = useState("1h");
  const [recentMonths, setRecentMonths] = useState(3);
  const [sessionId, setSessionId] = useState(paperSessionId || "");
  const [report, setReport] = useState<RevalidateReport | null>(null);
  const [history, setHistory] = useState<RevalidateHistoryRow[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const loadHistory = useCallback(async () => {
    setHistory(await fetchRevalidateHistory(undefined, undefined, 10));
  }, []);

  useEffect(() => {
    loadHistory().catch(() => {});
  }, [loadHistory]);

  useEffect(() => {
    if (paperSessionId) setSessionId(paperSessionId);
  }, [paperSessionId]);

  async function onRun() {
    setBusy(true);
    setError("");
    try {
      const res = await runRevalidate({
        strategy,
        symbol,
        timeframe,
        recent_months: recentMonths,
        paper_session_id: sessionId || undefined,
      });
      setReport(res);
      await loadHistory();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card revalidate-panel">
      <div className="live-header">
        <h3>Strategy re-validation</h3>
        <span className="muted">Baseline vs recent window decay check</span>
      </div>

      <div className="form-row">
        <select value={strategy} onChange={(e) => setStrategy(e.target.value)}>
          {strategies.map((s) => (
            <option key={s.name} value={s.name}>
              {s.name}
            </option>
          ))}
        </select>
        <input value={symbol} onChange={(e) => setSymbol(e.target.value)} placeholder="Symbol" />
        <select value={timeframe} onChange={(e) => setTimeframe(e.target.value)}>
          {["1h", "4h", "1d", "15m"].map((tf) => (
            <option key={tf} value={tf}>
              {tf}
            </option>
          ))}
        </select>
        <label className="inline-label">
          Recent mo
          <input
            type="number"
            min={1}
            max={24}
            value={recentMonths}
            onChange={(e) => setRecentMonths(Number(e.target.value))}
            style={{ width: "3rem", marginLeft: "0.25rem" }}
          />
        </label>
        <input
          value={sessionId}
          onChange={(e) => setSessionId(e.target.value)}
          placeholder="Paper session (optional)"
          style={{ minWidth: "140px" }}
        />
        <button className="primary" onClick={onRun} disabled={busy}>
          Run revalidate
        </button>
      </div>

      {error && <p className="error">{error}</p>}

      {report && (
        <div style={{ marginTop: "1rem" }}>
          <div className={`health-banner ${verdictClass(report.verdict)}`}>
            <strong>Verdict: {report.verdict}</strong>
          </div>
          <ul className="reason-list">
            {report.reasons.map((r, i) => (
              <li key={i}>{r}</li>
            ))}
          </ul>
          <div className="metrics-grid">
            <div>
              <h4>Baseline</h4>
              <p>Sharpe {report.baseline.metrics.sharpe?.toFixed(2)}</p>
              <p>Return {report.baseline.metrics.total_return_pct?.toFixed(1)}%</p>
              <p className="muted">{report.baseline.period.start?.slice(0, 10)} …</p>
            </div>
            <div>
              <h4>Recent ({recentMonths}mo)</h4>
              <p>Sharpe {report.recent.metrics.sharpe?.toFixed(2)}</p>
              <p>Return {report.recent.metrics.total_return_pct?.toFixed(1)}%</p>
              <p className="muted">{report.recent.period.start?.slice(0, 10)} …</p>
            </div>
            <div>
              <h4>Capital</h4>
              <p>{report.capital.action === "scale_up" ? "Scale up" : "Hold"}</p>
              <p>${report.capital.current_capital} → ${report.capital.recommended_capital}</p>
              {report.capital.blockers?.length > 0 && (
                <p className="muted">{report.capital.blockers.join("; ")}</p>
              )}
            </div>
          </div>
        </div>
      )}

      <h4 style={{ marginTop: "1.25rem" }}>History</h4>
      <table>
        <thead>
          <tr>
            <th>When</th>
            <th>Strategy</th>
            <th>Pair</th>
            <th>Verdict</th>
          </tr>
        </thead>
        <tbody>
          {history.map((h) => (
            <tr key={h.id}>
              <td>{h.created_at.slice(0, 19)}</td>
              <td>{h.strategy}</td>
              <td>
                {h.symbol} {h.timeframe}
              </td>
              <td className={verdictClass(h.verdict)}>{h.verdict}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
