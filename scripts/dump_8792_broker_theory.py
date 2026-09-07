"""Parse MT5 reports + 8792 dashboard into one JSON for the 2-day compare."""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

import pandas as pd
import urllib.request

OUT = Path(__file__).resolve().parents[1] / "data" / "runs" / "mirror_2d_8792"
HTML_PATH = Path(r"c:\Users\azeva\OneDrive\Documentos\ReportHistory-53019882.html")
XLSX_PATH = Path(r"c:\Users\azeva\OneDrive\Documentos\ReportTrade-53019882.xlsx")
DASH = "http://127.0.0.1:8792/api/dashboard"


def _f(x) -> float:
    if x is None or x == "" or x == " ":
        return 0.0
    s = str(x).replace("\xa0", "").replace(" ", "").replace(",", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


class PositionsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] = []
        self._cell = ""
        self._in_td = False
        self._capture = False
        self.balance: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        if tag == "td":
            self._in_td = True
            self._cell = ""

    def handle_endtag(self, tag):
        if tag == "td" and self._in_td:
            self._row.append(self._cell.strip())
            self._in_td = False
        if tag == "tr" and self._row:
            self.rows.append(self._row)

    def handle_data(self, data):
        if self._in_td:
            self._cell += data


def parse_html_positions(path: Path) -> dict:
    raw = path.read_text(encoding="utf-16", errors="replace")
    if "<html" not in raw.lower()[:200] and "<html" not in raw.lower()[:2000]:
        raw = path.read_text(encoding="utf-8", errors="replace")
    p = PositionsParser()
    p.feed(raw)

    positions = []
    orders = []
    deals = []
    section = None
    for row in p.rows:
        joined = " ".join(row).strip()
        if joined == "Positions" or (len(row) == 1 and row[0] == "Positions"):
            section = "pos"
            continue
        if "Orders" == joined or (len(row) == 1 and row[0] == "Orders"):
            section = "ord"
            continue
        if joined.startswith("Deals") or (len(row) == 1 and row[0] == "Deals"):
            section = "deal"
            continue
        if not row or row[0] in ("Time", "") and "Position" in row:
            continue
        if row[0] in ("Time", "Open Time") or "Symbol" in row[:4] and row[0] == "Time":
            continue
        if row[0].startswith("Balance:") or "Balance:" in joined:
            continue

        # Position rows: Time, ticket, symbol, type, [comment hidden], vol, price, sl, tp, close_time, close_price, comm, swap, profit
        if section == "pos" and re.match(r"\d{4}\.\d{2}\.\d{2}", row[0] or ""):
            # After hidden comment, typical visible: time, pos, sym, type, vol, open, sl, tp, ctime, cprice, comm, swap, profit
            cells = [c for c in row if c != ""]
            # comment is in the raw row somewhere starting bx_
            comment = next((c for c in row if c.startswith("bx_")), "")
            # Find numeric ticket
            ticket = ""
            for c in row[1:4]:
                if re.fullmatch(r"\d+", c):
                    ticket = c
                    break
            # symbol is a forex code
            sym = next(
                (
                    c
                    for c in row
                    if re.fullmatch(r"[A-Z]{6,7}", c) or c.endswith("USD") or len(c) == 6 and c.isalpha()
                ),
                "",
            )
            side = next((c for c in row if c in ("buy", "sell")), "")
            # Last 8 numeric-ish after type
            nums = []
            started = False
            for c in row:
                if c in ("buy", "sell"):
                    started = True
                    continue
                if started and c.startswith("bx_"):
                    continue
                if started:
                    nums.append(c)
            # nums: vol, open, sl, tp, close_time?, close_price, comm, swap, profit  OR close_time is not number
            vol = open_px = sl = tp = close_px = comm = swap = profit = 0.0
            close_time = ""
            rest = [c for c in nums if c]
            # Pattern from known rows: vol, open, sl, tp, close_time, close_px, comm, swap, profit
            if len(rest) >= 9:
                vol = _f(rest[0])
                open_px = _f(rest[1])
                sl = _f(rest[2])
                tp = _f(rest[3])
                close_time = rest[4]
                close_px = _f(rest[5])
                comm = _f(rest[6])
                swap = _f(rest[7])
                profit = _f(rest[8])
            elif len(rest) >= 6:
                vol = _f(rest[0])
                open_px = _f(rest[1])
                close_time = rest[-5] if re.match(r"\d{4}", rest[-5] or "") else ""
                close_px = _f(rest[-4])
                comm = _f(rest[-3])
                swap = _f(rest[-2])
                profit = _f(rest[-1])
            positions.append(
                {
                    "open_time": row[0],
                    "ticket": ticket,
                    "symbol": sym,
                    "side": side,
                    "volume": vol,
                    "open": open_px,
                    "sl": sl,
                    "tp": tp,
                    "close_time": close_time,
                    "close": close_px,
                    "commission": comm,
                    "swap": swap,
                    "profit": profit,
                    "net": profit + comm + swap,
                    "comment": comment,
                    "probe": "probe" in comment.lower(),
                }
            )
        if "Total net profit" in joined or "Balance:" in joined or "Credit Facility" in joined:
            pass

    # Summary lines
    summary = {}
    for row in p.rows:
        j = " ".join(row)
        for key in (
            "Total Net Profit:",
            "Balance:",
            "Credit Facility:",
            "Floating P/L:",
            "Total Trades:",
            "Profit Trades:",
            "Loss Trades:",
            "Short Trades(won %):",
            "Long Trades(won %):",
            "Profit Factor:",
            "Expected Payoff:",
            "Sharpe Ratio:",
            "Recovery Factor:",
            "AHPR:",
            "GHPR:",
            "Gross Profit:",
            "Gross Loss:",
            "Initial Deposit:",
            "Withdrawal:",
            "Deposit:",
        ):
            if key.rstrip(":") in j or key in j:
                nums = re.findall(r"-?[\d,.]+", j.replace(key, "", 1))
                if nums:
                    summary[key.rstrip(":")] = nums[0]

    return {"positions": positions, "n_rows": len(p.rows), "summary_guess": summary}


def parse_xlsx(path: Path) -> dict:
    xl = pd.ExcelFile(path)
    out: dict = {"sheets": xl.sheet_names, "tables": {}}
    for name in xl.sheet_names:
        df = pd.read_excel(path, sheet_name=name, header=None)
        out["tables"][name] = {
            "shape": list(df.shape),
            "preview": df.head(25).fillna("").astype(str).values.tolist(),
        }
    return out


def fetch_dash() -> dict:
    try:
        with urllib.request.urlopen(DASH, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as exc:
        return {"error": str(exc)}


def summarize_positions(rows: list[dict]) -> dict:
    real = [r for r in rows if not r.get("probe")]
    nets = [r["net"] for r in real]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    by_day = Counter()
    pnl_day: dict[str, float] = {}
    for r in real:
        d = (r["open_time"] or "")[:10]
        by_day[d] += 1
        pnl_day[d] = pnl_day.get(d, 0.0) + r["net"]
    return {
        "n": len(real),
        "probes_excluded": sum(1 for r in rows if r.get("probe")),
        "wins": len(wins),
        "losses": len(losses),
        "be": sum(1 for x in nets if x == 0),
        "wr": (len(wins) / len(real) * 100) if real else 0,
        "gross_profit": round(sum(wins), 2),
        "gross_loss": round(sum(losses), 2),
        "profit": round(sum(r["profit"] for r in real), 2),
        "commission": round(sum(r["commission"] for r in real), 2),
        "swap": round(sum(r["swap"] for r in real), 2),
        "net": round(sum(nets), 2),
        "by_day_n": dict(by_day),
        "by_day_net": {k: round(v, 2) for k, v in pnl_day.items()},
        "symbols": dict(Counter(r["symbol"] for r in real)),
        "comments": dict(Counter(r["comment"][:18] for r in real)),
    }


def summarize_book(trades: list[dict], *, closed_only: bool = True) -> dict:
    rows = trades or []
    if closed_only:
        rows = [t for t in rows if str(t.get("status") or "closed") != "open"]
        if rows and all(t.get("status") is None for t in rows[:3]):
            # dashboard closed_trades already closed
            pass
    pnls = [float(t.get("pnl") or 0) for t in rows]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x < 0]
    return {
        "n": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "wr": (len(wins) / len(rows) * 100) if rows else 0,
        "net": round(sum(pnls), 2),
        "gross_profit": round(sum(wins), 2),
        "gross_loss": round(sum(losses), 2),
        "open": sum(1 for t in (trades or []) if t.get("status") == "open" or t.get("exit_reason") is None and t.get("pnl") in (None, 0) and not t.get("exit_time")),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    html = parse_html_positions(HTML_PATH)
    xlsx = parse_xlsx(XLSX_PATH)
    dash = fetch_dash()

    html_sum = summarize_positions(html["positions"])
    theory_closed = (dash.get("theory") or {}).get("closed_trades") or []
    theory_open = (dash.get("theory") or {}).get("open_trades") or []
    mirror_closed = dash.get("closed_trades") or []
    mirror_open = dash.get("open_trades") or []

    payload = {
        "html_summary": html_sum,
        "html_n_positions": len(html["positions"]),
        "xlsx_sheets": xlsx["sheets"],
        "xlsx_preview": {k: v["preview"][:20] for k, v in xlsx["tables"].items()},
        "xlsx_shape": {k: v["shape"] for k, v in xlsx["tables"].items()},
        "dash_ok": "error" not in dash,
        "dash_keys": list(dash.keys()) if isinstance(dash, dict) else [],
        "dash_error": dash.get("error"),
        "account": dash.get("account"),
        "cash": dash.get("cash"),
        "theory_summary_closed": summarize_book(theory_closed, closed_only=False),
        "theory_open_n": len(theory_open),
        "mirror_summary_closed": summarize_book(mirror_closed, closed_only=False),
        "mirror_open_n": len(mirror_open),
        "positions": html["positions"],
        "theory_closed": theory_closed,
        "theory_open": theory_open,
        "mirror_closed": mirror_closed,
        "mirror_open": mirror_open,
        "dash_raw_meta": {
            k: dash.get(k)
            for k in ("mode", "cash", "account")
            if isinstance(dash, dict)
        },
    }
    path = OUT / "broker_theory_dump.json"
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print("wrote", path)
    print("html", json.dumps(html_sum, indent=2))
    print("xlsx sheets", xlsx["sheets"], xlsx["tables"][xlsx["sheets"][0]]["shape"] if xlsx["sheets"] else None)
    print("dash", payload["dash_ok"], payload.get("dash_error"))
    print("theory closed", payload["theory_summary_closed"])
    print("mirror closed", payload["mirror_summary_closed"])
    print("open theory/mirror", payload["theory_open_n"], payload["mirror_open_n"])
    print("first 3 pos", html["positions"][:3])
    print("xlsx preview row0-8:")
    if xlsx["sheets"]:
        for i, row in enumerate(xlsx["tables"][xlsx["sheets"][0]]["preview"][:12]):
            print(i, row[:16])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
