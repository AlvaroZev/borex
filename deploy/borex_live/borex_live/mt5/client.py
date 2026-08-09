from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from borex.models.candle import Candle

from borex_live.mt5.symbols import mt5_to_yahoo, yahoo_to_mt5


@dataclass
class Mt5OrderResult:
    ok: bool
    ticket: int = 0
    message: str = ""
    retcode: int = 0


@dataclass
class Mt5Position:
    ticket: int
    symbol: str
    side: str
    volume: float
    price_open: float
    sl: float
    tp: float
    profit: float
    magic: int = 0
    comment: str = ""


class Mt5Client:
    """Thin MetaTrader5 wrapper. Safe to construct without terminal (dry-run)."""

    MAGIC = 88001

    TIMEFRAME_MAP = {
        "1m": "TIMEFRAME_M1",
        "5m": "TIMEFRAME_M5",
        "15m": "TIMEFRAME_M15",
        "30m": "TIMEFRAME_M30",
        "1h": "TIMEFRAME_H1",
        "4h": "TIMEFRAME_H4",
        "1d": "TIMEFRAME_D1",
    }

    def __init__(
        self,
        *,
        path: str = "",
        login: int = 0,
        password: str = "",
        server: str = "",
        dry_run: bool = False,
    ) -> None:
        self.path = path
        self.login = login
        self.password = password
        self.server = server
        self.dry_run = dry_run
        self._mt5: Any = None
        self._connected = False

    def connect(self) -> None:
        """
        Connect once to MT5. Prefer attaching to an already-open terminal
        (no shutdown/re-login thrash → no connect sounds).
        https://www.mql5.com/en/docs/python_metatrader5/mt5initialize_py
        """
        if self.dry_run:
            self._connected = True
            return
        import MetaTrader5 as mt5

        self._mt5 = mt5
        # Already attached?
        if self._connected:
            try:
                if mt5.terminal_info() is not None and mt5.account_info() is not None:
                    return
            except Exception:
                self._connected = False

        path = self.path or r"C:\Program Files\MetaTrader 5\terminal64.exe"

        # 1) Attach to running terminal (quiet). Do NOT shutdown between attempts.
        ok = bool(mt5.initialize(path=path, timeout=60_000, portable=False))
        if not ok:
            ok = bool(mt5.initialize(timeout=60_000))

        # 2) If attached but wrong/empty account, login in-place (no re-init).
        if ok and self.login and self.password:
            acc = mt5.account_info()
            need_login = acc is None or int(acc.login) != int(self.login)
            if need_login:
                ok = bool(
                    mt5.login(
                        int(self.login),
                        password=self.password,
                        server=self.server or None,
                    )
                )

        # 3) Last resort: initialize with credentials in one call.
        if not ok and self.login and self.password:
            ok = bool(
                mt5.initialize(
                    path=path,
                    login=int(self.login),
                    password=self.password,
                    server=self.server or None,
                    timeout=60_000,
                    portable=False,
                )
            )

        if not ok:
            err = mt5.last_error()
            hint = ""
            if err and err[0] == -6:
                hint = (
                    " Authorization failed: log into MT5 in the UI first, "
                    "or fix MT5_PASSWORD / server (master password)."
                )
            elif err and err[0] == -10005:
                hint = " IPC timeout: enable Algo Trading / try portable MT5 install."
            raise RuntimeError(f"MT5 initialize failed: {err}.{hint}")
        self._connected = True

    def disconnect(self) -> None:
        if self._mt5 is not None and not self.dry_run and self._connected:
            try:
                self._mt5.shutdown()
            except Exception:
                pass
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def _tf(self, interval: str) -> int:
        if self.dry_run:
            return 16385  # H1 placeholder
        key = self.TIMEFRAME_MAP.get(interval.lower())
        if not key:
            raise ValueError(f"Unsupported interval for MT5: {interval}")
        return getattr(self._mt5, key)

    def ensure_symbol(self, yahoo_symbol: str) -> str:
        """Select symbol in Market Watch so history/trading works."""
        mt5_sym = yahoo_to_mt5(yahoo_symbol)
        if self.dry_run:
            return mt5_sym
        info = self._mt5.symbol_info(mt5_sym)
        if info is None:
            raise RuntimeError(f"MT5 symbol not found: {mt5_sym}")
        if not info.visible:
            if not self._mt5.symbol_select(mt5_sym, True):
                raise RuntimeError(
                    f"MT5 symbol_select failed for {mt5_sym}: {self._mt5.last_error()}"
                )
        return mt5_sym

    def list_forex_yahoo_symbols(self) -> list[str]:
        """
        All tradeable FX pairs under the broker Forex tree
        (majors + minors + exotics), as Yahoo-style keys.
        """
        if self.dry_run:
            return []
        raw = self._mt5.symbols_get()
        if raw is None:
            raise RuntimeError(f"MT5 symbols_get failed: {self._mt5.last_error()}")
        out: list[str] = []
        for s in raw:
            path = getattr(s, "path", "") or ""
            if "Forex" not in path:
                continue
            # trade_mode 0 = disabled
            if int(getattr(s, "trade_mode", 0) or 0) == 0:
                continue
            name = str(s.name)
            if len(yahoo_to_mt5(name)) != 6:
                continue
            out.append(mt5_to_yahoo(name))
        return sorted(set(out))

    def fetch_bars(
        self,
        yahoo_symbol: str,
        interval: str,
        *,
        count: int = 500,
        from_pos: int = 0,
    ) -> list[Candle]:
        if self.dry_run:
            return []
        mt5_sym = self.ensure_symbol(yahoo_symbol)
        rates = self._mt5.copy_rates_from_pos(mt5_sym, self._tf(interval), from_pos, count)
        if rates is None:
            raise RuntimeError(
                f"MT5 copy_rates_from_pos failed for {mt5_sym}: {self._mt5.last_error()}"
            )
        candles: list[Candle] = []
        for row in rates:
            ts = datetime.fromtimestamp(int(row["time"]), tz=timezone.utc)
            candles.append(
                Candle(
                    timestamp=ts,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["tick_volume"]),
                )
            )
        return candles

    def account_balance(self) -> float:
        if self.dry_run:
            return 0.0
        info = self._mt5.account_info()
        return float(info.balance) if info else 0.0

    def account_equity(self) -> float:
        if self.dry_run:
            return 0.0
        info = self._mt5.account_info()
        if info is None:
            return 0.0
        return float(getattr(info, "equity", 0.0) or info.balance or 0.0)

    def account_free_margin(self) -> float:
        if self.dry_run:
            return 0.0
        info = self._mt5.account_info()
        if info is None:
            return 0.0
        return float(getattr(info, "margin_free", 0.0) or 0.0)

    def quote_price(self, yahoo_symbol: str, side: str) -> float | None:
        """Current ask for buy / bid for sell."""
        if self.dry_run:
            return None
        mt5_sym = self.ensure_symbol(yahoo_symbol)
        tick = self._mt5.symbol_info_tick(mt5_sym)
        if tick is None:
            return None
        return float(tick.ask) if side.lower() == "buy" else float(tick.bid)

    def lots_for_risk(
        self,
        yahoo_symbol: str,
        side: str,
        entry: float,
        sl: float,
        risk_money: float,
    ) -> float:
        """
        Lots so a move from entry→sl loses ~risk_money in account currency.

        Matches backtest margin mode: risk_money = equity × position_size_pct.
        Clamped to symbol volume limits and free margin.
        """
        if self.dry_run:
            return 0.01
        if risk_money <= 0 or entry <= 0 or sl <= 0:
            return 0.0
        mt5_sym = self.ensure_symbol(yahoo_symbol)
        mt5 = self._mt5
        info = mt5.symbol_info(mt5_sym)
        if info is None:
            return 0.0

        sl_dist = abs(float(entry) - float(sl))
        if sl_dist <= 0:
            return 0.0

        tick_size = float(getattr(info, "trade_tick_size", 0.0) or info.point or 0.0)
        tick_value = float(getattr(info, "trade_tick_value", 0.0) or 0.0)
        if tick_size <= 0 or tick_value <= 0:
            # Fallback: contract_size × price_move in quote, treat as account ccy
            contract = float(getattr(info, "trade_contract_size", 0.0) or 100_000.0)
            loss_per_lot = contract * sl_dist
        else:
            loss_per_lot = (sl_dist / tick_size) * tick_value
        if loss_per_lot <= 0:
            return 0.0

        raw = float(risk_money) / loss_per_lot
        vol = self._normalize_volume(mt5_sym, raw)

        # Cap by free margin so the order is actually placeable
        free = self.account_free_margin()
        order_type = mt5.ORDER_TYPE_BUY if side.lower() == "buy" else mt5.ORDER_TYPE_SELL
        while vol > 0 and free > 0:
            needed = mt5.order_calc_margin(order_type, mt5_sym, vol, float(entry))
            if needed is None or float(needed) <= free * 0.95:
                break
            step = float(info.volume_step or 0.01)
            vol = self._normalize_volume(mt5_sym, vol - step)
            if vol <= float(info.volume_min or step):
                needed = mt5.order_calc_margin(order_type, mt5_sym, vol, float(entry))
                if needed is not None and float(needed) > free * 0.95:
                    return 0.0
                break
        return float(vol)

    def closed_deal_profit(self, position_ticket: int, *, lookback_days: int = 14) -> float | None:
        """Sum profit+swap+commission for deals on a closed position ticket."""
        if self.dry_run or position_ticket <= 0:
            return None
        from datetime import timedelta

        mt5 = self._mt5
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=lookback_days)
        deals = mt5.history_deals_get(start, end)
        if not deals:
            return None
        total = 0.0
        found = False
        for d in deals:
            if int(getattr(d, "position_id", 0) or 0) != int(position_ticket):
                continue
            found = True
            total += float(getattr(d, "profit", 0.0) or 0.0)
            total += float(getattr(d, "swap", 0.0) or 0.0)
            total += float(getattr(d, "commission", 0.0) or 0.0)
        return total if found else None

    def open_positions(self, yahoo_symbol: str | None = None) -> list[Mt5Position]:
        if self.dry_run:
            return []
        mt5_sym = yahoo_to_mt5(yahoo_symbol) if yahoo_symbol else None
        raw = (
            self._mt5.positions_get(symbol=mt5_sym)
            if mt5_sym
            else self._mt5.positions_get()
        )
        if raw is None:
            return []
        out: list[Mt5Position] = []
        for p in raw:
            out.append(
                Mt5Position(
                    ticket=int(p.ticket),
                    symbol=str(p.symbol),
                    side="long" if p.type == 0 else "short",
                    volume=float(p.volume),
                    price_open=float(p.price_open),
                    sl=float(p.sl),
                    tp=float(p.tp),
                    profit=float(p.profit),
                    magic=int(getattr(p, "magic", 0) or 0),
                    comment=str(getattr(p, "comment", "") or ""),
                )
            )
        return out

    def pending_order_exists(self, ticket: int) -> bool:
        if self.dry_run:
            return ticket < 0
        if ticket <= 0:
            return False
        orders = self._mt5.orders_get(ticket=int(ticket))
        return bool(orders)

    def position_for_ghost(self, yahoo_symbol: str) -> Mt5Position | None:
        """Find an open position opened by this service for the pair."""
        mt5_sym = yahoo_to_mt5(yahoo_symbol)
        for p in self.open_positions(yahoo_symbol):
            if p.symbol != mt5_sym:
                continue
            if p.magic == self.MAGIC or "borex" in (p.comment or "").lower():
                return p
        return None

    def _normalize_volume(self, mt5_sym: str, volume: float) -> float:
        info = self._mt5.symbol_info(mt5_sym)
        if info is None:
            return float(volume)
        step = float(info.volume_step or 0.01)
        vmin = float(info.volume_min or step)
        vmax = float(info.volume_max or volume)
        steps = round(float(volume) / step) if step > 0 else 1
        out = max(vmin, min(vmax, steps * step))
        # avoid float dust
        digits = max(0, len(str(step).rstrip("0").split(".")[-1]) if "." in str(step) else 0)
        return round(out, digits or 2)

    def _normalize_price(self, mt5_sym: str, price: float) -> float:
        info = self._mt5.symbol_info(mt5_sym)
        digits = int(info.digits) if info else 5
        return round(float(price), digits)

    def _min_stop_distance(self, mt5_sym: str) -> float:
        """Broker min SL/TP distance in price units (stops_level / freeze / spread)."""
        info = self._mt5.symbol_info(mt5_sym)
        if info is None:
            return 0.0
        pts = max(
            int(getattr(info, "trade_stops_level", 0) or 0),
            int(getattr(info, "trade_freeze_level", 0) or 0),
            int(getattr(info, "spread", 0) or 0) + 2,
        )
        return float(pts) * float(info.point or 0.0)

    def _adjust_stops(
        self,
        mt5_sym: str,
        side: str,
        entry: float,
        sl: float | None,
        tp: float | None,
    ) -> tuple[float | None, float | None]:
        """
        Keep SL/TP on the correct side of entry and at least min stop distance away.
        Buy: SL < entry < TP. Sell: TP < entry < SL.
        """
        if (sl is None or sl <= 0) and (tp is None or tp <= 0):
            return sl, tp
        min_d = self._min_stop_distance(mt5_sym)
        side_l = side.lower()
        out_sl = float(sl) if sl is not None and sl > 0 else None
        out_tp = float(tp) if tp is not None and tp > 0 else None

        if side_l == "buy":
            if out_sl is not None:
                out_sl = min(out_sl, entry - min_d)
            if out_tp is not None:
                out_tp = max(out_tp, entry + min_d)
        else:
            if out_sl is not None:
                out_sl = max(out_sl, entry + min_d)
            if out_tp is not None:
                out_tp = min(out_tp, entry - min_d)

        if out_sl is not None:
            out_sl = self._normalize_price(mt5_sym, out_sl)
        if out_tp is not None:
            out_tp = self._normalize_price(mt5_sym, out_tp)
        return out_sl, out_tp

    def _shift_stops_to_fill(
        self,
        planned_entry: float,
        fill: float,
        sl: float | None,
        tp: float | None,
    ) -> tuple[float | None, float | None]:
        """Move protective levels by the same delta as fill vs planned ghost price."""
        delta = float(fill) - float(planned_entry)
        out_sl = (float(sl) + delta) if sl is not None and sl > 0 else None
        out_tp = (float(tp) + delta) if tp is not None and tp > 0 else None
        return out_sl, out_tp

    def place_pending_ghost(
        self,
        yahoo_symbol: str,
        side: str,
        price: float,
        volume: float,
        *,
        sl: float | None = None,
        tp: float | None = None,
        comment: str = "borex_ghost",
    ) -> Mt5OrderResult:
        """
        Pending limit at the ghost trigger (strategy stop_loss).

        BUY → BUY_LIMIT at SL (await dip). SELL → SELL_LIMIT at SL (await rally).
        SL/TP are the protective levels after the pending fills.

        If price has already traded through the limit, falls back to a market
        order and shifts SL/TP to the actual fill price (avoids retcode 10016).
        """
        if self.dry_run:
            return Mt5OrderResult(ok=True, ticket=-1, message="dry_run pending")
        mt5_sym = self.ensure_symbol(yahoo_symbol)
        mt5 = self._mt5
        tick = mt5.symbol_info_tick(mt5_sym)
        if tick is None:
            return Mt5OrderResult(ok=False, message=f"no tick for {mt5_sym}")

        entry = self._normalize_price(mt5_sym, price)
        side_l = side.lower()
        ask, bid = float(tick.ask), float(tick.bid)

        if side_l == "buy":
            # Limit buys must be below ask. If already through, market-enter instead.
            if entry >= ask:
                m_sl, m_tp = self._shift_stops_to_fill(entry, ask, sl, tp)
                m_sl, m_tp = self._adjust_stops(mt5_sym, "buy", ask, m_sl, m_tp)
                return self.place_market_with_sltp(
                    yahoo_symbol,
                    "buy",
                    volume,
                    m_sl if m_sl is not None else 0.0,
                    m_tp if m_tp is not None else 0.0,
                    comment=comment[:31],
                )
            order_type = mt5.ORDER_TYPE_BUY_LIMIT
        else:
            if entry <= bid:
                m_sl, m_tp = self._shift_stops_to_fill(entry, bid, sl, tp)
                m_sl, m_tp = self._adjust_stops(mt5_sym, "sell", bid, m_sl, m_tp)
                return self.place_market_with_sltp(
                    yahoo_symbol,
                    "sell",
                    volume,
                    m_sl if m_sl is not None else 0.0,
                    m_tp if m_tp is not None else 0.0,
                    comment=comment[:31],
                )
            order_type = mt5.ORDER_TYPE_SELL_LIMIT

        adj_sl, adj_tp = self._adjust_stops(mt5_sym, side_l, entry, sl, tp)
        vol = self._normalize_volume(mt5_sym, volume)
        request: dict[str, Any] = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": mt5_sym,
            "volume": vol,
            "type": order_type,
            "price": entry,
            "deviation": 20,
            "magic": self.MAGIC,
            "comment": (comment or "borex_ghost")[:31],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_RETURN,
        }
        if adj_sl is not None and adj_sl > 0:
            request["sl"] = adj_sl
        if adj_tp is not None and adj_tp > 0:
            request["tp"] = adj_tp

        result = mt5.order_send(request)
        if result is None:
            return Mt5OrderResult(ok=False, message=str(mt5.last_error()))

        # Fallback: some brokers reject pending+stops; place bare pending.
        if int(result.retcode) == 10016 and ("sl" in request or "tp" in request):
            bare = dict(request)
            bare.pop("sl", None)
            bare.pop("tp", None)
            result = mt5.order_send(bare)
            if result is None:
                return Mt5OrderResult(ok=False, message=str(mt5.last_error()))

        ok = result.retcode in (
            mt5.TRADE_RETCODE_DONE,
            getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", -1),
            getattr(mt5, "TRADE_RETCODE_PLACED", -1),
        )
        return Mt5OrderResult(
            ok=ok,
            ticket=int(result.order),
            message=str(result.comment) if result.comment else f"retcode={result.retcode}",
            retcode=int(result.retcode),
        )

    def place_market_with_sltp(
        self,
        yahoo_symbol: str,
        side: str,
        volume: float,
        sl: float,
        tp: float,
        *,
        comment: str = "borex_live",
        risk_money: float | None = None,
        rr: float | None = None,
    ) -> Mt5OrderResult:
        if self.dry_run:
            return Mt5OrderResult(ok=True, ticket=-2, message="dry_run market")
        mt5_sym = self.ensure_symbol(yahoo_symbol)
        mt5 = self._mt5
        tick = mt5.symbol_info_tick(mt5_sym)
        if tick is None:
            return Mt5OrderResult(ok=False, message=f"no tick for {mt5_sym}")
        if side == "buy":
            order_type = mt5.ORDER_TYPE_BUY
            price = float(tick.ask)
        else:
            order_type = mt5.ORDER_TYPE_SELL
            price = float(tick.bid)
        adj_sl, adj_tp = self._adjust_stops(
            mt5_sym,
            side,
            price,
            sl if sl else None,
            tp if tp else None,
        )
        # Always rebuild TP from the (possibly widened) SL so RR stays intact
        # and TP cannot land on the wrong side of the live fill.
        if adj_sl is not None and rr is not None and rr > 0:
            risk = abs(price - float(adj_sl))
            if risk > 0:
                if side.lower() == "buy":
                    adj_tp = self._normalize_price(mt5_sym, price + risk * float(rr))
                else:
                    adj_tp = self._normalize_price(mt5_sym, price - risk * float(rr))
            adj_sl, adj_tp = self._adjust_stops(mt5_sym, side, price, adj_sl, adj_tp)

        # Hard reject inverted protective levels (buy needs SL < price < TP).
        if adj_sl is not None and adj_tp is not None:
            if side.lower() == "buy" and not (adj_sl < price < adj_tp):
                return Mt5OrderResult(
                    ok=False,
                    message=f"invalid buy stops sl={adj_sl} entry={price} tp={adj_tp}",
                )
            if side.lower() == "sell" and not (adj_tp < price < adj_sl):
                return Mt5OrderResult(
                    ok=False,
                    message=f"invalid sell stops tp={adj_tp} entry={price} sl={adj_sl}",
                )

        if risk_money is not None and adj_sl is not None:
            vol = self.lots_for_risk(yahoo_symbol, side, price, float(adj_sl), float(risk_money))
            if vol <= 0:
                return Mt5OrderResult(
                    ok=False,
                    message=(
                        f"risk sizing produced 0 lots "
                        f"(risk={risk_money:.2f} sl_dist={abs(price - float(adj_sl)):.6f})"
                    ),
                )
        else:
            vol = self._normalize_volume(mt5_sym, volume)
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": mt5_sym,
            "volume": vol,
            "type": order_type,
            "price": price,
            "sl": adj_sl if adj_sl is not None else 0.0,
            "tp": adj_tp if adj_tp is not None else 0.0,
            "deviation": 20,
            "magic": self.MAGIC,
            "comment": (comment or "borex_live")[:31],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result is None:
            return Mt5OrderResult(ok=False, message=str(mt5.last_error()))
        ok = result.retcode == mt5.TRADE_RETCODE_DONE
        msg = str(result.comment)
        if ok:
            msg = f"{msg} vol={vol:.2f} risk={risk_money}" if risk_money is not None else f"{msg} vol={vol:.2f}"
        return Mt5OrderResult(
            ok=ok,
            ticket=int(result.order) or int(getattr(result, "deal", 0) or 0),
            message=msg,
            retcode=int(result.retcode),
        )

    def pending_orders(self) -> list[dict[str, Any]]:
        if self.dry_run:
            return []
        raw = self._mt5.orders_get()
        if raw is None:
            return []
        return [
            {
                "ticket": int(o.ticket),
                "symbol": str(o.symbol),
                "type": int(o.type),
                "price": float(o.price_open),
                "sl": float(o.sl),
                "tp": float(o.tp),
                "volume": float(o.volume_current),
                "magic": int(getattr(o, "magic", 0) or 0),
            }
            for o in raw
        ]

    def modify_position_sltp(self, ticket: int, sl: float, tp: float) -> Mt5OrderResult:
        if self.dry_run:
            return Mt5OrderResult(ok=True, ticket=ticket, message="dry_run modify")
        mt5 = self._mt5
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": int(ticket),
            "sl": float(sl),
            "tp": float(tp),
        }
        result = mt5.order_send(request)
        if result is None:
            return Mt5OrderResult(ok=False, message=str(mt5.last_error()))
        ok = result.retcode == mt5.TRADE_RETCODE_DONE
        return Mt5OrderResult(ok=ok, ticket=ticket, retcode=int(result.retcode))

    def cancel_order(self, ticket: int) -> Mt5OrderResult:
        if self.dry_run:
            return Mt5OrderResult(ok=True, ticket=ticket, message="dry_run cancel")
        mt5 = self._mt5
        request = {
            "action": mt5.TRADE_ACTION_REMOVE,
            "order": int(ticket),
        }
        result = mt5.order_send(request)
        if result is None:
            return Mt5OrderResult(ok=False, message=str(mt5.last_error()))
        ok = result.retcode == mt5.TRADE_RETCODE_DONE
        return Mt5OrderResult(ok=ok, ticket=ticket, retcode=int(result.retcode))
