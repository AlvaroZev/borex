# AlexG10 Test 1 — Offline Validation

STATUS: **NOT REPRODUCED** (bit-exact re-execution)  
LEDGER: **COHERENT** (original +$851 reconstructible from trade file)  
CURRENT-CACHE PATH: still strongly positive under same knobs (~+$1143)

Generated (UTC): 2026-10-03  
Git HEAD: `d75becc` (Feat/ add alexg10 first profit backtest)  
Artifacts: `data/runs/research/alexg10_test1_20261003_170949/`  
MT5 / Live / Mirror: **not used**

Interpretation: this is validation of a **discovery** result, not a fresh unbiased OOS.

---

## Executive summary

1. **Did +$851 reproduce bit-exactly?** **NO.** Current utc_v2 H1 cache + jun-index spreads re-ran to **+$1143.08** (diff **+$291.85**, fills 297 vs 278).
2. **Is +$851 a reporting artifact?** **NO.** Original trade ledger sums exactly to **851.231259328757** (`ORIGINAL_LEDGER_RECONCILIATION.json`).
3. **Exact documented config:** alexg10 entry-flip; `true_sl=True`; `rr_mode=dynamic`×1 clamp 2–5; `force_flat_daily=False`; `honor_signal_stops=False`; Thursday gate **OFF**; lev 500; 1% margin; $1000; **H1-only** fill-at-close; commission $7/lot.
4. **Dates:** continuous entries `2026-06-01` → `2026-10-02 14:00Z` (original META). Path type: **CONTINUOUS_COMPOUND_PATH** (not sum of independent months).
5. **June–September positive?** On original ledger: Jun/Jul/Aug/Sep all green. On current-cache continuous path: also all green (Sep ~+$600). Independent monthly resets do **not** equal the continuous path.
6. **September alone (original ledger):** **+$362.87** (discovery-contaminated; not AlexG10 OOS).
7. **Mechanism (current-cache ablations):** turning **force_flat back ON** collapses net to ~**−$8** (Δ ≈ **−$1151**). That is the dominant unlock. Strategy10 flip vs alexg8 adds ~**+$362**. Dynamic RR vs fixed-2 adds ~**+$308**. `honor_signal_stops=ON` destroys the edge (Δ ≈ **−$1204**).
8. **Concentrated?** Yes. Original: top-10 trades ≈ **51%** of net; top-5 pairs ≈ **93%**; only **8** symbols. Current-cache: top-10 trades ≈ **41%**.
9. **`honor_signal_stops`:** material — ON makes the noflat/dynamic-RR discovery configuration lose money.
10. **`force_flat NaT` guard:** infrastructure correctness only; no economic effect for valid fills.
11. **Unvalidated:** pinned data/spreads snapshot for bit-exact replay; H1+M1 path parity with N1; Live execution; pre-registered October+ holdout.

---

## What the +$851 run actually was

Verified against code + original META/summary:

| Question | Answer |
|---|---|
| Changed from alexg8/N1 | Entry at **planned ghost entry**, **flipped** direction toward ghost SL |
| no flat | `force_flat_daily=False` |
| rrdyn | `rr_mode=dynamic`, factor 1.0, clamp 2–5, with `true_sl` rewriting SL/TP |
| Thursday gating | **OFF** (all weekdays) |
| Path type | **Continuous** Jun→Oct 2 H1 walk; monthly table is a slice of that ledger |
| Engine | `MultiMarketEngine`, timeframe **`1h`** (not h1+1m) |
| Window | entry ≥ 2026-06-01, data end exclusive 2026-10-02 14:00 |
| Capital / lev / size | $1000 / 500× / 1% margin |
| Costs | $7/lot RT, risk_include_commission=True; spreads from live MT5 at discovery time |
| Entry | H1 close fill when planned entry touched (`ghost_fill_at_close`) |
| Exit | true_sl margin stop + dynamic RR TP (not structural ghost-SL TP) |
| honor_signal_stops | **False** (not required; False is what produced +$851) |
| NaT guard | crash-prevention only |

---

## Reproduction

### Bit-exact re-execution: FAIL

```text
reported_net:     851.2313
reproduced_net:  1143.0807
difference:      +291.8495
fills:           278 → 297
```

### Why it drifted

- Discovery script called MT5 for spreads + cache refresh; Test 1 is cache-only (MT5 closed).
- Spreads were **not pinned** in the original artifact (live-read at run time).
- H1 cache symbol mix changed: original traded EURUSD (40) and not AUDCHF; re-run traded AUDCHF (34)/CADCHF and essentially lost EURUSD fills.
- Original data snapshot was not content-hashed/pinned for replay.

### Original ledger: PASS (coherent)

| Check | Result |
|---|---|
| sum(trades.pnl) | 851.231259328757 |
| matches summary.pnl | True |
| starting 1000 + net | 1851.23 |
| engine final_equity 1916.50 | includes pre-eval ghost-prime trades |

See `ORIGINAL_LEDGER_RECONCILIATION.json`.

---

## Economic results

### Original discovery ledger

- fills 278 · WR 36.7% · PF 1.353 · net **+$851.23**
- TP 101 / margin_stop 175 / end_of_data 2 · **daily_flat 0**
- shorts +$887 · longs −$36

### Current-cache re-run (same knobs)

- fills 297 · net **+$1143.08** · continuous Jun→Sep **+$1098.06**

### Independent monthly resets (current cache)

| Month | net |
|---|---:|
| 2026-06 | +181.26 |
| 2026-07 | −37.38 |
| 2026-08 | +85.99 |
| 2026-09 | +154.87 |
| **Sum** | **+384.75** |

Label: **AGGREGATED_MONTHLY_PnL** ≠ continuous compound path (+1098). Path dependence is large.

---

## Monthly results (original continuous ledger slices)

| Month | n | net |
|---|---:|---:|
| 2026-06 | 48 | +105.77 |
| 2026-07 | 82 | +282.44 |
| 2026-08 | 69 | +37.30 |
| 2026-09 | 72 | +362.87 |
| 2026-10 (partial) | 7 | +62.84 |

Positive months Jun–Sep: **4/4** on the original continuous ledger.

---

## Attribution (ablations on current cache, same window)

| Ablation | net | Δ vs A |
|---|---:|---:|
| A alexg10 exact (noflat, rrdyn, honor=False) | +1143.08 | 0 |
| B alexg8 same exits/noflat (no flip) | +781.29 | **−361.79** |
| C alexg10 force_flat restored | −7.93 | **−1151.01** |
| D alexg10 Thursday gate ON | +762.76 | −380.32 |
| E alexg10 RR fixed 2 | +835.36 | −307.72 |
| honor_signal_stops ON | −60.49 | **−1203.57** |

**Causal reading:** the +$851-class outcome is not “alexg10 geometry alone.” It is primarily **no daily flatten** allowing dynamic-RR TPs to realize, plus the entry-flip and dynamic RR. Restoring force-flat or enabling honor_signal_stops removes the discovery edge.

---

## Profit concentration

### Original ledger

- top-10 trades ≈ **51%** of net
- top-20 trades ≈ **99%** of net
- top-5 pairs ≈ **93%** (NOKSEK alone ≈ **61%**)
- only **8** symbols filled

### Current-cache re-run

- top-10 trades ≈ **41%** of net

This is a concentrated discovery book, not a broad 60-pair result.

---

## Comparison context (not path-identical)

| Book | Path | September note |
|---|---|---|
| N1 control / Thursday | H1 signal + M1 exit | Locked OOS: both negative; Thursday relative +$27.59 |
| AlexG10 discovery | H1-only fill-at-close | Sep slice positive on discovery ledger — **not** a pre-registered AlexG10 OOS |

Do not treat AlexG10’s positive September slice as defeating or replacing the locked N1 September OOS.

---

## Failure modes / weaknesses

1. Bit-exact replay failed — data/spreads not pinned at discovery.
2. Extreme pair concentration (NOKSEK-class).
3. Short-side dominated.
4. Low WR / fat TP tail under dynamic RR.
5. Edge depends on `force_flat_daily=False` (different risk profile vs frozen N1).
6. Discovery window includes September (contaminated vs holdout logic).
7. H1-only ≠ frozen N1 M1-exit research path.

---

## Research conclusion

**NOT REPRODUCED** for bit-exact +$851 re-execution.  
**LEDGER COHERENT** for the original artifact.  
**Mechanically informative:** on today’s cache, the same knobs remain largely positive, and ablations show **no-flat + dynamic RR (+ flip)** as the economic drivers — not `honor_signal_stops`.

This does **not** promote AlexG10 to live trading.

If pursued further: pin data+spreads, then **pre-register** an unseen holdout (October+). Do not retune on June–September.
