# Strategy Variations Log

Reference document to track how each strategy variant differs from the others.

Last updated: 2026-07-09

## Comparison matrix

| Strategy | Base logic | Entry trigger | Filters | Exit model | RR / TP model | Notes |
|---|---|---|---|---|---|---|
| `candles` | Candlestick pattern engine | Pattern signal | Optional MTF (`--mtf`) | Percent SL/TP from config | Fixed (`--stop-loss`, `--take-profit`) | Baseline non-AlexG strategy |
| `alexg` | AlexG confluence (trend + AOI + break/retest + confirmation) | Immediate signal | MTF on by default | Structural SL + target | Min RR via `--min-rr` | Score-driven confluence model |
| `alexg2` | AlexG AOI/confirmation flow | Immediate signal | Pattern quality filter (toggleable) | Structural SL + AOI TP | Min RR with optional TP fraction (`--tp-fraction`) | Cleaner AOI-first variant |
| `alexg3` | `alexg2` + cross-market currency strength | Immediate signal | Currency strength + confirming pairs | Structural SL + AOI TP | Min RR with optional TP fraction | Multi-market oriented |
| `alexg4` | `alexg3` setup logic | **Late entry**: waits for retest/touch of planned SL | Same as `alexg3` + pending invalidation rules | After fill, SL/TP are shifted from late entry | Preserves original risk/reward distances from new fill | Skips if TP hit first / SL never touched / near-miss invalidation |
| `alexg5` | `alexg4` entry logic | Same late-entry behavior as `alexg4` | Same as `alexg4` | **Margin stop as SL** | **RR from winrate** (`RR = 1 / winrate × --rr-factor`) then TP from RR | Forces `size_mode=margin` and `true_sl=True`; `--rr-factor` default `1.0` (e.g. `1.1` = 10% wider TP) |
| `institutional` | Institutional flow signals | Immediate signal | Strategy-specific filters | ATR/structure-oriented exits | Min RR via config | Separate non-AlexG branch |

## AlexG lineage details

### `alexg3` -> `alexg4`
- Keeps setup detection from `alexg3`.
- Does not enter on setup bar.
- Queues pending setup and only enters if market touches planned SL within wait window.
- Invalidation while waiting:
  - TP touched first -> skip setup.
  - SL never touched before expiry -> skip setup.
  - Near-SL approach then leave zone without fill -> skip setup.

### `alexg4` -> `alexg5`
- Keeps `alexg4` delayed-entry mechanics.
- Changes exit geometry policy at engine/config level:
  - SL comes from margin stop distance (`1 / leverage` move).
  - RR becomes dynamic from realized winrate (`RR = 1 / winrate`, fallback to default before history exists).
  - TP is recomputed from SL distance and dynamic RR.
- Optional boost: `--rr-factor` (default `1.0`) multiplies the winrate RR before TP sizing.

## Operational defaults by variant

For `alexg5` specifically, the builders enforce:
- `size_mode = "margin"`
- `true_sl = True`
- `rr_factor` from `--rr-factor` (default `1.0`)

This makes behavior deterministic regardless of CLI flags passed accidentally.

## File map (where differences live)

- Strategy classes:
  - `borex/alexg/strategy.py`
  - `borex/alexg/strategy2.py`
  - `borex/alexg/strategy3.py`
  - `borex/alexg/strategy4.py`
  - `borex/alexg/strategy5.py`
- Engine-level RR/SL/TP policy:
  - `borex/backtest/engine.py`
  - `borex/backtest/multi_market_engine.py`
  - `borex/backtest/margin_stops.py`
- CLI/viewer wiring:
  - `main.py`
  - `borex/viewer/__main__.py`

## Update checklist (when adding `alexg6+`)

1. Add row in the matrix.
2. Document what changed vs previous variant.
3. List enforced config defaults (if any).
4. Update file map with new strategy file.
5. Update this document date.

## 7/9/26-4:52:00_04:53:00 -5