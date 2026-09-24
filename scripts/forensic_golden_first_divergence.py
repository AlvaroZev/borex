"""First-divergence forensic for a golden fixture."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT.parent / "borex_live"
sys.path[:0] = [str(ROOT), str(LIVE), str(ROOT / "scripts")]

from borex.alexg.multi_market import align_symbols_to_timeline
from borex_live.engine.state_checksum import canonicalize_shadow_state, state_hash
from replay_golden_fixture_twice import _load_fixture_candles, _restore_shadow


def walk(a, b, path=""):
    diffs = []
    if type(a) != type(b):
        return [(path, a, b)]
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                diffs.append((path + "." + k, None, b[k]))
            elif k not in b:
                diffs.append((path + "." + k, a[k], None))
            else:
                diffs.extend(walk(a[k], b[k], path + "." + k))
        return diffs
    if isinstance(a, list):
        if len(a) != len(b):
            return [(path + ".len", len(a), len(b))]
        for i, (x, y) in enumerate(zip(a, b)):
            diffs.extend(walk(x, y, path + f"[{i}]"))
        return diffs
    if a != b:
        return [(path, a, b)]
    return []


def main() -> None:
    fix = Path(sys.argv[1])
    traces = sorted((fix / "state_trace").glob("*.json"))
    live0 = json.loads(traces[0].read_text(encoding="utf-8"))
    live1 = json.loads(traces[1].read_text(encoding="utf-8"))

    shadow = _restore_shadow(fix)
    master_symbol, series, steps = _load_fixture_candles(fix)

    # step 0
    mi = steps[0]["master_index"]
    candles = {
        sym: (bars[: mi + 1] if len(bars) > mi else list(bars))
        for sym, bars in series.items()
        if bars
    }
    shadow.step_master_bar(mi, candles, master_symbol)
    assert state_hash(shadow) == live0["STATE_AFTER_hash"]

    # inspect AUDNZD alignment at step 1
    mi = steps[1]["master_index"]
    candles = {
        sym: (bars[: mi + 1] if len(bars) > mi else list(bars))
        for sym, bars in series.items()
        if bars
    }
    master = candles[master_symbol]
    ts = master[mi].timestamp
    ts_maps = align_symbols_to_timeline(master, candles)
    indices = {s: m[ts] for s, m in ts_maps.items() if ts in m}
    print("step1 AUDNZD in indices", "AUDNZD=X" in indices, indices.get("AUDNZD=X"))
    print("AUDNZD len", len(candles.get("AUDNZD=X") or []))
    for p in sorted((fix / "h1_input").iterdir()):
        if not p.is_dir() or p.name in {"warmup_series", "full_series"}:
            continue
        b = json.loads((p / "_bundle.json").read_text(encoding="utf-8"))
        if b["sequence"] != 1:
            continue
        rows = [r for r in b["bars"] if r["pair"] == "AUDNZD=X"]
        print("live bundle1 AUDNZD", rows)

    # Call on_bar for AUDNZD alone to see signal
    from borex.alexg.multi_market import MultiMarketContext

    ctx = MultiMarketContext.at_master_bar(
        mi,
        master,
        candles,
        ts_maps,
        strength_lookback=shadow.strategy.strength_lookback,
        min_currency_edge=shadow.strategy.min_currency_edge,
        min_confirming_pairs=shadow.strategy.min_confirming_pairs,
    )
    if "AUDNZD=X" in ctx.indices:
        idx = ctx.indices["AUDNZD=X"]
        shadow.strategy.set_context("AUDNZD=X", ctx)
        sig = shadow.strategy.on_bar(idx, candles["AUDNZD=X"], None)
        print("isolated on_bar AUDNZD signal", sig)
        print("pending after isolated", "AUDNZD=X" in (shadow.strategy._pending or {}))

    # fresh restore and full step1
    shadow = _restore_shadow(fix)
    mi0 = steps[0]["master_index"]
    c0 = {
        sym: (bars[: mi0 + 1] if len(bars) > mi0 else list(bars))
        for sym, bars in series.items()
        if bars
    }
    shadow.step_master_bar(mi0, c0, master_symbol)
    before = canonicalize_shadow_state(shadow)
    shadow.step_master_bar(mi, candles, master_symbol)
    after = canonicalize_shadow_state(shadow)
    diffs = walk(live1["state_after"], after)
    print("n_diffs", len(diffs))
    for d in diffs[:20]:
        print(d)

    out = {
        "first_divergence": {
            "symbol": "AUDNZD=X",
            "timestamp": live1["master_ts"],
            "phase": "STATE_AFTER",
            "field": "strategy.pending.AUDNZD=X",
            "expected_live": live1["state_after"]["strategy"]["pending"].get("AUDNZD=X"),
            "actual_replay": after["strategy"]["pending"].get("AUDNZD=X"),
            "classification": "SIGNAL",
            "hypothesis": (
                "Same STATE_BEFORE + same step input_hash, but live armed AUDNZD "
                "ghost and replay did not. Likely hidden state in strategy caches "
                "cleared at checkpoint on live between bars, OR on_bar path depends "
                "on non-hashed fields, OR symbol scan order / context differs."
            ),
        },
        "diffs": [{"path": p, "live": a, "replay": b} for p, a, b in diffs[:50]],
        "before_step1_hash_match": True,
    }
    dest = fix.parent / "reconciliation" / "first_divergence.json"
    dest.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print("wrote", dest)


if __name__ == "__main__":
    main()
