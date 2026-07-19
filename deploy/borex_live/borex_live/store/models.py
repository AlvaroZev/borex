from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


class ServiceRun(Base):
    __tablename__ = "service_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    strategy: Mapped[str] = mapped_column(String(32))
    entry_mode: Mapped[str] = mapped_column(String(16))
    config_json: Mapped[dict] = mapped_column(JSON)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class BarCursor(Base):
    __tablename__ = "bar_cursors"

    symbol: Mapped[str] = mapped_column(String(24), primary_key=True)
    last_ts: Mapped[str] = mapped_column(String(64))
    bar_index: Mapped[int] = mapped_column(Integer, default=0)


class PendingGhost(Base):
    __tablename__ = "pending_ghosts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(24), index=True)
    action: Mapped[str] = mapped_column(String(8))
    pattern: Mapped[str] = mapped_column(Text)
    stop_loss: Mapped[float] = mapped_column(Float)
    take_profit: Mapped[float] = mapped_column(Float)
    planned_entry: Mapped[float] = mapped_column(Float)
    created_index: Mapped[int] = mapped_column(Integer)
    expires_index: Mapped[int] = mapped_column(Integer)
    saw_near_sl: Mapped[bool] = mapped_column(Boolean, default=False)
    mt5_ticket: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="waiting")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class LiveTrade(Base):
    __tablename__ = "live_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(24), index=True)
    side: Mapped[str] = mapped_column(String(8))
    pattern: Mapped[str] = mapped_column(Text, default="")
    entry_price: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    margin: Mapped[float] = mapped_column(Float, default=0.0)
    rr_used: Mapped[float] = mapped_column(Float, default=0.0)
    mt5_ticket: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="open")
    entry_time: Mapped[str] = mapped_column(String(64), default="")
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_time: Mapped[str | None] = mapped_column(String(64), nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    pnl: Mapped[float] = mapped_column(Float, default=0.0)
    expected_win_usd: Mapped[float] = mapped_column(Float, default=0.0)
    expected_loss_usd: Mapped[float] = mapped_column(Float, default=0.0)


class PortfolioState(Base):
    __tablename__ = "portfolio_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    cash: Mapped[float] = mapped_column(Float)
    initial_capital: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class ServiceEvent(Base):
    __tablename__ = "service_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    level: Mapped[str] = mapped_column(String(16), default="info")
    kind: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class LiveCandle(Base):
    """Candles ingested live from MT5 after service start (excludes warmup/backdata)."""

    __tablename__ = "live_candles"
    __table_args__ = (UniqueConstraint("symbol", "ts", name="uq_live_candle_symbol_ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(24), index=True)
    ts: Mapped[str] = mapped_column(String(64), index=True)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float, default=0.0)
    interval: Mapped[str] = mapped_column(String(8), default="1h")
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


def make_engine(database_url: str):
    return create_engine(
        database_url,
        pool_pre_ping=True,
        pool_recycle=280,
    )


def init_db(database_url: str) -> sessionmaker:
    if not database_url:
        database_url = "sqlite+pysqlite:///:memory:"
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)
