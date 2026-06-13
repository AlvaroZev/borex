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
```

Defaults: `EURUSD=X`, periodo `30d`, intervalo `1h`, capital `$10,000`, SL `2%`, TP `4%`.

## Parámetros

| Parámetro | Corto | Default | Descripción |
|-----------|-------|---------|-------------|
| `--symbol` | `-s` | `EURUSD=X` | Par o activo en Yahoo Finance |
| `--period` | `-p` | `30d` | Cuánto historial descargar |
| `--interval` | `-i` | `1h` | Tamaño de cada vela |
| `--csv` | — | — | CSV local en lugar de Yahoo |
| `--capital` | — | `10000` | Capital inicial simulado |
| `--stop-loss` | — | `0.02` | Stop loss (0.02 = 2%) |
| `--take-profit` | — | `0.04` | Take profit (0.04 = 4%) |
| `--patterns` | — | todos | Patrones específicos a usar |
| `--verbose` | `-v` | off | Lista cada trade ejecutado |

```powershell
python main.py --help
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
```

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

**Intervalos:** `1m`, `5m`, `15m`, `30m`, `1h`, `1d`, `1wk`

Yahoo limita el historial según el intervalo (ej. `1h` ≈ hasta ~730 días).

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
    ├── backtest/        # Motor de simulación
    └── data/            # Carga de datos
```
