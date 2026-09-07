from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class MirrorConfig:
    """Config for the theory→MT5 mirror service."""

    strategy: str = "alexg9"
    demo: bool = True
    capital: float = 0.0  # 0 = fetch MT5 balance
    leverage: float = 5000.0
    min_rr: float = 3.0
    rr_factor: float = 1.0
    rr_mode: str = "fixed"
    position_size_pct: float = 0.01
    max_positions: int = 60
    interval: str = "1h"
    master_yahoo: str = "EURUSD=X"
    second_signal: str = "off"
    warmup_bars: int = 10_000
    catchup_bars: int = 500
    port: int = 8792
    host: str = "127.0.0.1"
    dry_run: bool = False
    # False = record mirror fills in DB without MT5 (bulk/parity)
    execute_mt5: bool = True
    same_bar_exit: bool = False
    commission_per_lot: float = 7.0
    min_commission_per_side: float = 0.04
    lot_notional: float = 100_000.0
    risk_include_commission: bool = False
    commission_at_entry: bool = True
    winrate_min_trades: int = 20
    rr_min: float = 0.0
    rr_max: float = 0.0
    force_flat_friday: bool = True
    force_flat_daily: bool = True
    force_flat_utc_hour: int = 19
    force_flat_friday_from_hour: int = 19
    database_url: str = ""
    database_backup_url: str = ""
    backup_interval_seconds: int = 300
    mt5_path: str = ""
    mt5_login: int = 0
    mt5_password: str = ""
    mt5_server: str = ""
    symbols: list[str] = field(default_factory=list)
    borex_main_root: Path | None = None
    # Distinct from live magic 88001
    mt5_magic: int = 88002

    @classmethod
    def from_env(cls) -> MirrorConfig:
        db = (
            os.environ.get("MIRROR_DATABASE_URL", "")
            or os.environ.get("DATABASE_URL", "")
        )
        backup = (
            os.environ.get("MIRROR_DATABASE_BACKUP_URL", "")
            or os.environ.get("DATABASE_BACKUP_URL", "")
            or os.environ.get("RAILWAY_DATABASE_URL", "")
        )
        return cls(database_url=db, database_backup_url=backup)

    def mt5_credentials(self) -> tuple[int, str, str]:
        login = self.mt5_login or int(
            os.environ.get("MIRROR_MT5_LOGIN")
            or os.environ.get("MT5_LOGIN", "0")
            or 0
        )
        password = self.mt5_password or (
            os.environ.get("MIRROR_MT5_PASSWORD")
            or os.environ.get("MT5_PASSWORD", "")
        )
        server = self.mt5_server or os.environ.get("MT5_SERVER", "")
        if self.demo and not server:
            server = (
                os.environ.get("MIRROR_MT5_DEMO_SERVER")
                or os.environ.get("MT5_DEMO_SERVER", server)
            )
        return login, password, server

    def to_live_cfg(self):
        """Adapt to LiveServiceConfig so ShadowEngine can reuse it."""
        from borex_live.config import LiveServiceConfig

        return LiveServiceConfig(
            strategy=self.strategy,
            demo=self.demo,
            capital=self.capital,
            leverage=self.leverage,
            rr_factor=self.rr_factor,
            min_rr=self.min_rr,
            position_size_pct=self.position_size_pct,
            max_positions=self.max_positions,
            interval=self.interval,
            master_yahoo=self.master_yahoo,
            second_signal=self.second_signal,
            warmup_bars=self.warmup_bars,
            catchup_bars=self.catchup_bars,
            port=self.port,
            host=self.host,
            dry_run=self.dry_run,
            same_bar_exit=self.same_bar_exit,
            commission_per_lot=self.commission_per_lot,
            min_commission_per_side=self.min_commission_per_side,
            lot_notional=self.lot_notional,
            risk_include_commission=self.risk_include_commission,
            winrate_min_trades=self.winrate_min_trades,
            rr_mode=self.rr_mode,
            rr_min=self.rr_min,
            rr_max=self.rr_max,
            commission_at_entry=self.commission_at_entry,
            force_flat_friday=self.force_flat_friday,
            force_flat_daily=self.force_flat_daily,
            force_flat_utc_hour=self.force_flat_utc_hour,
            force_flat_friday_from_hour=self.force_flat_friday_from_hour,
            database_url=self.database_url,
            mt5_path=self.mt5_path,
            mt5_login=self.mt5_login,
            mt5_password=self.mt5_password,
            mt5_server=self.mt5_server,
            symbols=list(self.symbols),
            borex_main_root=self.borex_main_root,
        )
