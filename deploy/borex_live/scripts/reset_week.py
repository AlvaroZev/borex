#!/usr/bin/env python3
"""Reset live DB for a fresh week: wipe trades/ghosts/PnL, set cash.

Keeps bar_cursors so we do not re-process historical bars.
Does NOT close MT5 positions — flatten the terminal separately if needed.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from borex_live.store.models import (  # noqa: E402
    LiveTrade,
    PendingGhost,
    PortfolioState,
    ServiceEvent,
    ServiceRun,
    init_db,
)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cash", type=float, default=889.69, help="Starting cash / initial capital")
    p.add_argument("--db", default="", help="Override DATABASE_URL")
    p.add_argument("--yes", action="store_true", help="Skip confirmation")
    args = p.parse_args()

    url = args.db or os.environ.get("DATABASE_URL", "")
    if not url:
        print("ERROR: set DATABASE_URL or --db", file=sys.stderr)
        return 1

    if not args.yes:
        print(f"About to RESET live DB trades/ghosts and set cash={args.cash:.2f}")
        print("Bar cursors kept. MT5 positions NOT closed.")
        ans = input("Type YES to continue: ").strip()
        if ans != "YES":
            print("Aborted")
            return 1

    Session = init_db(url)
    with Session() as session:
        n_trades = session.query(LiveTrade).count()
        n_ghosts = session.query(PendingGhost).count()
        n_events = session.query(ServiceEvent).count()

        session.query(LiveTrade).delete()
        session.query(PendingGhost).delete()
        session.query(ServiceEvent).delete()
        for run in session.query(ServiceRun).filter(ServiceRun.active.is_(True)):
            run.active = False

        pf = session.get(PortfolioState, 1)
        if pf is None:
            pf = PortfolioState(id=1, cash=args.cash, initial_capital=args.cash)
            session.add(pf)
        else:
            pf.cash = args.cash
            pf.initial_capital = args.cash

        session.commit()

        pf2 = session.get(PortfolioState, 1)
        print(
            f"Reset OK | deleted trades={n_trades} ghosts={n_ghosts} events={n_events} | "
            f"cash={pf2.cash:.2f} initial_capital={pf2.initial_capital:.2f}"
        )
        # sanity
        left = session.execute(text("SELECT COUNT(*) FROM live_trades")).scalar()
        print(f"live_trades remaining={left}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
