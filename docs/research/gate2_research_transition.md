# Research Transition — Close Gate 2 E → Strategy Research

Canonical copy for version control. The run-local twin lives at:

`data/runs/gate2_fidelity_assessment/runs/g2a_20260924_130000/research_transition.md`

(Note: `data/runs/` is gitignored; this `docs/` copy is the committed record.)

---

## Gate 2 E

```text
CHARACTERIZED
```

Authoritative assessment: `g2a_20260924_130000`  
Fidelity lock: `docs/research/gate2_fidelity_lock.md`

---

## What is established

* Theory reproduction — Level 5b `21/21`
* Theory→Broker mapping — Level 5b `21/21 EXACT_MATCH`
* M1 Simulator→Broker exit-event agreement — `20/20` valid sample
* Explicit exclusion — ticket `1959571094` = `OHLC_PATH_UNRESOLVABLE`
* Execution/reconciliation plumbing — `g2p` / `g2e` / `g2f`
* Simulator independence — `PASS_INDEPENDENT`

---

## What is not established

* Tick-level fidelity
* Live-account fidelity
* Strategy profitability / OOS robustness

---

## Current simulator status

```text
FROZEN_FOR_STRATEGY_RESEARCH
```

---

## Next research domain

```text
STRATEGY ROBUSTNESS / OOS VALIDATION
```

Prior N1 evidence (alexg8, ghost_sl_mult=4.0, H1→1m closest-sim, lev 500):

* Aug 2026 IS: 501 intents / 372 fills, 45.2% WR, PF 1.21, MDD 12.3%, end > $1k
* Jul 2026 OOS: **failed** (PF 0.91, MDD 24.8%, end < $1k)
* Jun 2026 OOS: survived (PF 1.08)

Next hypothesis: July failure is expectancy degradation inside the same
daily_close-dominated regime, not throughput collapse — tested first via
read-only forensic comparison of existing run artifacts.

### Forensic executed (2026-09-24)

```text
data/runs/research/n1_jul_forensic_20260924_134848/
```

Verdict: hypothesis supported by existing artifacts (no throughput collapse;
similar daily_close-dominated mix; worse mean trade PnL; higher margin_stop
share vs August). Does **not** establish a fix or profitability.
