import { useCallback, useEffect, useState } from "react";
import {
  ScreenHistoryRow,
  ScreenReport,
  fetchScreenHistory,
  runScreen,
} from "../api/client";

type Props = {
  strategies: { name: string }[];
};

export default function ScreenPanel({ strategies }: Props) {
  const [strategy, setStrategy] = useState("sma_cross");
  const [symbol, setSymbol] = useState("EURUSD=X");
  const [minSharpe, setMinSharpe] = useState(0.5);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [report, setReport] = useState<ScreenReport | null>(null);
  const [history, setHistory] = useState<ScreenHistoryRow[]>([]);

  const loadHistory = useCallback(async () => {
    setHistory(await fetchScreenHistory(10));
  }, []);

  useEffect(() => {
    loadHistory();
  }, [loadHistory]);

  useEffect(() => {
    if (strategies.length && !strategies.some((s) => s.name === strategy)) {
      setStrategy(strategies[0].name);
    }
  }, [strategies, strategy]);

  async function onRun() {
    setBusy(true);
    setError("");
    setReport(null);
    try {
      const res = await runScreen({
        strategies: [strategy],
        symbols: [symbol],
        timeframes: ["1h"],
        workers: 2,
        max_combos: 8,
        max_points: 3,
        min_oos_sharpe: minSharpe,
        save: true,
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
    <div className="card screen-panel" style={{ marginBottom: "1rem" }}>
      <h3>Strategy screen</h3>
      <p className="muted">
        Sweep + rolling walk-forward; promote configs passing OOS gates
      </p>

      <div className="form-row">
        <label>
          Strategy
          <select value={strategy} onChange={(e) => setStrategy(e.target.value)}>
            {strategies.map((s) => (
              <option key={s.name} value={s.name}>
                {s.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Symbol
          <input value={symbol} onChange={(e) => setSymbol(e.target.value)} />
        </label>
        <label>
          Min OOS Sharpe
          <input
            type="number"
            step="0.1"
            value={minSharpe}
            onChange={(e) => setMinSharpe(Number(e.target.value))}
          />
        </label>
        <button className="primary" onClick={onRun} disabled={busy}>
          {busy ? "Running…" : "Run screen"}
        </button>
      </div>

      {error && <p className="error">{error}</p>}

      {report && (
        <div className="screen-results">
          <p>
            <strong>{report.promoted_count}</strong> promoted / {report.total} jobs
            {report.run_id && (
              <span className="muted"> · {report.run_id}</span>
            )}
          </p>
          {report.promoted.length > 0 ? (
            <table>
              <thead>
                <tr>
                  <th>Strategy</th>
                  <th>Symbol</th>
                  <th>TF</th>
                  <th>OOS Sharpe</th>
                  <th>Params</th>
                </tr>
              </thead>
              <tbody>
                {report.promoted.map((c) => (
                  <tr key={`${c.strategy}-${c.symbol}-${c.timeframe}`}>
                    <td>{c.strategy}</td>
                    <td>{c.symbol}</td>
                    <td>{c.timeframe}</td>
                    <td>{c.oos_metrics.avg_oos_sharpe?.toFixed(2) ?? "—"}</td>
                    <td className="muted">{JSON.stringify(c.best_params)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p className="muted">No configs passed gates.</p>
          )}
        </div>
      )}

      {history.length > 0 && (
        <>
          <h4 style={{ marginTop: "1rem" }}>Recent runs</h4>
          <table>
            <thead>
              <tr>
                <th>Run</th>
                <th>When</th>
                <th>Promoted</th>
                <th>Total</th>
              </tr>
            </thead>
            <tbody>
              {history.map((h) => (
                <tr key={h.id}>
                  <td className="muted">{h.id.slice(-12)}</td>
                  <td>{new Date(h.created_at).toLocaleString()}</td>
                  <td>{h.promoted_count}</td>
                  <td>{h.total}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}
