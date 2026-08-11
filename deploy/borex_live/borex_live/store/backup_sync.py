"""Mirror local (primary) live DB tables to Railway (backup).

Primary stays local Docker Postgres. Backup URL is optional; sync failures
must never crash the live trading loop.
"""

from __future__ import annotations

import logging
from typing import Iterable, Type

from sqlalchemy.orm import Session, make_transient

from borex_live.store.models import (
    BarCursor,
    Base,
    LiveTrade,
    PendingGhost,
    PortfolioState,
    ServiceEvent,
    ServiceRun,
    init_db,
)

logger = logging.getLogger(__name__)

# Core state for recovery / dashboard mirror. Skip LiveCandle (large, rebuildable).
BACKUP_MODELS: tuple[Type[Base], ...] = (
    PortfolioState,
    LiveTrade,
    PendingGhost,
    BarCursor,
    ServiceRun,
    ServiceEvent,
)


def _copy_table(local: Session, remote: Session, model: Type[Base]) -> int:
    """Replace remote rows with a snapshot of local rows (preserve PKs)."""
    rows = local.query(model).all()
    remote.query(model).delete()
    remote.flush()
    n = 0
    for row in rows:
        local.expunge(row)
        make_transient(row)
        remote.merge(row)
        n += 1
    return n


def sync_local_to_backup(local_url: str, backup_url: str) -> dict[str, int]:
    """Full replace-sync of BACKUP_MODELS from local → backup."""
    if not local_url or not backup_url:
        raise ValueError("local_url and backup_url are required")
    if local_url.strip() == backup_url.strip():
        raise ValueError("local and backup URLs must differ")

    Local = init_db(local_url)
    Backup = init_db(backup_url)
    counts: dict[str, int] = {}
    with Local() as ls, Backup() as bs:
        for model in BACKUP_MODELS:
            name = model.__tablename__
            try:
                counts[name] = _copy_table(ls, bs, model)
            except Exception:
                logger.exception("backup sync failed for table %s", name)
                bs.rollback()
                raise
        bs.commit()
    return counts


def sync_backup_to_local(backup_url: str, local_url: str) -> dict[str, int]:
    """One-shot seed: Railway → local (bootstrap a new machine)."""
    return sync_local_to_backup(backup_url, local_url)


def table_names() -> Iterable[str]:
    return (m.__tablename__ for m in BACKUP_MODELS)
