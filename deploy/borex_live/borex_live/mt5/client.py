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
        Connect per official MetaTrader5 Python API.
        https://www.mql5.com/en/docs/python_metatrader5/mt5initialize_py
        """
        if self.dry_run:
            self._connected = True
            return
        import MetaTrader5 as mt5

        self._mt5 = mt5
        path = self.path or r"C:\Program Files\MetaTrader 5\terminal64.exe"

        # Official options: bare initialize → path → credentials
        ok = bool(mt5.initialize())
        if not ok:
            mt5.shutdown()
            ok = bool(mt5.initialize(path))
        if not ok:
            mt5.shutdown()
            ok = bool(mt5.initialize(path=path, timeout=60_000, portable=False))

        if ok and mt5.account_info() is None and self.login and self.password:
            ok = bool(
                mt5.login(int(self.login), password=self.password, server=self.server)
            )

        if not ok and self.login and self.password:
            mt5.shutdown()
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
        if self._mt5 is not None and not self.dry_run:
            self._mt5.shutdown()
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
                return self.place_market_with_sltp(
                    yahoo_symbol,
                    "buy",
                    volume,
                    sl if sl is not None else 0.0,
                    tp if tp is not None else 0.0,
                    comment=comment[:31],
                )
            order_type = mt5.ORDER_TYPE_BUY_LIMIT
        else:
            if entry <= bid:
                return self.place_market_with_sltp(
                    yahoo_symbol,
                    "sell",
                    volume,
                    sl if sl is not None else 0.0,
                    tp if tp is not None else 0.0,
                    comment=comment[:31],
                )
            order_type = mt5.ORDER_TYPE_SELL_LIMIT

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
        if sl is not None and sl > 0:
            request["sl"] = self._normalize_price(mt5_sym, sl)
        if tp is not None and tp > 0:
            request["tp"] = self._normalize_price(mt5_sym, tp)

        check = mt5.order_check(request)
        if check is not None and check.retcode not in (0, mt5.TRADE_RETCODE_DONE):
            # still try send — some builds only populate comment on check
            pass

        result = mt5.order_send(request)
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
        vol = self._normalize_volume(mt5_sym, volume)
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": mt5_sym,
            "volume": vol,
            "type": order_type,
            "price": price,
            "sl": self._normalize_price(mt5_sym, sl) if sl else 0.0,
            "tp": self._normalize_price(mt5_sym, tp) if tp else 0.0,
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
        return Mt5OrderResult(
            ok=ok,
            ticket=int(result.order) or int(getattr(result, "deal", 0) or 0),
            message=str(result.comment),
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
