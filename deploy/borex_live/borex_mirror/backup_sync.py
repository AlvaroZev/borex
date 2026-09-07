"""Backup only mirror_* tables to Railway — never touches live/theory tables."""

from __future__ import annotations

import logging
from typing import Type

from sqlalchemy.orm import Session, make_transient

from borex_mirror.models import (
    Base,
    MirrorEvent,
    MirrorPortfolio,
    MirrorTheoryState,
    MirrorTheoryTrade,
    MirrorTrade,
    init_db,
)

logger = logging.getLogger(__name__)

BACKUP_MODELS: tuple[Type[Base], ...] = (
    MirrorPortfolio,
    MirrorTrade,
    MirrorTheoryState,
    MirrorTheoryTrade,
    MirrorEvent,
)


def _copy_table(local: Session, remote: Session, model: Type[Base]) -> int:
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
                logger.exception("mirror backup sync failed for table %s", name)
                bs.rollback()
                raise
        bs.commit()
    return counts
