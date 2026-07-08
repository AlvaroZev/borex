# AlexG3 backtest results history

Summaries of multi-market `alexg3` experiments and the exact commands used to reproduce them.

**Shared defaults (unless noted):**

| Setting | Value |
|--------|--------|
| Strategy | `alexg3` |
| Master symbol | `EURUSD=X` |
| Universe | 10 FX pairs (cached Dukascopy) |
| Interval | `1h` |
| Period | `max` (~17k bars) |
| Capital | `$1,000` |
| Size mode | `margin` |
| Position size | `0.01` (1% free cash as margin) |
| Max positions | `9999` |
| Close on opposite | on |
| Pattern quality filter | on (default) |
| Cache | `--use-cache` |

**Common base command:**

```powershell
cd "c:\Users\azeva\OneDrive\Documentos\work\trading\borex-main"
$env:PYTHONUNBUFFERED=1
```

CSV export helper (same engine params as run #11):

```powershell
python scripts\run_alexg3_csv.py --capital 1000 -l 5000 -p max -i 1h --out data/alexg3_1000_5000x_trades.csv
```

---

## 2026-07-08 — Batch A: 11 parallel viewers (`-p max`)

Inverse SL/TP double-flip fix and confirmation quality filter were already in the tree for this batch.

Viewer pattern (change flags + port):

```powershell
python -m borex.viewer --strategy alexg3 -s "EURUSD=X" -p max -i 1h --use-cache `
  --capital 1000 --size-mode margin --position-size 0.01 --max-positions 9999 `
  --close-on-opposite <FLAGS> --port <PORT>
```

### Leaderboard (by return)

| Rank | # | Port | Variant | Lev | Return | WR | Trades | Final $ | Max DD | PF |
|-----:|--:|-----:|---------|----:|-------:|---:|-------:|--------:|-------:|---:|
| 1 | 1 | 8801 | inversed | 5000x | **+126.21%** | 9.02% | 1308 | 2262.09 | 65.32% | 1.04 |
| 2 | 3 | 8803 | inversed + TP 10% + RR 0.5 | 5000x | **+86.48%** | 9.02% | 1308 | 1864.78 | 65.32% | 1.03 |
| 3 | 5 | 8805 | no-momentum | 5000x | −10.02% | 5.97% | 318 | 899.82 | 45.63% | 0.96 |
| 4 | 10 | 8810 | no-momentum | 500x | −10.74% | 14.52% | 310 | 892.62 | 34.00% | 0.94 |
| 5 | 6 | 8806 | inversed | 500x | −34.66% | 19.58% | 1134 | 653.45 | 53.21% | 0.95 |
| 6 | 2 | 8802 | inversed + TP 10% | 5000x | −48.96% | 9.02% | 1308 | 510.41 | 75.22% | 0.96 |
| 7 | 8 | 8808 | inversed + TP 10% + RR 0.5 | 500x | −49.04% | 12.01% | 1199 | 509.56 | 61.89% | 0.89 |
| 8 | 9 | 8809 | TP 10% + RR 0.5 | 500x | −72.00% | 10.75% | 1209 | 279.96 | 78.14% | 0.81 |
| 9 | 7 | 8807 | inversed + TP 10% | 500x | −72.05% | 7.10% | 1254 | 279.52 | 76.08% | 0.67 |
| 10 | 4 | 8804 | TP 10% + RR 0.5 | 5000x | −99.94% | 0.79% | 1258 | 0.64 | 99.96% | 0.33 |
| 11 | 11 | 8811 | **baseline / original CSV** | 5000x | −99.94% | 0.79% | 1258 | 0.64 | 99.96% | 0.34 |

### Replication commands (Batch A)

#### #1 — inversed, 5000x (best return)

```powershell
python -m borex.viewer --strategy alexg3 -s "EURUSD=X" -p max -i 1h --use-cache `
  --capital 1000 -l 5000 --size-mode margin --position-size 0.01 --max-positions 9999 `
  --close-on-opposite --inversed --port 8801
```

- Avg win `$290.48` / avg loss `-$27.74`
- Top confirmation PnL: bearish_engulfing `+$1,204`, rejection `+$939`

#### #2 — inversed + TP 10% of AOI path, 5000x

```powershell
python -m borex.viewer --strategy alexg3 -s "EURUSD=X" -p max -i 1h --use-cache `
  --capital 1000 -l 5000 --size-mode margin --position-size 0.01 --max-positions 9999 `
  --close-on-opposite --inversed --tp-fraction 0.1 --port 8802
```

#### #3 — inversed + TP 10% + min-RR 0.5, 5000x

```powershell
python -m borex.viewer --strategy alexg3 -s "EURUSD=X" -p max -i 1h --use-cache `
  --capital 1000 -l 5000 --size-mode margin --position-size 0.01 --max-positions 9999 `
  --close-on-opposite --inversed --tp-fraction 0.1 --min-rr 0.5 --port 8803
```

#### #4 — TP 10% + min-RR 0.5, 5000x (not inversed)

```powershell
python -m borex.viewer --strategy alexg3 -s "EURUSD=X" -p max -i 1h --use-cache `
  --capital 1000 -l 5000 --size-mode margin --position-size 0.01 --max-positions 9999 `
  --close-on-opposite --tp-fraction 0.1 --min-rr 0.5 --port 8804
```

#### #5 — no momentum candles, 5000x

```powershell
python -m borex.viewer --strategy alexg3 -s "EURUSD=X" -p max -i 1h --use-cache `
  --capital 1000 -l 5000 --size-mode margin --position-size 0.01 --max-positions 9999 `
  --close-on-opposite --no-momentum --port 8805
```

- Fewer trades because momentum is the majority confirmation type

#### #6 — inversed, 500x

```powershell
python -m borex.viewer --strategy alexg3 -s "EURUSD=X" -p max -i 1h --use-cache `
  --capital 1000 -l 500 --size-mode margin --position-size 0.01 --max-positions 9999 `
  --close-on-opposite --inversed --port 8806
```

#### #7 — inversed + TP 10%, 500x

```powershell
python -m borex.viewer --strategy alexg3 -s "EURUSD=X" -p max -i 1h --use-cache `
  --capital 1000 -l 500 --size-mode margin --position-size 0.01 --max-positions 9999 `
  --close-on-opposite --inversed --tp-fraction 0.1 --port 8807
```

#### #8 — inversed + TP 10% + min-RR 0.5, 500x

```powershell
python -m borex.viewer --strategy alexg3 -s "EURUSD=X" -p max -i 1h --use-cache `
  --capital 1000 -l 500 --size-mode margin --position-size 0.01 --max-positions 9999 `
  --close-on-opposite --inversed --tp-fraction 0.1 --min-rr 0.5 --port 8808
```

#### #9 — TP 10% + min-RR 0.5, 500x

```powershell
python -m borex.viewer --strategy alexg3 -s "EURUSD=X" -p max -i 1h --use-cache `
  --capital 1000 -l 500 --size-mode margin --position-size 0.01 --max-positions 9999 `
  --close-on-opposite --tp-fraction 0.1 --min-rr 0.5 --port 8809
```

#### #10 — no momentum, 500x

```powershell
python -m borex.viewer --strategy alexg3 -s "EURUSD=X" -p max -i 1h --use-cache `
  --capital 1000 -l 500 --size-mode margin --position-size 0.01 --max-positions 9999 `
  --close-on-opposite --no-momentum --port 8810
```

#### #11 — baseline / original CSV params (not inversed, full TP, momentum on, 5000x)

```powershell
python -m borex.viewer --strategy alexg3 -s "EURUSD=X" -p max -i 1h --use-cache `
  --capital 1000 -l 5000 --size-mode margin --position-size 0.01 --max-positions 9999 `
  --close-on-opposite --port 8811
```

Or CLI + CSV:

```powershell
python scripts\run_alexg3_csv.py --capital 1000 -l 5000 -p max -i 1h `
  --out data/alexg3_1000_5000x_trades.csv
```

Matches CSV export: final ~`$0.64`, −99.94%, 1258 trades, WR 0.79%.

---

## Earlier notes (pre Batch A)

### 2026-07-08 — Standalone CSV baseline (`scripts/run_alexg3_csv.py`)

Same as batch #11: $1000 / 5000x / max / margin 1% / close-on-opposite / quality filter on.

| Metric | Value |
|--------|------:|
| Final capital | $0.64 |
| Return | −99.94% |
| Trades | 1258 (10W / 1248L) |
| Win rate | 0.79% |
| Dominant exit | `margin_stop` (~1247) |
| Worst confirmation | `momentum` (−$753) |
| Best confirmation | `bearish_engulfing` (+$50) |

### 2026-07-07 — Parallel viewers on **90d** (superseded)

Used `-p 90d` for speed (~1.5k bars). Many runs landed near **~100 trades** because the date window ended, not a hard trade cap. Replaced by Batch A (`max`).

---

## How to append a new result

1. Re-run the viewer or CSV script with the flags you want.
2. Copy the printed `Estrategia: …` summary block.
3. Add a dated section here with:
   - headline metrics table row
   - exact PowerShell command
   - optional confirmation / notes (bugs fixed, filters, etc.)
