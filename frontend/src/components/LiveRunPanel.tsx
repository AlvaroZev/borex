import { useEffect, useRef, useState } from "react";
import {
  LiveResult,
  RunProgress,
  fetchLeaderboard,
  fetchRun,
  subscribeRun,
} from "../api/client";

type Props = {
  runId: string | null;
  onDone?: () => void;
};

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  if (m < 60) return `${m}m ${s}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

function healthLabel(p: RunProgress): { text: string; className: string } {
  if (p.is_dead || p.health === "dead") {
    return { text: "Worker died — run crashed", className: "health-dead" };
  }
  if (p.is_stalled || p.health === "stalled") {
    return {
      text: `No heartbeat ${formatDuration(p.seconds_since_activity ?? 0)} — large download or stuck`,
      className: "health-stalled",
    };
  }
  if (p.current_job) {
    const y = p.current_job.year ? ` ${p.current_job.year}` : "";
    return {
      text: `Downloading ${p.current_job.symbol} ${p.current_job.timeframe}${y}`,
      className: "health-working",
    };
  }
  if (p.status === "running") {
    return { text: "Running", className: "health-ok" };
  }
  return { text: p.status, className: "muted" };
}

export default function LiveRunPanel({ runId, onDone }: Props) {
  const [progress, setProgress] = useState<RunProgress | null>(null);
  const [feed, setFeed] = useState<LiveResult[]>([]);
  const [liveBoard, setLiveBoard] = useState<
    { key: string; strategy: string; symbol: string; timeframe: string; sharpe: number; ret: number }[]
  >([]);
  const [tick, setTick] = useState(0);
  const doneRef = useRef(false);
  const lastActivityRef = useRef<number>(Date.now());

  useEffect(() => {
    if (!runId) return;
    doneRef.current = false;
    setFeed([]);
    setLiveBoard([]);
    setProgress(null);
    lastActivityRef.current = Date.now();

    const applyProgress = (p: RunProgress) => {
      setProgress(p);
      if (p.seconds_since_activity !== undefined && p.seconds_since_activity < 15) {
        lastActivityRef.current = Date.now();
      }
    };

    const es = subscribeRun(runId, (data) => {
      const p = data.progress as RunProgress | undefined;
      if (p) applyProgress(p);

      if (data.type === "result" && data.result) {
        const result = data.result as LiveResult;
        setFeed((prev) => [result, ...prev].slice(0, 40));

        if (result.metrics && result.strategy) {
          const key = `${result.strategy}-${result.symbol}-${result.timeframe}`;
          const sharpe = Number(result.metrics.sharpe ?? 0);
          const ret = Number(result.metrics.total_return_pct ?? 0);
          setLiveBoard((prev) => {
            const next = [...prev.filter((r) => r.key !== key), {
              key,
              strategy: result.strategy!,
              symbol: result.symbol ?? "",
              timeframe: result.timeframe ?? "",
              sharpe,
              ret,
            }];
            return next.sort((a, b) => b.sharpe - a.sharpe).slice(0, 15);
          });
        }
      }

      if ((data.type === "done" || data.type === "error") && !doneRef.current) {
        doneRef.current = true;
        es.close();
        if (p?.run_group) {
          fetchLeaderboard("sharpe", p.run_group).then(setLiveBoardFromDb).catch(() => {});
        }
        onDone?.();
      }
    });

    const poll = setInterval(() => {
      fetchRun(runId)
        .then(applyProgress)
        .catch(() => {});
      setTick((t) => t + 1);
    }, 5000);

    const clock = setInterval(() => setTick((t) => t + 1), 1000);

    return () => {
      es.close();
      clearInterval(poll);
      clearInterval(clock);
    };
  }, [runId, onDone]);

  function setLiveBoardFromDb(rows: { strategy: string; symbol: string; timeframe: string; metrics: Record<string, number> }[]) {
    setLiveBoard(
      rows.map((r) => ({
        key: `${r.strategy}-${r.symbol}-${r.timeframe}`,
        strategy: r.strategy,
        symbol: r.symbol,
        timeframe: r.timeframe,
        sharpe: Number(r.metrics.sharpe ?? 0),
        ret: Number(r.metrics.total_return_pct ?? 0),
      }))
    );
  }

  if (!runId || !progress) return null;

  const running = progress.status === "running" || progress.status === "pending";
  const health = healthLabel(progress);
  const idleSec =
    progress.seconds_since_activity ??
    (running ? (Date.now() - lastActivityRef.current) / 1000 : 0);

  return (
    <div className="card live-panel">
      <div className="live-header">
        <h3>
          Live{" "}
          {progress.kind === "mass"
            ? "backtest"
            : progress.kind === "dukascopy"
              ? "Dukascopy download"
              : "download"}
          {running && !progress.is_dead && <span className="pulse"> ● active</span>}
        </h3>
        <span className="muted">
          {progress.completed}/{progress.total}
          {progress.failed > 0 && ` (${progress.failed} failed)`}
        </span>
      </div>

      <div className={`health-banner ${health.className}`}>
        <strong>{health.text}</strong>
        {progress.current_job && (
          <span className="muted">
            {" "}
            — started {formatDuration(
              (Date.now() - new Date(progress.current_job.started_at).getTime()) / 1000
            )}{" "}
            ago
            {progress.current_job.year ? ` (year ${progress.current_job.year})` : ""}
          </span>
        )}
      </div>

      <div className="status-row">
        <span className={progress.worker_alive === false ? "health-dead" : "health-ok"}>
          Worker: {progress.worker_alive === false ? "dead" : "alive"}
        </span>
        <span className="muted">Elapsed: {formatDuration(progress.elapsed_seconds ?? 0)}</span>
        <span className={idleSec > 60 ? "health-stalled" : "muted"}>
          Last signal: {formatDuration(idleSec)} ago
        </span>
        <span className="muted">Poll + SSE {tick > 0 ? "✓" : ""}</span>
      </div>

      <div className="progress-track">
        <div className="progress-fill" style={{ width: `${progress.pct}%` }} />
      </div>
      <p className="muted" style={{ marginTop: "0.35rem" }}>
        {progress.pct.toFixed(1)}% — run {progress.run_id}
        {progress.is_stalled && " — still may be downloading a large file (30m/15m/1m take long)"}
      </p>

      <div className="live-grid">
        <div>
          <h4>Live feed</h4>
          <div className="feed">
            {feed.length === 0 && <p className="muted">Waiting for first result…</p>}
            {feed.map((row, i) => (
              <div
                key={i}
                className={`feed-row ${
                  row.error || row.status === "error"
                    ? "fail"
                    : row.status === "skipped"
                      ? "skip"
                      : "ok"
                }`}
              >
                {row.strategy ? (
                  <>
                    <span>{row.strategy}</span>
                    <span className="muted">{row.symbol} {row.timeframe}</span>
                    {row.metrics ? (
                      <span className={Number(row.metrics.total_return_pct) >= 0 ? "positive" : "negative"}>
                        {Number(row.metrics.total_return_pct).toFixed(1)}%
                      </span>
                    ) : (
                      <span className="error">{row.error}</span>
                    )}
                  </>
                ) : (
                  <>
                    <span>{row.symbol}</span>
                    <span className="muted">
                      {row.timeframe}
                      {(row as { year?: number }).year ? ` ${(row as { year?: number }).year}` : ""}
                    </span>
                    <span>
                      {row.status === "ok"
                        ? `${row.bars ?? ""} bars`
                        : row.status === "skipped"
                          ? "skipped"
                          : row.error}
                    </span>
                  </>
                )}
              </div>
            ))}
          </div>
        </div>

        <div>
          <h4>Top so far (Sharpe)</h4>
          {liveBoard.length === 0 ? (
            <p className="muted">Backtest results appear here during mass runs.</p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Strategy</th>
                  <th>Pair</th>
                  <th>TF</th>
                  <th>Sharpe</th>
                  <th>Ret %</th>
                </tr>
              </thead>
              <tbody>
                {liveBoard.map((row) => (
                  <tr key={row.key}>
                    <td>{row.strategy}</td>
                    <td>{row.symbol}</td>
                    <td>{row.timeframe}</td>
                    <td>{row.sharpe.toFixed(2)}</td>
                    <td className={row.ret >= 0 ? "positive" : "negative"}>{row.ret.toFixed(1)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
}
