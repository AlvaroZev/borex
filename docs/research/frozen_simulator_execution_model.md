# Frozen Simulator Execution Model (Strategy Research)

**Status:** `FROZEN_FOR_STRATEGY_RESEARCH`  
**Locked by:** Gate 2 E assessment `g2a_20260924_130000`  
**Engine:** `borex.backtest.multi_market_engine.MultiMarketEngine`  
**Replay adapter (Gate 2):** `borex.gate2.fidelity_replay.sim_exit.simulate_exit_on_m1_path`

Do **not** modify this model to improve historical broker agreement.

---

## Model summary (must be reported on future evaluations)

| Assumption | Current research default |
| --- | --- |
| Signal generation | H1 where strategy emits on H1 (`alexg8`) |
| Execution / exit path | M1 OHLC wick replay (`h1_signal_1m_exit`) when using closest-sim |
| Spread | Per-pair Raw spread (from MT5 read / run index) |
| Commission | `$7 / lot` (`commission_per_lot=7.0`), risk-include-commission on |
| SL/TP | Strategy geometry + dynamic RR clamps as configured per run |
| First-touch | First chronological M1 bar whose wick tags SL or TP |
| Both-wick same bar | **SL preferred** (MultiMarketEngine broker-path rule) |
| Daily flatten | UTC 19 force-flat when `force_flat_daily=True` |
| Friday behavior | As configured (`force_flat_friday`; N1 historical runs used `false`) |
| Margin wipe | Engine margin_stop when equity/margin rules trip |
| Occupancy | One open per pair at fill time; `max_positions=0` means no book-size cap |

---

## Known boundaries

* M1 OHLC cannot recover every intra-minute path (`OHLC_PATH_UNRESOLVABLE`).
* Broker deal prices may differ from theoretical SL/TP levels
  (`BROKER_EXECUTION_VARIANCE`).
* Not tick-level. Not Live-account validated.

---

## Change policy

Only reopen / change simulator exit logic given evidence of:

```text
SIMULATOR_IMPLEMENTATION_DEFECT
```

Otherwise classify discrepancies as `M1_OHLC_LIMITATION`,
`BROKER_EXECUTION_VARIANCE`, or `DATA_ISSUE`.
