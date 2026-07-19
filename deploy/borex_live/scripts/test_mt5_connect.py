"""Official MetaTrader5 Python connect smoke test.

Docs: https://www.mql5.com/en/docs/python_metatrader5
initialize: https://www.mql5.com/en/docs/python_metatrader5/mt5initialize_py
login: https://www.mql5.com/en/docs/python_metatrader5/mt5login_py

Required: MT5 terminal open and already logged into the demo in the UI.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
import MetaTrader5 as mt5

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
load_dotenv(Path.cwd() / ".env", override=False)

path = os.environ.get("MT5_PATH", r"C:\Program Files\MetaTrader 5\terminal64.exe")
login = int(os.environ.get("MT5_LOGIN", "0") or 0)
password = os.environ.get("MT5_PASSWORD", "")
server = os.environ.get("MT5_DEMO_SERVER", "") or os.environ.get("MT5_SERVER", "")

print("MetaTrader5 package author:", mt5.__author__)
print("MetaTrader5 package version:", mt5.__version__)
print("python", sys.version.split()[0])
print("path", path)
print("env login", login, "server", server)

# Official call options (in order):
# 1) initialize()            — auto-find terminal
# 2) initialize(path)        — specific EXE
# 3) initialize(..., login)  — + account params
# 4) initialize() then login()

attempts = [
    ("initialize()", lambda: mt5.initialize()),
    ("initialize(path)", lambda: mt5.initialize(path)),
    (
        "initialize(path, timeout, portable)",
        lambda: mt5.initialize(path=path, timeout=60_000, portable=False),
    ),
]

ok = False
for name, fn in attempts:
    print(f"try: {name}...")
    ok = bool(fn())
    print(" ", ok, mt5.last_error())
    if ok:
        break
    mt5.shutdown()

if ok and mt5.account_info() is None and login:
    print("try: login() after initialize...")
    ok = bool(mt5.login(login, password=password, server=server))
    print(" ", ok, mt5.last_error())

if not ok and login and password:
    print("try: initialize(path, login, password, server)...")
    ok = bool(
        mt5.initialize(
            path=path,
            login=login,
            password=password,
            server=server,
            timeout=60_000,
            portable=False,
        )
    )
    print(" ", ok, mt5.last_error())

if not ok:
    err = mt5.last_error()
    print("FAILED", err)
    if err and err[0] == -6:
        print(
            "Authorization failed (-6): log into MT5 in the UI first "
            "(title must show account + server). "
            "Or fix master password / exact server name in .env. "
            "Docs note: if password omitted, MT5 uses the saved terminal password."
        )
    raise SystemExit(1)

print("terminal_info:", mt5.terminal_info())
print("version:", mt5.version())
acc = mt5.account_info()
print("account", acc.login, acc.server, "balance", acc.balance)
print("trade_allowed", mt5.terminal_info().trade_allowed)
mt5.shutdown()
print("SUCCESS")
