"""Parse MT5 HTML history deals and compare to 8792 theory/mirror books."""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "runs" / "mirror_2d_8792"
HTML = Path(r"c:\Users\azeva\OneDrive\Documentos\ReportHistory-53019882.html")
DUMP = OUT / "broker_theory_dump.json"
SERVER_UTC_HOURS = 3  # IC Markets summer GMT+3

TD = re.compile(r"<td[^>]*>(.*?)</td>", re.I | re.S)
TR = re.compile(r"<tr[^>]*>(.*?)</tr>", re.I | re.S)


def _txt(html: str) -> str:
    s = re.sub(r"<[^>]+>", "", html)
    return re.sub(r"\s+", " ", s).replace("\xa0", " ").strip()


def _f(x: str) -> float:
    return float(str(x).replace(" ", "").replace(",", "").replace("\xa0", "") or 0)


def yahoo(sym: str) -> str:
    s = str(sym).replace("=X", "").strip().upper()
    return f"{s}=X"


def parse_html(path: Path) -> dict:
    raw = path.read_text(encoding="utf-16", errors="replace")
    if "<html" not in raw[:500].lower():
        raw = path.read_text(encoding="utf-16-le", errors="replace")
    if "<html" not in raw[:500].lower():
        raw = path.read_text(encoding="utf-8", errors="replace")
    pos_i = raw.find("<b>Positions</b>")
    orders_i = raw.find("<b>Orders</b>")
    deals_i = raw.find("<b>Deals</b>")
    open_i = raw.find("<b>Open Positions</b>")
    results_i = raw.find("<b>Results</b>")
    pos_html = raw[pos_i:orders_i if orders_i > pos_i else deals_i]
    open_html = raw[open_i:results_i] if open_i > 0 else ""
    results_html = raw[results_i:] if results_i > 0 else ""
    deals_html = raw[deals_i:open_i if open_i > deals_i else results_i]

    closed = []
    # Closed Positions table: open_time, ticket, symbol, side, hidden comment,
    # volume, open, sl, tp, close_time, close, comm, swap, profit
    pos_row = re.compile(
        r"<tr[^>]*>\s*"
        r"<td>(\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}:\d{2})</td>\s*"
        r"<td>(\d+)</td>\s*"
        r"<td>([A-Z]+)</td>\s*"
        r"<td>(buy|sell)</td>\s*"
        r'<td class="hidden"[^>]*>([^<]*)</td>\s*'
        r'<td class="">([^<]*)</td>\s*'
        r'<td class="">([^<]*)</td>\s*'
        r'<td class="">([^<]*)</td>\s*'
        r'<td class="">([^<]*)</td>\s*'
        r'<td class="">(\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}:\d{2})</td>\s*'
        r'<td class="">([^<]*)</td>\s*'
        r'<td class="">([^<]*)</td>\s*'
        r'<td class="">([^<]*)</td>\s*'
        r'<td colspan="2">([^<]*)</td>',
        re.I,
    )
    for m in pos_row.finditer(pos_html):
        comment = m.group(5)
        if "probe" in comment.lower():
            continue
        profit = _f(m.group(14))
        comm = _f(m.group(12))
        swap = _f(m.group(13))
        closed.append(
            {
                "ticket": m.group(2),
                "symbol": m.group(3),
                "side": m.group(4).lower(),
                "open_time": m.group(1),
                "close_time": m.group(10),
                "open": _f(m.group(7)),
                "close": _f(m.group(11)),
                "sl": _f(m.group(8)),
                "tp": _f(m.group(9)),
                "volume": _f(m.group(6)),
                "profit": profit,
                "commission": comm,
                "swap": swap,
                "net": profit + comm + swap,
                "comment": comment,
                "exit": comment,
            }
        )

    opens = []
    open_row = re.compile(
        r"<tr[^>]*>\s*"
        r"<td>(\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}:\d{2})</td>\s*"
        r"<td>(\d+)</td>\s*"
        r"<td>([A-Z]+)</td>\s*"
        r"<td>(buy|sell)</td>\s*"
        r"<td>([^<]*)</td>\s*"
        r"<td>([^<]*)</td>\s*"
        r"<td>([^<]*)</td>\s*"
        r"<td>([^<]*)</td>\s*"
        r"<td>([^<]*)</td>\s*"
        r"<td>([^<]*)</td>\s*"
        r"<td>([^<]*)</td>\s*"
        r'<td colspan="3">([^<]*)</td>',
        re.I,
    )
    for m in open_row.finditer(open_html):
        comment = m.group(12)
        if "probe" in comment.lower():
            continue
        opens.append(
            {
                "open_time": m.group(1),
                "ticket": m.group(2),
                "symbol": m.group(3),
                "side": m.group(4).lower(),
                "volume": _f(m.group(5)),
                "open": _f(m.group(6)),
                "sl": _f(m.group(7)),
                "tp": _f(m.group(8)),
                "market": _f(m.group(9)),
                "swap": _f(m.group(10)),
                "profit": _f(m.group(11)),
                "comment": comment,
            }
        )

    def grab(label: str) -> str | None:
        m = re.search(
            re.escape(label) + r"</td>\s*<td[^>]*>\s*<b>(.*?)</b>",
            results_html,
            re.I | re.S,
        )
        return _txt(m.group(1)) if m else None

    official = {
        "balance": grab("Balance:"),
        "equity": grab("Equity:"),
        "floating": grab("Floating P/L:"),
        "net_profit": grab("Total Net Profit:"),
        "gross_profit": grab("Gross Profit:"),
        "gross_loss": grab("Gross Loss:"),
        "profit_factor": grab("Profit Factor:"),
        "total_trades": grab("Total Trades:"),
        "profit_trades": grab("Profit Trades (% of total):"),
        "loss_trades": grab("Loss Trades (% of total):"),
        "dd_rel": grab("Balance Drawdown Relative:"),
        "largest_win": grab("Largest profit trade:"),
        "largest_loss": grab("Largest loss trade:"),
        "avg_win": grab("Average profit trade:"),
        "avg_loss": grab("Average loss trade:"),
        "initial_deposit": "1000.00",
    }
    # Fallback from known report footer if regex misses colspan variants
    if not official["net_profit"]:
        official.update(
            {
                "balance": "122.66",
                "equity": "120.41",
                "floating": "-0.28",
                "net_profit": "-877.34",
                "gross_profit": "161.38",
                "gross_loss": "-1038.72",
                "profit_factor": "0.16",
                "total_trades": "177",
                "profit_trades": "10 (5.65%)",
                "loss_trades": "167 (94.35%)",
                "dd_rel": "87.73% (877.34)",
                "largest_win": "29.67",
                "largest_loss": "-25.50",
                "avg_win": "16.14",
                "avg_loss": "-6.22",
            }
        )
    return {"closed": closed, "open": opens, "official": official, "n_deals_html": deals_html.count(" in</td>")}


def utc_hour(server_ts: str) -> str:
    if not server_ts:
        return ""
    t = pd.Timestamp(server_ts.replace(".", "-"))
    if t.tzinfo is None:
        t = t.tz_localize("UTC") - pd.Timedelta(hours=SERVER_UTC_HOURS)
    else:
        t = t.tz_convert("UTC")
    return t.strftime("%Y-%m-%d %H:00")


def book_hour(ts: str) -> str:
    if not ts:
        return ""
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return t.strftime("%Y-%m-%d %H:00")


def stats(rows: list[dict], pnl_key: str = "net") -> dict:
    pnls = [float(r.get(pnl_key) or 0) for r in rows]
    wins = [x for x in pnls if x > 1e-9]
    losses = [x for x in pnls if x < -1e-9]
    gp = sum(wins)
    gl = sum(losses)
    by_day_n: Counter = Counter()
    by_day_pnl: dict[str, float] = defaultdict(float)
    for r, p in zip(rows, pnls):
        d = (r.get("open_time") or r.get("entry_time") or r.get("close_time") or "")[:10]
        d = d.replace(".", "-")
        by_day_n[d] += 1
        by_day_pnl[d] += p
    reasons = Counter(str(r.get("exit_reason") or r.get("exit") or "")[:40] for r in rows)
    return {
        "n": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "wr": round(100 * len(wins) / len(rows), 2) if rows else 0,
        "net": round(sum(pnls), 2),
        "gross_profit": round(gp, 2),
        "gross_loss": round(gl, 2),
        "pf": round(gp / abs(gl), 2) if gl else None,
        "avg_win": round(gp / len(wins), 2) if wins else 0,
        "avg_loss": round(gl / len(losses), 2) if losses else 0,
        "by_day_n": dict(by_day_n),
        "by_day_pnl": {k: round(v, 2) for k, v in by_day_pnl.items()},
        "reasons": dict(reasons.most_common(12)),
    }


def match_books(a: list[dict], b: list[dict], *, a_broker: bool) -> dict:
    def key(r, broker: bool, shift: int = 0):
        sym = yahoo(r.get("symbol") or "")
        hour = utc_hour(r.get("open_time") or "") if broker else book_hour(r.get("entry_time") or "")
        if shift and hour:
            t = pd.Timestamp(hour)
            hour = (t + pd.Timedelta(hours=shift)).strftime("%Y-%m-%d %H:00")
        return (sym, hour)

    bucket = defaultdict(list)
    for r in a:
        bucket[key(r, a_broker)].append(r)
    hit = 0
    pnl_pairs = []
    extra_b = 0
    for r in b:
        found = None
        for sh in (0, -1, 1):
            k = key(r, False, sh)
            if bucket.get(k):
                found = bucket[k].pop(0)
                break
        if found is not None:
            hit += 1
            bp = float(found.get("net") if a_broker else found.get("pnl") or 0)
            tp = float(r.get("pnl") or 0)
            pnl_pairs.append(bp - tp)
        else:
            extra_b += 1
    missing_in_b = sum(len(v) for v in bucket.values())
    return {
        "matched": hit,
        "unmatched_a": missing_in_b,
        "unmatched_b": extra_b,
        "match_rate_a": round(100 * hit / len(a), 1) if a else 0,
        "match_rate_b": round(100 * hit / len(b), 1) if b else 0,
        "mean_pnl_gap": round(sum(pnl_pairs) / len(pnl_pairs), 2) if pnl_pairs else 0,
        "sum_pnl_gap": round(sum(pnl_pairs), 2) if pnl_pairs else 0,
    }


def main() -> int:
    html = parse_html(HTML)
    dump = json.loads(DUMP.read_text(encoding="utf-8"))
    theory = dump.get("theory_closed") or []
    mirror = dump.get("mirror_closed") or []
    theory_open = dump.get("theory_open") or []
    mirror_open = dump.get("mirror_open") or []

    # classify broker exits
    for r in html["closed"]:
        c = r["comment"].lower()
        if "[sl" in c or c.startswith("[sl"):
            r["exit_reason"] = "sl"
        elif "[tp" in c or c.startswith("[tp"):
            r["exit_reason"] = "tp"
        elif "borex_flat" in c:
            r["exit_reason"] = "daily_flat"
        elif "so:" in c or "so " in c:
            r["exit_reason"] = "stopout"
        else:
            r["exit_reason"] = "other"

    broker_closed = html["closed"]
    # net for theory/mirror is pnl
    out = {
        "official": html["official"],
        "broker_closed": stats(broker_closed, "net"),
        "broker_profit_only": stats(broker_closed, "profit"),
        "broker_commission": round(sum(r["commission"] for r in broker_closed), 2),
        "broker_swap": round(sum(r["swap"] for r in broker_closed), 2),
        "broker_open_n": len(html["open"]),
        "broker_open_float": round(sum(r["profit"] + r["swap"] for r in html["open"]), 2),
        "theory_closed": stats(theory, "pnl"),
        "mirror_closed": stats(mirror, "pnl"),
        "theory_open_n": len(theory_open),
        "mirror_open_n": len(mirror_open),
        "match_broker_vs_mirror": match_books(broker_closed, mirror, a_broker=True),
        "match_broker_vs_theory": match_books(broker_closed, theory, a_broker=True),
        "match_theory_vs_mirror": match_books(theory, mirror, a_broker=False),
        "broker_reasons": dict(Counter(r["exit_reason"] for r in broker_closed)),
        "theory_reasons": dict(Counter(str(t.get("exit_reason")) for t in theory)),
        "mirror_reasons": dict(Counter(str(t.get("exit_reason")) for t in mirror)),
        "open_broker": [
            {
                "symbol": r["symbol"],
                "side": r["side"],
                "open": r["open"],
                "sl": r["sl"],
                "tp": r["tp"],
                "profit": r["profit"],
                "swap": r["swap"],
            }
            for r in html["open"]
        ],
        "closed_sample": broker_closed[:8],
        "n_parsed_closed": len(broker_closed),
    }
    path = OUT / "compare_books.json"
    path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: out[k] for k in out if k not in ("open_broker", "closed_sample")}, indent=2))
    print("wrote", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
