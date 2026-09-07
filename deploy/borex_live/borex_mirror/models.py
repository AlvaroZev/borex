from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


class MirrorPortfolio(Base):
    __tablename__ = "mirror_portfolio"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    cash: Mapped[float] = mapped_column(Float, default=0.0)
    initial_capital: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class MirrorTheoryState(Base):
    __tablename__ = "mirror_theory_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    strategy: Mapped[str] = mapped_column(String(32))
    config_hash: Mapped[str] = mapped_column(String(64), index=True)
    initial_capital: Mapped[float] = mapped_column(Float)
    cash: Mapped[float] = mapped_column(Float)
    equity: Mapped[float] = mapped_column(Float)
    last_master_ts: Mapped[str] = mapped_column(String(64), default="")
    state_blob: Mapped[bytes] = mapped_column(LargeBinary)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class MirrorTheoryTrade(Base):
    """Theory book for the mirror service (authority ledger)."""

    __tablename__ = "mirror_theory_trades"
    __table_args__ = (
        UniqueConstraint(
            "symbol",
            "entry_time",
            "pattern",
            name="uq_mirror_theory_trade_signal",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(24), index=True)
    side: Mapped[str] = mapped_column(String(8))
    pattern: Mapped[str] = mapped_column(Text, default="")
    entry_index: Mapped[int] = mapped_column(Integer)
    entry_price: Mapped[float] = mapped_column(Float)
    entry_time: Mapped[str] = mapped_column(String(64), index=True)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    margin: Mapped[float] = mapped_column(Float, default=0.0)
    rr_used: Mapped[float] = mapped_column(Float, default=0.0)
    commission: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    exit_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_time: Mapped[str | None] = mapped_column(String(64), nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    pnl: Mapped[float] = mapped_column(Float, default=0.0)


class MirrorTrade(Base):
    """Real (or paper) mirror of a theory trade — 1:1 by design."""

    __tablename__ = "mirror_trades"
    __table_args__ = (
        UniqueConstraint(
            "symbol",
            "entry_time",
            "pattern",
            name="uq_mirror_trade_signal",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    theory_trade_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(String(24), index=True)
    side: Mapped[str] = mapped_column(String(8))
    pattern: Mapped[str] = mapped_column(Text, default="")
    theory_entry: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float] = mapped_column(Float)
    entry_time: Mapped[str] = mapped_column(String(64), index=True)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    sl_pips: Mapped[float] = mapped_column(Float, default=0.0)
    tp_pips: Mapped[float] = mapped_column(Float, default=0.0)
    margin: Mapped[float] = mapped_column(Float, default=0.0)
    rr_used: Mapped[float] = mapped_column(Float, default=0.0)
    mt5_ticket: Mapped[int | None] = mapped_column(Integer, nullable=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_time: Mapped[str | None] = mapped_column(String(64), nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    pnl: Mapped[float] = mapped_column(Float, default=0.0)
    expected_win_usd: Mapped[float] = mapped_column(Float, default=0.0)
    expected_loss_usd: Mapped[float] = mapped_column(Float, default=0.0)
    paper: Mapped[bool] = mapped_column(Boolean, default=False)


class MirrorEvent(Base):
    __tablename__ = "mirror_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(48), index=True)
    message: Mapped[str] = mapped_column(Text)
    level: Mapped[str] = mapped_column(String(16), default="info")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


def make_engine(database_url: str):
    connect_args: dict = {}
    pool_args: dict = {}
    if database_url.startswith("postgresql"):
        connect_args = {
            "connect_timeout": 30,
            "keepalives": 1,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 5,
        }
        pool_args = {
            "pool_recycle": 180,
            "pool_size": 3,
            "max_overflow": 2,
            "pool_timeout": 30,
        }
    return create_engine(
        database_url,
        pool_pre_ping=True,
        connect_args=connect_args,
        **pool_args,
    )


def init_db(database_url: str) -> sessionmaker:
    if not database_url:
        database_url = "sqlite+pysqlite:///:memory:"
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)
