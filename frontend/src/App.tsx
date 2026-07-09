import { useCallback, useEffect, useState } from "react";
import {
  fetchCache,
  fetchHealth,
  fetchLeaderboard,
  fetchStrategies,
  startDownload,
  startDukascopyDownload,
  startMass,
} from "./api/client";
import LiveRunPanel from "./components/LiveRunPanel";
import PaperMonitorPanel from "./components/PaperMonitorPanel";
import RevalidatePanel from "./components/RevalidatePanel";
import ScreenPanel from "./components/ScreenPanel";

type CacheRow = {
  symbol: string;
  timeframe: string;
  bars: number;
  start: string;
  end: string;
};

type LeaderRow = {
  id: number;
  strategy: string;
  symbol: string;
  timeframe: string;
  metrics: Record<string, number | boolean>;
};

export default function App() {
  const [health, setHealth] = useState<string>("...");
  const [cache, setCache] = useState<CacheRow[]>([]);
  const [strategies, setStrategies] = useState<{ name: string }[]>([]);
  const [leaderboard, setLeaderboard] = useState<LeaderRow[]>([]);
  const [activeTab, setActiveTab] = useState<"research" | "paper">("research");
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    setError("");
    try {
      const h = await fetchHealth();
      setHealth(`${h.status} v${h.version}`);
      setCache(await fetchCache());
      setStrategies(await fetchStrategies());
      setLeaderboard(await fetchLeaderboard());
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function onDownload() {
    setBusy(true);
    setError("");
    try {
      const res = await startDownload(false);
      setActiveRunId(res.run_id);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function onMass() {
    setBusy(true);
    setError("");
    try {
      const res = await startMass(8);
      setActiveRunId(res.run_id);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function onDukascopy() {
    setBusy(true);
    setError("");
    try {
      const res = await startDukascopyDownload();
      setActiveRunId(res.run_id);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="layout">
      <header className="header">
        <div>
          <h1>Borex</h1>
          <p className="muted">Forex backtesting — watch mass runs live via SSE</p>
        </div>
        <div className="muted">API: {health}</div>
      </header>

      {error && <p className="error">{error}</p>}

      <nav className="tab-nav">
        <button
          type="button"
          className={activeTab === "research" ? "tab active" : "tab"}
          onClick={() => setActiveTab("research")}
        >
          Research
        </button>
        <button
          type="button"
          className={activeTab === "paper" ? "tab active" : "tab"}
          onClick={() => setActiveTab("paper")}
        >
          Paper &amp; revalidate
        </button>
      </nav>

      {activeTab === "paper" && (
        <>
          <PaperMonitorPanel strategies={strategies} />
          <RevalidatePanel strategies={strategies} />
        </>
      )}

      {activeTab === "research" && (
        <>
      <div className="grid" style={{ marginBottom: "1rem" }}>
        <div className="card">
          <h3>Dukascopy (2020 → 2026)</h3>
          <p className="muted">
            10 forex pairs × 15m, 1h, 4h, 1wk — via dukascopy-node
          </p>
          <p className="muted" style={{ fontSize: "0.8rem" }}>
            Yearly chunks · skips cached data · ~1000+ steps with progress
          </p>
          <button className="primary" onClick={onDukascopy} disabled={busy || !!activeRunId}>
            Download all Dukascopy data
          </button>
        </div>
        <div className="card">
          <h3>Yahoo (quick)</h3>
          <p className="muted">{cache.length} cached datasets</p>
          <button onClick={onDownload} disabled={busy || !!activeRunId}>
            Download Yahoo (short history)
          </button>
        </div>
        <div className="card">
          <h3>Mass backtest</h3>
          <p className="muted">
            {strategies.length} strategies × all symbols × all timeframes
          </p>
          <button onClick={onMass} disabled={busy || !!activeRunId}>
            Run mass backtest (8 workers)
          </button>
        </div>
      </div>

      <ScreenPanel strategies={strategies} />

      <LiveRunPanel
        runId={activeRunId}
        onDone={() => {
          setActiveRunId(null);
          refresh();
        }}
      />

      <div className="card" style={{ marginBottom: "1rem" }}>
        <h3>Leaderboard (Sharpe)</h3>
        <table>
          <thead>
            <tr>
              <th>Strategy</th>
              <th>Symbol</th>
              <th>TF</th>
              <th>Sharpe</th>
              <th>Return %</th>
              <th>Max DD %</th>
              <th>Trades</th>
            </tr>
          </thead>
          <tbody>
            {leaderboard.map((row) => (
              <tr key={row.id}>
                <td>{row.strategy}</td>
                <td>{row.symbol}</td>
                <td>{row.timeframe}</td>
                <td>{Number(row.metrics.sharpe).toFixed(2)}</td>
                <td className={Number(row.metrics.total_return_pct) >= 0 ? "positive" : "negative"}>
                  {Number(row.metrics.total_return_pct).toFixed(2)}
                </td>
                <td>{Number(row.metrics.max_drawdown_pct).toFixed(2)}</td>
                <td>{row.metrics.trades as number}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="card">
        <h3>Cached data</h3>
        <table>
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Timeframe</th>
              <th>Bars</th>
              <th>Start</th>
              <th>End</th>
            </tr>
          </thead>
          <tbody>
            {cache.map((row) => (
              <tr key={`${row.symbol}-${row.timeframe}`}>
                <td>{row.symbol}</td>
                <td>{row.timeframe}</td>
                <td>{row.bars}</td>
                <td>{row.start.slice(0, 19)}</td>
                <td>{row.end.slice(0, 19)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
        </>
      )}
    </div>
  );
}
