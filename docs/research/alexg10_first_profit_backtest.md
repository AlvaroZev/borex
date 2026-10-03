# AlexG10 first profit backtest

**Run:** `frozen_thu_only_alexg10_rrdyn_noflat_20261002_144951`  
**Variant:** `C_alexg10_entry_rrdyn_noflat`  
**Eval window:** entries `2026-06-01` → last closed H1 `2026-10-02`  
**Universe:** 60 FX pairs H1 (fills occurred on **8** symbols only)  
**Capital / leverage:** $1000 / **500×**  
**Risk:** 1% margin per trade  
**RR:** **dynamic**, factor **1.0**, clamp **2–5**  
**true_sl:** True  
**force_flat_daily:** **False**  
**Thursday filter:** off (all weekdays)

---

## 1. What alexg10 is (and what this run actually executed)

### Strategy intent (`borex/alexg/strategy10.py`)
1. Same alexg8 AOI / ghost **setup detection**.
2. Wait for price to tag the **planned ghost entry** (not the ghost SL).
3. Enter **flipped** vs the original setup (trade toward the stop / stop-run).
4. Conceptually: TP at ghost SL, SL at 1R the other way.

### What this backtest did for exits
This run used **`true_sl=True` + `rr_mode=dynamic`**, so the engine **rewrote** SL/TP after fill to margin-stop-out SL and dynamic-RR TP.  
So here C is best described as:

> **AlexG10 entry timing + flip direction**, with **N1-style true_sl / dynamic RR exits**, and **no daily flatten**.

It is **not** a pure “TP = ghost SL” geometry run (that was an earlier C with `honor_signal_stops`).

---

## 2. Headline results (eval entries only)

| Metric | Value |
|---|---:|
| Trades | **278** |
| Wins / losses | **102 / 176** |
| Win rate | **36.7%** |
| Total PnL | **+$851.23** |
| Return on $1000 | **+85.1%** |
| Avg PnL / trade | +$3.06 |
| Median PnL / trade | **−$11.76** (most trades lose) |
| Avg win | **+$32.00** |
| Avg loss | **−$13.71** |
| Best / worst trade | +$47.32 / −$18.74 |
| Expectancy | **+$3.06 / trade** |
| Profit factor | **1.35** |
| Commission (sum) | $128.50 |
| Typical margin | ~$13.2–13.3 |
| Hold time | mean **30.6h**, median **16h**, p90 **72h** |
| Best win streak | 8 |
| Best loss streak | 7 |
| Engine final equity | **$1916.50** |

**Equity note:** filtered eval PnL is +$851, but engine `final_equity` is $1916. The gap is mostly from the **ghost-prime / pre-eval** segment (`start_i` before `2026-06-01`) plus compounding on the full walk. Eval-window trade PnL alone compounds to ~+$851 from a flat $1000 bookkeeping view.

Reported `max_dd_pct` in the summary file (~0.14) looks **mis-scaled / not useful**; trade-sequence cum PnL never went below ~+$20 after the first wins, but that is not a proper equity-curve MDD.

---

## 3. Exit breakdown (why it made money)

| Exit | n | WR | PnL | Avg | Median hold |
|---|---:|---:|---:|---:|---:|
| **take_profit** | 101 | 100% | **+$3229.84** | +$31.98 | 23h |
| **margin_stop** | 175 | 0% | **−$2403.82** | −$13.74 | 13h |
| end_of_data | 2 | 50% | +$25.21 | +$12.61 | 13.5h |

**Edge shape:** low WR, but **wins ~2.3× larger than losses** (avg win $32 vs avg loss $14). That matches dynamic RR with `true_sl` (TP distance ≫ SL distance once WR is weak and RR climbs toward the clamp).

No `daily_close` exits (flat daily was off) — trades could run for days until TP or margin stop.

---

## 4. Side asymmetry (important)

| Side | n | WR | PnL |
|---|---:|---:|---:|
| **short** | 175 | 40.6% | **+$887.22** |
| **long** | 103 | 30.1% | **−$35.99** |

Almost the entire edge is **shorts**. Longs are roughly flat-to-slightly-red.

---

## 5. By month

| Month | n | WR | PnL |
|---|---:|---:|---:|
| 2026-06 | 48 | 41.7% | +$105.77 |
| 2026-07 | 82 | 37.8% | +$282.44 |
| 2026-08 | 69 | 30.4% | +$37.30 |
| 2026-09 | 72 | 37.5% | +$362.87 |
| 2026-10 (partial) | 7 | 42.9% | +$62.84 |

Every month in the window was green. Softest month: **August**. Strongest: **September**, then July.

---

## 6. By weekday (entry UTC)

| Day | n | WR | PnL |
|---|---:|---:|---:|
| Mon | 58 | 41.4% | +$249.45 |
| Tue | 57 | 36.8% | +$180.92 |
| Wed | 50 | 40.0% | +$268.77 |
| Thu | 55 | 30.9% | +$38.29 |
| Fri | 48 | 39.6% | +$200.37 |
| Sun | 10 | 10.0% | **−$86.56** |

Thursday is the **weakest weekday** among Mon–Fri (still barely green). Sunday entries are rare and clearly red.

---

## 7. Symbol concentration

Only **8 symbols** produced fills (despite a 60-pair universe). Ranked by PnL:

| Symbol | n | WR | PnL |
|---|---:|---:|---:|
| **NOKSEK=X** | 73 | 43.8% | **+$518.62** |
| EURSEK=X | 39 | 38.5% | +$111.80 |
| GBPTRY=X | 36 | 36.1% | +$96.46 |
| AUDCAD=X | 46 | 30.4% | +$35.32 |
| EURNOK=X | 41 | 31.7% | +$29.96 |
| EURUSD=X | 40 | 32.5% | +$22.34 |
| GBPSEK=X | 1 | 100% | +$20.14 |
| EURTRY=X | 2 | 50% | +$16.59 |

**NOKSEK alone is ~61% of total PnL.** This is a concentrated book, not a broad 60-pair result.

---

## 8. Sample extremes

**Largest wins (all TPs):** ~+$47 on several NOKSEK / EURSEK-style winners (hold often 1–3 days).  
**Largest losses (all margin stops):** ~−$18 to −$19 (true_sl wipe size at ~1% risk + costs).

---

## 9. Comparison context (same family of experiments)

| Prior C setting | Approx result |
|---|---|
| honor_signal_stops / ghost-SL TP, daily flat ON | ~−$380 |
| true_sl + RR dyn ×1, daily flat ON | ~−$85 |
| true_sl + RR dyn ×1.4, daily flat ON | ~−$46 |
| **true_sl + RR dyn ×1, daily flat OFF (this run)** | **+$851** |

Turning off daily flatten was the big unlock for this flipped-entry / dynamic-RR combination: winners can actually reach the larger TP instead of being cut at session close.

---

## 10. Caveats / how to read this

1. **Not OOS-clean vs N1 research firewalls** — this includes Sep/Oct by design.
2. **Exit geometry ≠ strategy10 docstring** in this run (true_sl + dynamic RR override).
3. **Heavy symbol concentration** (NOKSEK). Remove/halt that pair and the headline changes a lot.
4. **Short-side dominated**; longs did not carry the edge.
5. **H1 + fill-at-close**, no M1 path — Live-like, not classic SL-touch ghost fill.
6. **max_positions=60**, score-sorted candidates — sparse fills can mean many symbols never ranked/queued into a fill.
7. WR ~37% with fat right tail is fragile if RR clamp / winrate bootstrap behaves differently live.

---

## 11. Files

- Trades: `C_alexg10_entry_rrdyn_noflat_trades.json` (under run folder; gitignored `data/runs/`)
- Summary: `C_alexg10_entry_rrdyn_noflat_summary.json`
- Machine detail dump: `C_alexg10_DETAIL.json`
- Strategy code: `borex/alexg/strategy10.py`
- This report: `docs/research/alexg10_first_profit_backtest.md`
