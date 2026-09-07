from __future__ import annotations

from typing import Any, Iterable, Mapping

from borex.models.candle import Signal, SignalAction


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def signal_from_decision(
    row: Mapping[str, Any],
    *,
    default_score: float = 0.0,
) -> Signal:
    """Rebuild a Signal from a saved analysis decision row."""
    action_raw = str(row.get("action") or "hold").strip().lower()
    try:
        action = SignalAction(action_raw)
    except ValueError:
        action = SignalAction.HOLD

    ts = row.get("time_unix")
    if ts is not None and ts != "":
        import pandas as pd

        timestamp: object = pd.Timestamp(int(float(ts)), unit="s", tz="UTC")
    else:
        timestamp = row.get("time") or row.get("timestamp")

    score_raw = row.get("score")
    score = float(score_raw) if score_raw not in (None, "") else float(default_score)

    return Signal(
        action=action,
        pattern=str(row.get("pattern") or ""),
        index=int(float(row["index"])),
        price=float(row.get("price") or 0.0),
        timestamp=timestamp,
        stop_loss=_float_or_none(row.get("stop_loss")),
        take_profit=_float_or_none(row.get("take_profit")),
        score=score,
    )


def index_decisions_by_bar(
    decisions: Iterable[Mapping[str, Any]],
    *,
    default_score: float = 0.0,
) -> dict[tuple[str, int], list[Signal]]:
    """
    Map (symbol, candle_index) → signals that fired on that bar.

    Matches MultiMarketEngine's live scan keying (ctx.indices[sym] == signal.index).
    """
    out: dict[tuple[str, int], list[Signal]] = {}
    for row in decisions:
        sym = str(row.get("symbol") or "")
        if not sym:
            continue
        signal = signal_from_decision(row, default_score=default_score)
        if signal.action == SignalAction.HOLD:
            continue
        key = (sym, signal.index)
        out.setdefault(key, []).append(signal)
    return out
