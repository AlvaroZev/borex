# Borex

Backtesting de trading con patrones de velas japonesas. Simula trades sobre datos históricos (Yahoo Finance o CSV).

## Instalación

```powershell
cd borex
pip install -r requirements.txt
```

## Comando base

```powershell
python main.py
python main.py --strategy alexg -s "GBPUSD=X" -p 60d -i 1h -v
```

Defaults (candles): `EURUSD=X`, periodo `30d`, intervalo `1h`, capital `$10,000`, SL `2%`, TP `4%`.

AlexG Method activa MTF automáticamente y usa SL/TP por estructura (RR mín. 3.0).

## Parámetros

| Parámetro | Corto | Default | Descripción |
|-----------|-------|---------|-------------|
| `--strategy` | — | `candles` | `candles` o `alexg` (AlexG Method) |
| `--symbol` | `-s` | `EURUSD=X` | Par o activo en Yahoo Finance |
| `--period` | `-p` | `30d` | Cuánto historial descargar |
| `--interval` | `-i` | `1h` | Timeframe de ejecución (mín. `15m` con MTF) |
| `--mtf` | `-f` | off | Alineación multi-timeframe (todos los TF superiores) |
| `--filter-mode` | — | `trend` | Filtro MTF: `trend` o `off` |
| `--csv` | — | — | CSV local en lugar de Yahoo |
| `--capital` | — | `10000` | Capital inicial simulado |
| `--leverage` | `-l` | `1` | Apalancamiento (1–1000) |
| `--stop-loss` | — | `0.02` | Stop loss (0.02 = 2%) |
| `--take-profit` | — | `0.04` | Take profit (0.04 = 4%) |
| `--patterns` | — | todos | Patrones específicos a usar |
| `--verbose` | `-v` | off | Lista cada trade ejecutado |
| `--inversed` | — | off | Invierte dirección de trades (buy ↔ sell) |
| `--spread-pips` | — | `0` | Spread en pips (round-trip) |
| `--slippage-pips` | — | `0` | Slippage adverso por fill |
| `--commission` | — | `0` | Comisión USD por trade cerrado |
| `--min-score` | — | `70` | AlexG: score mínimo de confluencia |
| `--min-rr` | — | `3.0` | AlexG: risk/reward mínimo |
| `--max-tp-pct` | — | — | AlexG: TP máximo %% (ej. `0.01` = 1%) |
| `--sl-mult` | — | `1.0` | AlexG: ancho del SL estructural (`1.25` = 25% más lejos) |

```powershell
python main.py --help
```

## AlexG Method

Sistema de confluencia en **4 pilares** (todos obligatorios):

1. **Trend** — HH/HL (alcista) o LL/LH (bajista) **+ patrón de continuación** (bandera, pennant, tres soldados/cuervos, swings en secuencia)
2. **AOI** — soporte/resistencia con mínimo 3 toques
3. **Break & Retest** — cuerpo rompe nivel → retest → confirmación
4. **Entry confirmation** — engulfing, rechazo por mecha, momentum

Además: MTF parcialmente alineado (≥50% de TF superiores), RR ≥ 3.0, score ≥ 70.

| Score | Grado |
|-------|-------|
| 0–40 | Ignore |
| 40–70 | Watchlist |
| 70–100 | Valid Trade |
| 100+ | A+ Setup |

```powershell
# AlexG en 1h con MTF automático
python main.py --strategy alexg -s "GBPUSD=X" -p 60d -i 1h -v

# 15m entries, score mínimo 80, RR mínimo 5
python main.py --strategy alexg -s "EURUSD=X" -p 60d -i 15m --min-score 80 --min-rr 5 -v

# SL más ancho + RR 5 + TP cap 2% (parámetros optimizados en 730d GBPUSD)
python main.py --strategy alexg -s "GBPUSD=X" -p 730d -i 1h --min-rr 5 --max-tp-pct 0.02 --sl-mult 1.25 --use-cache
```

## Ejemplos

### Backtest rápido (~100 trades)

```powershell
python main.py
python main.py -v
```

### GBP/USD — 1h, 60 días, SL 1%, TP 2%

```powershell
python main.py -s "GBPUSD=X" -p 60d -i 1h --stop-loss 0.01 --take-profit 0.02 -v
```

### Multi-timeframe (alineación completa)

Con `--mtf` / `-f`, **no entra** al trade a menos que **todos** los timeframes superiores estén alineados (alcista para compras, bajista para ventas). Incluye siempre `1d` y `1wk` cuando aplican.

Escalera: `15m → 30m → 1h → 4h → 1d → 1wk`

Ejemplos según timeframe de ejecución:

| `-i` | Timeframes que deben alinear |
|------|------------------------------|
| `15m` | 30m, 1h, 4h, 1d, 1wk |
| `1h` | 4h, 1d, 1wk |
| `4h` | 1d, 1wk |

```powershell
# 1h con alineación completa (4h + 1d + 1wk)
python main.py -s "GBPUSD=X" -p 60d -i 1h -f --stop-loss 0.01 --take-profit 0.02 -v

# 15m con todos los TF superiores
python main.py -s "GBPUSD=X" -p 60d -i 15m -f --stop-loss 0.01 --take-profit 0.02 -v
```

Desactivar filtro (detecta patrones sin exigir alineación MTF):

```powershell
python main.py -s "GBPUSD=X" -p 60d -i 1h -f --filter-mode off -v
```

### Otros pares forex

```powershell
python main.py -s "EURUSD=X" -v
python main.py -s "USDJPY=X" -p 30d -i 1h
```

### Cripto o acciones

```powershell
python main.py -s "BTC-USD" -p 60d -i 1h -v
python main.py -s "AAPL" -p 1y -i 1d
```

### Ajustar cantidad de trades

```powershell
python main.py -p 25d          # ~80 trades (1h)
python main.py -p 35d          # ~120 trades (1h)
python main.py -p 1y -i 1d     # ~10-15 trades (diario)
python main.py -p 60d -i 15m   # muchos trades (intraday)
```

### Cambiar riesgo

```powershell
python main.py --stop-loss 0.01 --take-profit 0.02
python main.py --capital 50000 --stop-loss 0.015 -v
python main.py -s "GBPUSD=X" -p 60d -i 1h --leverage 100 --stop-loss 0.01 --take-profit 0.02 -v
python main.py -s "GBPUSD=X" -p 60d -i 1h --inversed -v
python main.py -s "GBPUSD=X" -p 30d -i 1h --spread-pips 2 --slippage-pips 0.5 --commission 3 -v
```

## Roadmap v2 (prioridad)

1. ~~Costos de ejecución (spread, slippage, comisión)~~ — implementado
2. **Optimización de parámetros** — grid search (`min_score`, `min_rr`, etc.)
3. **Walk-forward** — train/validate/forward splits
4. ATR-adaptive swings (reemplazar lookback fijo)
5. AOI v2 (base + impulso)
6. Monte Carlo + métricas (Sharpe, Sortino, Calmar)

### Solo ciertos patrones

```powershell
python main.py --patterns hammer bullish_engulfing morning_star -v
python main.py --patterns shooting_star bearish_engulfing evening_star -v
```

### Desde CSV

```powershell
python main.py --csv "C:\ruta\mis_velas.csv" -v
```

Formato del CSV:

```text
Date,Open,High,Low,Close,Volume
2025-01-02,1.0350,1.0380,1.0320,1.0370,0
2025-01-03,1.0370,1.0400,1.0340,1.0390,0
```

## Patrones disponibles

| Nombre | Señal |
|--------|-------|
| `hammer` | Compra |
| `shooting_star` | Venta |
| `bullish_engulfing` | Compra |
| `bearish_engulfing` | Venta |
| `morning_star` | Compra |
| `evening_star` | Venta |
| `three_white_soldiers` | Compra |
| `three_black_crows` | Venta |

Sin `--patterns` se usan todos.

## Símbolos Yahoo Finance

| Par | Símbolo |
|-----|---------|
| EUR/USD | `EURUSD=X` |
| GBP/USD | `GBPUSD=X` |
| USD/JPY | `USDJPY=X` |
| AUD/USD | `AUDUSD=X` |

**Periodos:** `5d`, `1mo`, `3mo`, `6mo`, `1y`, `2y`, `30d`, `60d`, `730d`

**Intervalos:** `1m`, `5m`, `15m`, `30m`, `1h`, `4h`, `1d`, `1wk`

Para MTF, el timeframe de filtro (`-f`) debe ser **mayor** que el de ejecución (`-i`). El TF superior se construye por resample desde `-i` cuando encaja (ej. 1h → 4h).

## Multi-timeframe (MTF)

- **Ejecución (`-i`):** patrones y trades simulados (mínimo `15m` con MTF).
- **Filtro (`-f` / `--mtf`):** exige que **todos** los TF superiores confirmen la dirección.
- Solo se usan velas **cerradas** en cada TF (sin look-ahead).
- Compra: 30m, 1h, 4h, 1d y 1wk alcistas (según `-i`).
- Venta: todos bajistas.

```
15m: [==][==][==]     ← patrones (ejemplo)
30m: [====][====]
1h:  [========]
4h:  [================]
1d:  [========================================]
1wk: [================================================================]  ← todos deben coincidir
```

## Fuente de datos

- **Por defecto:** [Yahoo Finance](https://finance.yahoo.com/) vía la librería `yfinance`.
- **Alternativa:** CSV propio con `--csv`.

## Salida del backtest

```
Estrategia: candle_patterns
Símbolo: GBPUSD=X (1h)
Capital inicial: $10,000.00
Capital final: ...
Retorno total: ...
Max drawdown: ...
Trades: ... (W: ... / L: ...)
Win rate: ...
Velas analizadas: ...
```

Con `-v` se listan todos los trades: dirección, patrón, entrada/salida, PnL y motivo de cierre (`stop_loss`, `take_profit`, `opposite_signal`, `end_of_data`).

## Estructura del proyecto

```
borex/
├── main.py              # CLI
├── requirements.txt
└── borex/
    ├── models/          # Vela OHLCV y señales
    ├── patterns/        # Detección de patrones
    ├── strategy/        # Estrategias
    ├── alexg/           # AlexG Method (trend, AOI, break/retest, scoring)
    ├── backtest/        # Motor de simulación
    └── data/            # Carga de datos y alineación MTF
```
