#!/usr/bin/env python3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from borex.alexg.strategy import AlexGMethodStrategy
from borex.backtest.engine import BacktestConfig, BacktestEngine
from borex.data import build_full_mtf_context, load_market_data

candles = load_market_data("GBPUSD=X", "730d", "1h", cache_mode="only")
mtf = build_full_mtf_context(candles, "1h", "GBPUSD=X", "730d", cache_mode="only")
s = AlexGMethodStrategy(min_rr=3, max_tp_pct=0.02, sl_mult=1.25)
r = BacktestEngine(
    s,
    BacktestConfig(
        initial_capital=10_000,
        leverage=1,
        inversed=True,
        stop_loss_pct=None,
        take_profit_pct=None,
    ),
).run(candles, symbol="GBPUSD=X", timeframe="1h", mtf=mtf)
w = [t for t in r.trades if t.pnl > 0]
l = [t for t in r.trades if t.pnl <= 0]
print("=== POR QUE POCO RETURN CON ALTO WR (1x) ===")
print(f"Trades: {len(r.trades)} | WR: {r.win_rate*100:.1f}%")
print(f"Avg win: ${sum(t.pnl for t in w)/len(w):.2f} ({sum(t.pnl_pct for t in w)/len(w)*100:.3f}% precio)")
print(f"Avg loss: ${sum(t.pnl for t in l)/len(l):.2f} ({sum(t.pnl_pct for t in l)/len(l)*100:.3f}% precio)")
print(f"Total wins: ${sum(t.pnl for t in w):.2f} | losses: ${sum(t.pnl for t in l):.2f}")
print(f"Expectancy/trade: ${(r.final_equity-10000)/len(r.trades):.2f}")
