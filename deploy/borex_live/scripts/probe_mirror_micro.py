"""One-off: 0.01 EURUSD round-trip on the MIRROR MT5 instance only."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from borex_live.mt5.client import Mt5Client

MIRROR_LOGIN = int(os.environ.get("MIRROR_MT5_LOGIN") or 0)
OG_LOGIN = int(os.environ.get("MT5_LOGIN") or 0)
PATH = os.environ.get("MIRROR_MT5_PATH") or ""
PASSWORD = os.environ.get("MIRROR_MT5_PASSWORD") or ""
SERVER = os.environ.get("MIRROR_MT5_DEMO_SERVER") or os.environ.get("MT5_DEMO_SERVER") or ""

if not MIRROR_LOGIN or not PATH:
    raise SystemExit("MIRROR_MT5_LOGIN / MIRROR_MT5_PATH missing")

client = Mt5Client(path=PATH, login=MIRROR_LOGIN, password=PASSWORD, server=SERVER)
client.MAGIC = 88002
client.connect()
info = client._mt5.account_info()
term = client._mt5.terminal_info()
login = int(info.login) if info else 0
term_path = str(getattr(term, "path", "") or "") if term else ""
print(f"connected login={login}")
print(f"expected_mirror={MIRROR_LOGIN} og={OG_LOGIN}")
print(f"server={getattr(info, 'server', '')}")
print(f"company={getattr(info, 'company', '')}")
print(f"terminal_path={term_path}")
print(f"configured_path={PATH}")
print(f"balance={float(info.balance):.2f} equity={float(info.equity):.2f}")
if login != MIRROR_LOGIN:
    raise SystemExit(f"ABORT: attached login {login} is not mirror {MIRROR_LOGIN}")
if login == OG_LOGIN:
    raise SystemExit("ABORT: this is the og account")

bal0 = float(info.balance)
opened = client.place_market_with_sltp(
    "EURUSD=X",
    "buy",
    0.01,
    0.0,
    0.0,
    comment="bx_m|probe",
)
print(f"open ok={opened.ok} ticket={opened.ticket} ret={opened.retcode} {opened.message}")
if not opened.ok:
    raise SystemExit("open failed")
closed = client.close_position("EURUSD=X", ticket=opened.ticket)
print(f"close ok={closed.ok} ticket={closed.ticket} ret={closed.retcode} {closed.message}")
info2 = client._mt5.account_info()
print(f"balance_after={float(info2.balance):.2f} equity_after={float(info2.equity):.2f} dbal={float(info2.balance) - bal0:.4f}")
# Do not mt5.shutdown() — leave the live mirror's terminal IPC alone.
client._connected = False
client._mt5 = None
if not closed.ok:
    raise SystemExit("close failed — check the second terminal for an open 0.01 EURUSD")
print("PROBE_OK")
