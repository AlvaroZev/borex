from __future__ import annotations

import sys
from pathlib import Path


def borex_main_root() -> Path:
    """Path to strategy repo (borex package root). Override with BOREX_MAIN_ROOT."""
    import os

    env = os.environ.get("BOREX_MAIN_ROOT")
    if env:
        return Path(env).resolve()
    here = Path(__file__).resolve()
    # Vendored layout: <repo>/deploy/borex_live/borex_live/paths.py
    monorepo = here.parents[3]
    if (monorepo / "borex").is_dir():
        return monorepo
    # Standalone sibling layout: .../trading/borex_live → .../trading/borex-main
    return here.parents[2] / "borex-main"


def ensure_borex_main_on_path() -> Path:
    root = borex_main_root()
    root_s = str(root)
    if root_s not in sys.path:
        sys.path.insert(0, root_s)
    return root
