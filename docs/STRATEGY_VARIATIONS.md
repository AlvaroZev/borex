# Strategy Variations Log

Reference document to track how each strategy variant differs from the others.

Last updated: 2026-07-23

## Comparison matrix

| Strategy | Base logic | Entry trigger | Filters | Exit model | RR / TP model | Notes |
|---|---|---|---|---|---|---|
| `candles` | Candlestick pattern engine | Pattern signal | Optional MTF (`--mtf`) | Percent SL/TP from config | Fixed (`--stop-loss`, `--take-profit`) | Baseline non-AlexG strategy |
| `alexg` | AlexG confluence (trend + AOI + break/retest + confirmation) | Immediate signal | MTF on by default | Structural SL + target | Min RR via `--min-rr` | Score-driven confluence model |
| `alexg2` | AlexG AOI/confirmation flow | Immediate signal | Pattern quality filter (toggleable) | Structural SL + AOI TP | Min RR with optional TP fraction (`--tp-fraction`) | Cleaner AOI-first variant |
| `alexg3` | `alexg2` + cross-market currency strength | Immediate signal | Currency strength + confirming pairs | Structural SL + AOI TP | Min RR with optional TP fraction | Multi-market oriented |
| `alexg4` | `alexg3` setup logic | **Late entry**: waits for retest/touch of planned SL | Same as `alexg3` + pending invalidation rules | After fill, SL/TP are shifted from late entry | Preserves original risk/reward distances from new fill | Skips if TP hit first / SL never touched / near-miss invalidation |
| `alexg5` | `alexg4` entry logic | Same late-entry as `alexg4` | Same as `alexg4` | **Margin stop as SL** | **`--rr-mode fixed` (default RR=`--min-rr`=3) or `dynamic` (`1/winrate`); both × `--rr-factor`** | Forces `size_mode=margin` and `true_sl=True` |
| `alexg5revised` | Video-1 Set-and-Forget | Ghost trade filled at its planned SL (or immediate close-in-AOI with `ghost0`) | Ablation pills: HTF bias, chart trend, patterns, retest, ghost SL entry, session | Structural SL (5–7 pips beyond AOI) + next structure TP | Strategy `min_rr`; engine may rewrite via `--rr-mode` | Body-based structure; pip AOIs 5–60; daily/weekly zones |
| `alexg7` | Video-2 winner + ghost | Ghost fill at planned SL | Filters off; London–NY overlap only | Same margin-stop / RR policy as `alexg5` | Fixed/dynamic RR like `alexg5` | Preset `video2_ghost` baked in |
| `alexg6` | `alexg5` entry + exit logic | Same late-entry as `alexg5`; opposite pending signal handling | Same as `alexg5` | Same margin-stop / RR policy as `alexg5` | Same as `alexg5` | `--second-signal`: off/flip/replace |
| `alexg6a` | `alexg6` | SL touch + favorable close → enter at close | Same as `alexg6` | Same as `alexg6` | Same as `alexg6` | Fill at close |
| `alexg6b` | `alexg6` | Same late-entry as `alexg6` | Same as `alexg6` | Margin SL armed next bar | Same as `alexg6` | Avoids same-bar wick wipe |
| `alexg6-1m` | `alexg6` on 1m bars | Same as `alexg6` | Same as `alexg6` | Same as `alexg6` | Same as `alexg6` | Bar windows scaled for 1m |
| `alexg-market` | `alexg6` + market brief | Late-entry; cancel on opposite | H&S / impulse / confluence | Same as `alexg5` | `min_rr` default 2 | `second_signal=off` |
| `institutional` | Institutional flow | Immediate | Strategy-specific | ATR/structure exits | Min RR | Non-AlexG branch |

## RR modes (alexg5+)

- `--rr-mode fixed` (default): TP RR = `--min-rr` (default **3**)
- `--rr-mode dynamic`: TP RR = `1 / winrate` (fallback `--min-rr`)
- `--rr-factor`: multiplies either mode (TP distance multiplier)

## alexg5revised + ablation

Six rule pills: HTF bias × chart trend × pattern × retest × ghost SL entry ×
session = **400** configs.

```bash
python scripts/run_ablation.py --use-cache -p 2y -i 1h --quick
python scripts/run_ablation.py --use-cache -p 2y -i 1h
```

Presets: `--ablation-preset video1|video2` plus override flags (`--htf-bias`,
`--no-pattern`, `--no-ghost-sl-entry`, `--session`, …).

### Ghost SL entry (video 1's last rule)

With `ghost1` (video 1 default) a qualifying setup is not traded on the spot:
it is queued as a ghost trade and only fills if price comes back and tags the
ghost's planned SL, at which point SL/TP are re-anchored from the fill keeping
the original risk/reward distances. The setup is dropped if TP is hit first,
if price near-misses the SL and then leaves the zone, or after
`sl_wait_max_bars` (72). Shared with alexg4/5/6 via `borex/alexg/ghost_entry.py`;
these fills are tagged `|g:…` in the trade pattern.

## 1m data (~3y)

```bash
python scripts/ensure_1m_3y.py
```

Existing HistData parquet (~2023-09 → 2026-06) already covers ~2.75y for all 10 FX pairs.

## Nautilus

Strategies registered in `borex_nautilusEngine`: `alexg3`, `alexg4`, `alexg5`, `alexg5revised`, `alexg7`.

## File map

- `borex/alexg/strategy5_revised.py`, `strategy7.py`, `ablation.py`, `structure_trend.py`, `aoi_setforget.py`, `sessions.py`, `ghost_entry.py`
- `borex/backtest/margin_stops.py` → `resolve_rr`
- `scripts/run_ablation.py`, `scripts/ensure_1m_3y.py`
- `tests/test_rr_and_ablation.py`
