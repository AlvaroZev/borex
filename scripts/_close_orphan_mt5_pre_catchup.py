#!/usr/bin/env python3
"""Close leftover real MT5 positions from pre-catchup live session (borex magic)."""
from __future__ import annotations

import MetaTrader5 as mt5

MAGIC = 88001
ok = mt5.initialize(timeout=60_000)
if not ok:
    raise SystemExit(mt5.last_error())
pos = mt5.positions_get() or ()
closed = 0
for p in pos:
    magic = int(getattr(p, "magic", 0) or 0)
    print(f"pos {p.symbol} ticket={p.ticket} magic={magic} vol={p.volume} profit={p.profit}")
    if magic != MAGIC:
        print("  skip non-borex")
        continue
    tick = mt5.symbol_info_tick(p.symbol)
    if tick is None:
        print("  no tick")
        continue
    # type 0=buy -> close sell; type 1=sell -> close buy
    order_type = mt5.ORDER_TYPE_SELL if int(p.type) == 0 else mt5.ORDER_TYPE_BUY
    price = float(tick.bid) if int(p.type) == 0 else float(tick.ask)
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": p.symbol,
        "volume": float(p.volume),
        "type": order_type,
        "position": int(p.ticket),
        "price": price,
        "deviation": 30,
        "magic": MAGIC,
        "comment": "bx_cleanup_pre_catchup",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    r = mt5.order_send(req)
    print("  close", None if r is None else (r.retcode, r.comment))
    if r is not None and r.retcode == mt5.TRADE_RETCODE_DONE:
        closed += 1
print(f"closed={closed}")
mt5.shutdown()
