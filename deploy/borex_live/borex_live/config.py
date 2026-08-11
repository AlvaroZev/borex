from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class LiveServiceConfig:
    strategy: str = "alexg7"
    demo: bool = True
    capital: float = 1000.0
    leverage: float = 5000.0
    rr_factor: float = 2.5
    # alexg7/8 backtests use 3.0; alexg5 historically used 2.0 + rr_factor.
    min_rr: float = 3.0
    position_size_pct: float = 0.01
    max_positions: int = 60
    interval: str = "1h"
    master_yahoo: str = "EURUSD=X"
    second_signal: str = "off"
    # alexg8: LTF fields kept for CLI compat; unused by current alexg8.
    ltf_intervals: tuple[str, ...] = ("1m",)
    ltf_confirm_mode: str = "any"
    ltf_warmup_bars: int = 500
    default_lot: float = 0.01
    dry_run: bool = False
    port: int = 8790
    host: str = "127.0.0.1"
    warmup_bars: int = 300
    database_url: str = ""
    # Optional Railway (or other) mirror; live never depends on this for trading.
    database_backup_url: str = ""
    backup_interval_seconds: int = 300
    mt5_path: str = ""
    mt5_login: int = 0
    mt5_password: str = ""
    mt5_server: str = ""
    symbols: list[str] = field(default_factory=list)
    borex_main_root: Path | None = None
    # False = do not evaluate SL/TP on the entry bar (matches backtest default).
    same_bar_exit: bool = False

    @classmethod
    def from_env(cls) -> LiveServiceConfig:
        db = os.environ.get("DATABASE_URL", "")
        backup = (
            os.environ.get("DATABASE_BACKUP_URL", "")
            or os.environ.get("RAILWAY_DATABASE_URL", "")
        )
        return cls(database_url=db, database_backup_url=backup)

    def mt5_credentials(self) -> tuple[int, str, str]:
        login = self.mt5_login or int(os.environ.get("MT5_LOGIN", "0") or 0)
        password = self.mt5_password or os.environ.get("MT5_PASSWORD", "")
        server = self.mt5_server or os.environ.get("MT5_SERVER", "")
        if self.demo and not server:
            server = os.environ.get("MT5_DEMO_SERVER", server)
        return login, password, server
