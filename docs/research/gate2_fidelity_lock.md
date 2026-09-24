# Gate 2 E — Fidelity Lock

**Status:** `CHARACTERIZED`  
**Authoritative assessment:** `g2a_20260924_130000`  
**Assessment path:** `data/runs/gate2_fidelity_assessment/runs/g2a_20260924_130000/`  
**Git branch at lock:** `closest-sim`  

This document freezes the Gate 2 execution / simulator-fidelity investigation as a
**research constraint**, not as a claim of perfection.

Do **not** call this:

- `PERFECT`
- `TICK-LEVEL VALIDATED`
- automatic `PASS` / `FAIL`

---

## Gate 2 E status

```text
CHARACTERIZED
```

Simulator results may be used for strategy research **provided** research respects
the documented M1-OHLC execution model and does **not** claim tick-level broker
equivalence.

---

## Evidence summary (authoritative)

| Dimension | Result |
| --- | --- |
| Theory reproduction (Level 5b) | **21/21** |
| Natural Theory→Broker mapping (Level 5b) | **21/21 EXACT_MATCH** |
| Natural trades with continuous M1 | **21** |
| Replayable natural trades | **20** |
| Exit-event agreement (valid sample) | **20/20** |
| Different exit events | **0** |
| OHLC-unresolvable (excluded) | **1/21** (`ticket 1959571094`) |
| Simulator independence | **PASS_INDEPENDENT** |

Compact form:

```text
Theory reproduction: 21/21
Natural simulator replay: 20/21
Valid exit-event agreement: 20/20
OHLC-unresolvable: 1/21
```

### Explicit exclusion (not a simulator disagreement)

```text
AUDUSD long
ticket 1959571094
source: g2m_20260924_043542
class: OHLC_PATH_UNRESOLVABLE
```

Continuous M1 archive present (72 bars, no gap); broker exited `BROKER_SL`; OHLC wick
replay never touches SL/TP. This is an inherent M1 path limitation — **not** a
simulator bug and **not** missing data.

---

## What is established

### Theory reproduction

Frozen Theory pipeline reproduces observed natural Theory events causally from the
relevant candle sequences (`21/21`).

### Natural Theory → Broker mapping

Direct Theory-to-broker execution mapping is operational (`21/21 EXACT_MATCH` on
Level 5b).

### Continuous M1 evidence

Continuous M1 archive is operational across the six micro-fidelity symbols: no
archive gaps, trade windows closed, broker exit timestamps persisted.

### Simulator exit-event fidelity

For the valid Level 5b natural replay sample:

```text
20/20 same exit event
0/20 different event
```

### Simulator independence

```text
m1 archive
→ simulate_exit_on_m1_path
→ MultiMarketEngine
→ SimExitResult
→ broker comparison
```

Broker exit information is **not** fed into the simulator before the simulated exit.

---

## Controlled probes (plumbing only)

| Run | Role |
| --- | --- |
| `g2p_20260922_162842` | Market entry, deal reconciliation, execution reporting, market-close handling |
| `g2e_20260923_170458` | Broker SL/TP deal reasons + entry/exit reconciliation |
| `g2f_20260923_181609` | Mapping architecture, natural vs controlled separation, taxonomy |

These are **not** Theory-generated natural trades and are **not** part of the natural
simulator-fidelity denominator.

Primary natural / replay lineage:

```text
g2m_20260924_001929 → g2r_20260924_032600
g2m_20260924_043542 → g2r_20260924_055920
assessment → g2a_20260924_130000
```

---

## Known limitations (permanent)

1. **M1 OHLC path limitation** — cannot reconstruct every intra-minute broker path
   (canonical example: ticket `1959571094`).
2. **Broker execution price variance** — fills can differ from theoretical SL/TP
   levels (observed same-event max |Δ| ≈ **0.8 pips**). Do not force simulator fills
   to equal broker fills.
3. **Tick-level fidelity** — not established.
4. **Live-account fidelity** — not established.
5. **Strategy profitability** — not established by Gate 2.

These are research limitations, **not** reasons to continue micro-fidelity batches
indefinitely.

---

## Simulator status

```text
FROZEN_FOR_STRATEGY_RESEARCH
```

Do **not** change `MultiMarketEngine` exit logic, SL/TP trigger semantics,
first-touch / both-wick behavior, OHLC handling, spread treatment, or timestamp
behavior unless a future experiment independently demonstrates
`SIMULATOR_IMPLEMENTATION_DEFECT`.

---

## Research policy going forward

> Simulator results may be used for strategy research, provided the research
> respects the documented M1-OHLC execution model and does not claim tick-level
> broker equivalence.

Do **not** reopen Gate 2 merely because an individual future trade has broker
slippage or OHLC ambiguity.

Classify future discrepancies as one of:

```text
M1_OHLC_LIMITATION
BROKER_EXECUTION_VARIANCE
DATA_ISSUE
SIMULATOR_IMPLEMENTATION_DEFECT
```

Reopen simulator-fidelity work **only** if there is evidence of
`SIMULATOR_IMPLEMENTATION_DEFECT` or a systematic unexplained discrepancy.

Do **not**:

- run another Gate 2 micro-fidelity batch for curiosity
- modify the simulator to improve historical agreement
- modify `gate2_micro_fidelity` / optimize the micro-fidelity strategy
- alter simulator behavior to make historical broker trades match better

---

## Next research domain

```text
STRATEGY ROBUSTNESS / OOS VALIDATION
```

Fidelity is no longer the primary blocker. Strategy robustness remains unproven.
