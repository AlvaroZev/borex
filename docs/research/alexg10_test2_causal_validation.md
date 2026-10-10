# AlexG10 Test 2 — Causal + Concentration Validation

## 1. Executive verdict

**MECHANISM-DEPENDENT**

Does AlexG10 survive Test 2? **Not as a broad, mechanism-light edge.**

- Control net: **1143.08** (PF 1.4514091899575867)
- Force-flat effect (C1−C0): **-1151.01**
- Signal-stop effect (C2−C0): **-1203.57**
- 2x2 interaction: **940.98**
- Without NOKSEK: **486.80**
- Without top-5 symbols: **121.94**
- Profitable symbols: **8/9 = 88.9%**

Next step: **DO NOT PREREGISTER YET**

Remaining question: AlexG10 edge on this freeze requires force_flat_daily=False and is heavily symbol-concentrated. Smallest remaining question before any October+ preregistration: on a pinned dataset, does a pre-declared continuous-hold AlexG10 still clear a cost-adjusted hurdle after excluding NOKSEK (or after equal-symbol diagnostics)?

---

## 2. Data reproducibility

`	ext
ORIGINAL DATA RECONSTRUCTION
----------------------------
original H1 dataset recoverable: NO
original M1 dataset recoverable: NO
original spread dataset recoverable: NO
original universe recoverable: YES
original hashes recoverable: NO
original exact command recoverable: YES
`

Exact + bit-replay is permanently unresolvable (spreads never saved; H1 unpinned; discovery bars=3259 vs current freeze).

Test-2 H1 hash: 84b7433fb77c27128f3751f4c1fe2dd6522b4a8f475d4dd9829aeb2559a8dbf3 (matches Test 1: True)

---

## 3. 2×2 causal results

| Variant | Force flat | Signal stops | Net | PF | MDD | fills | daily_flat | hold_med_h |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| C0 | False | False | 1143.08 | 1.4514091899575867 | 0.0907 | 297 | 0 | 15.0 |\n| C1 | True | False | -7.93 | 0.9940954576125799 | 0.2099 | 420 | 301 | 4.0 |\n| C2 | False | True | -60.49 | 0.9390639377004923 | 0.3026 | 227 | 0 | 5.0 |\n| C3 | True | True | -270.51 | 0.7297323563402163 | 0.3542 | 383 | 210 | 3.0 |\n
- force_flat_effect (C1−C0) = **-1151.01**
- signal_stop_effect (C2−C0) = **-1203.57**
- interaction (C3−C2)−(C1−C0) = **940.98**

### Mechanism

- Force-flat OFF lets winners run multi-day into dynamic-RR TPs; ON floods daily_close exits (C1 daily_flat=301) and collapses net.
- honor_signal_stops ON keeps structural ghost-SL TP / 1R SL instead of true_sl+dynamic RR (hold_med 5.0h vs control 15.0h), destroying the fat TP tail.
- Strategy10 entry logic does not itself disable daily flatten — that was an experimental engine knob.

---

## 4. Continuous vs monthly

| Mode | Net | Fills |
|---|---:|---:|
| Continuous Jun→Sep | 1099.12 | 294 |
| Independent monthly sum | 384.75 | 285 |
| Delta | 714.37 | |

Month-crossing trades: 12 (pnl 138.61).

Mechanisms: dynamic RR winrate state carried continuously; equity-dependent 1% sizing compounds; positions can span month ends.

---

## 5. Symbol concentration

NOKSEK control net: **542.6405354301667**

Top pairs:
- NOKSEK=X: n=77 net=542.64 PF=1.8920683661515558\n- GBPTRY=X: n=32 net=181.09 PF=1.7479364151719279\n- AUDCAD=X: n=65 net=153.50 PF=1.2742428650848472\n- EURSEK=X: n=39 net=126.55 PF=1.40922376326064\n- EURNOK=X: n=40 net=102.84 PF=1.2750018906903309\n- GBPSEK=X: n=1 net=19.57 PF=inf\n- EURTRY=X: n=2 net=16.14 PF=2.429668701096171\n- CADCHF=X: n=7 net=3.60 PF=1.0436799437782092\n- AUDCHF=X: n=34 net=-2.86 PF=0.991728086472768\n\nLeave-one-out:\n- all_pairs: net=1143.08 PF=1.4514091899575867 fills=297\n- minus_NOKSEK: net=486.80 PF=1.3293605720573725 fills=183\n- minus_top2: net=322.00 PF=1.19294885020207 fills=199\n- minus_top3: net=8.74 PF=1.0047085799689317 fills=223\n- minus_top5: net=121.94 PF=1.0843414373565972 fills=182\n- minus_top8: net=1256.07 PF=1.3712021434029569 fills=371\n
Profitable symbols: 8/9

---

## 6. Trade concentration

### Control
{
  "total": 1143.08073772525,
  "top1": {
    "sum": 50.3993966211422,
    "share": 0.04409084586749126
  },
  "top5": {
    "sum": 243.94643315495546,
    "share": 0.21341137603316893
  },
  "top10": {
    "sum": 470.1884403585907,
    "share": 0.41133440958359047
  },
  "top20": {
    "sum": 892.077569567509,
    "share": 0.7804151886442935
  },
  "top50": {
    "sum": 1998.8233457312886,
    "share": 1.7486283162368574
  },
  "net_excl_top1_winner": 1092.6813411041078,
  "net_excl_top5_winners": 899.134304570295,
  "net_excl_top10_winners": 672.8922973666598
}

### Original ledger
{
  "total": 851.231259328757,
  "top1": {
    "sum": 47.322833652187256,
    "share": 0.05559339266923061
  },
  "top5": {
    "sum": 224.01491108456605,
    "share": 0.26316574800273923
  },
  "top10": {
    "sum": 436.5534432047498,
    "share": 0.5128494030505839
  },
  "top20": {
    "sum": 842.8612532532619,
    "share": 0.9901671772696702
  },
  "top50": {
    "sum": 1909.123947098071,
    "share": 2.242779416493141
  },
  "net_excl_top1_winner": 803.9084256765698,
  "net_excl_top5_winners": 627.2163482441908,
  "net_excl_top10_winners": 414.6778161240071
}

---

## 7. September (RETROSPECTIVE — not pre-registered AlexG10 OOS)

- Continuous-control Sep slice: net **599.57**, PF **1.7564300298133566**, fills 82
- Independent Sep reset: net **154.87**, fills 82
- Locked N1 Sep context: {'n1_control_net': -123.83638107165194, 'n1_thursday_net': -96.2480930193219, 'label': 'LOCKED_SEPTEMBER_OOS_separate_experiment'}

---

## 8. Original + vs current

Delta: **291.8495**

Class counts: {'NEW_TRADE': 104, 'MISSING_TRADE': 85, 'SPREAD_OR_FILL_DIFFERENCE': 9, 'EXIT_DIFFERENCE': 160, 'OTHER': 24}

| Bucket | Σ Δpnl |
|---|---:|
| NEW_TRADE | 393.75 |
| MISSING_TRADE | -128.47 |
| EXIT_DIFFERENCE | 35.70 |
| SPREAD_OR_FILL_DIFFERENCE | -6.62 |

---

## 9. Interpretation

AlexG10's apparent edge is **not** broad cross-sectional alpha. It is primarily:

1. Continuous holding (orce_flat_daily=False) enabling dynamic-RR TP realization
2. Exit rewrite semantics (honor_signal_stops=False + true_sl/dynamic RR)
3. Heavy concentration in a handful of symbols/trades

---

## Future OOS decision

### DO NOT PREREGISTER YET

AlexG10 edge on this freeze requires force_flat_daily=False and is heavily symbol-concentrated. Smallest remaining question before any October+ preregistration: on a pinned dataset, does a pre-declared continuous-hold AlexG10 still clear a cost-adjusted hurdle after excluding NOKSEK (or after equal-symbol diagnostics)?
