from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class LiveServiceConfig:
    strategy: str = "alexg5"
    demo: bool = True
    capital: float = 1000.0
    leverage: float = 5000.0
    rr_factor: float = 2.5
    min_rr: float = 2.0
    position_size_pct: float = 0.01
    max_positions: int = 60
    interval: str = "1h"
    master_yahoo: str = "EURUSD=X"
    second_signal: str = "off"
    default_lot: float = 0.01
    dry_run: bool = False
    port: int = 8790
    host: str = "127.0.0.1"
    warmup_bars: int = 300
    database_url: str = ""
    mt5_path: str = ""
    mt5_login: int = 0
    mt5_password: str = ""
    mt5_server: str = ""
    symbols: list[str] = field(default_factory=list)
    borex_main_root: Path | None = None

    @classmethod
    def from_env(cls) -> LiveServiceConfig:
        db = os.environ.get("DATABASE_URL", "")
        return cls(database_url=db)

    def mt5_credentials(self) -> tuple[int, str, str]:
        login = self.mt5_login or int(os.environ.get("MT5_LOGIN", "0") or 0)
        password = self.mt5_password or os.environ.get("MT5_PASSWORD", "")
        server = self.mt5_server or os.environ.get("MT5_SERVER", "")
        if self.demo and not server:
            server = os.environ.get("MT5_DEMO_SERVER", server)
        return login, password, server
