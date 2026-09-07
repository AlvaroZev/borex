"""Build a one-row-per-idea compare of broker vs theory vs H1 backtest."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "runs" / "mirror_2d_8792"
CANVAS = Path(
    r"C:\Users\azeva\.cursor\projects\c-Users-azeva-OneDrive-Documentos-work-trading-borex-main"
    r"\canvases\week-real-theory-h1.canvas.tsx"
)
HTML = Path(r"c:\Users\azeva\OneDrive\Documentos\ReportHistory-53019882.html")
SERVER_UTC_HOURS = 3

sys.path.insert(0, str(ROOT / "scripts"))
from compare_8792_books import parse_html, utc_hour, book_hour, yahoo  # noqa: E402


def _side(x: str) -> str:
    s = str(x or "").lower()
    if s in ("buy", "long"):
        return "long"
    return "short"


def _sym(x: str) -> str:
    return yahoo(x).replace("=X", "")


def _short_why(x: str) -> str:
    w = str(x or "").lower()
    if "margin" in w or w in ("sl", "stop_loss", "stopout") or "dollar_sl" in w:
        return "sl"
    if "take_profit" in w or w == "tp":
        return "tp"
    if "daily" in w or "flat" in w:
        return "daily"
    if "end_of" in w:
        return "eod"
    if "mt5_closed" in w:
        return "sl"
    return (w or "—")[:12]


def _hhmm(ts: str, *, broker: bool) -> str:
    if broker:
        h = utc_hour(ts)
    else:
        h = book_hour(ts)
    return h[5:].replace("-", "-") if h else ""  # MM-DD HH:00


def _day(ts: str, *, broker: bool) -> str:
    if broker:
        h = utc_hour(ts)
    else:
        h = book_hour(ts)
    return h[:10] if h else ""


def _hour_key(ts: str, *, broker: bool) -> str:
    if broker:
        return utc_hour(ts)
    return book_hour(ts)


def _leg(row: dict, *, broker: bool) -> dict:
    if broker:
        et, xt = row.get("open_time") or "", row.get("close_time") or ""
        pnl = float(row.get("net") or 0)
        why = _short_why(row.get("exit_reason") or row.get("exit") or "")
        en, ex = float(row.get("open") or 0), float(row.get("close") or 0)
        sl, tp = float(row.get("sl") or 0), float(row.get("tp") or 0)
    else:
        et, xt = row.get("entry_time") or "", row.get("exit_time") or ""
        pnl = float(row.get("pnl") or 0)
        why = _short_why(row.get("exit_reason") or "")
        en = float(row.get("entry") or row.get("entry_price") or 0)
        ex = float(row.get("exit") or row.get("exit_price") or 0)
        sl = float(row.get("sl") or row.get("stop_loss") or 0)
        tp = float(row.get("tp") or row.get("take_profit") or 0)
    return {
        "et": _hhmm(et, broker=broker),
        "xt": _hhmm(xt, broker=broker),
        "en": round(en, 5),
        "ex": round(ex, 5),
        "sl": round(sl, 5),
        "tp": round(tp, 5),
        "pnl": round(pnl, 2),
        "why": why,
    }


def _wrap(row: dict, *, broker: bool) -> dict:
    et = row.get("open_time") if broker else row.get("entry_time")
    return {
        "sym": _sym(row.get("symbol") or ""),
        "side": _side(row.get("side") or ""),
        "hour": _hour_key(str(et or ""), broker=broker),
        "day": _day(str(et or ""), broker=broker),
        "leg": _leg(row, broker=broker),
    }


def _shift_hour(hour: str, hours: int) -> str:
    if not hour:
        return ""
    t = datetime.strptime(hour, "%Y-%m-%d %H:%M")
    return (t + timedelta(hours=hours)).strftime("%Y-%m-%d %H:00")


def _take(pool: list[dict], sym: str, side: str, hour: str) -> tuple[dict | None, int]:
    for require_side in (True, False):
        for sh in (0, -1, 1):
            want = _shift_hour(hour, sh)
            for i, row in enumerate(pool):
                if row["sym"] != sym:
                    continue
                if require_side and row["side"] != side:
                    continue
                if row["hour"] == want:
                    return pool.pop(i), sh
    return None, 0


def _row(sym, side, day, hour, r=None, t=None, h=None, shift=0) -> dict:
    books = "".join(
        ch
        for ch, ok in (("R", r), ("T", t), ("H", h))
        if ok
    )
    rp = r["pnl"] if r else None
    tp_ = t["pnl"] if t else None
    hp = h["pnl"] if h else None
    rw = r["why"] if r else ""
    tw = t["why"] if t else ""
    hw = h["why"] if h else ""
    disagree = False
    if tw and hw and tw != hw:
        disagree = True
    if rw and tw and rw != tw and rw != "—":
        disagree = True
    return {
        "day": day,
        "hour": (hour or "")[11:16],
        "sym": sym,
        "side": side,
        "books": books,
        "shift": shift,
        "disagree": disagree,
        "r": r,
        "t": t,
        "h": h,
        "rp": rp,
        "tp": tp_,
        "hp": hp,
    }


def build() -> dict:
    html = parse_html(HTML)
    dump = json.loads((OUT / "broker_theory_dump.json").read_text(encoding="utf-8"))
    bt = json.loads((OUT / "backtest.json").read_text(encoding="utf-8"))
    for r in html["closed"]:
        c = (r.get("comment") or "").lower()
        if "[sl" in c or c.startswith("[sl"):
            r["exit_reason"] = "sl"
        elif "[tp" in c or c.startswith("[tp"):
            r["exit_reason"] = "tp"
        elif "borex_flat" in c:
            r["exit_reason"] = "daily"
        else:
            r["exit_reason"] = r.get("exit_reason") or "other"

    broker = [_wrap(r, broker=True) for r in html["closed"]]
    theory = [_wrap(r, broker=False) for r in dump.get("theory_closed") or []]
    h1 = [_wrap(r, broker=False) for r in bt.get("trades") or []]

    rows: list[dict] = []
    # Spine: each broker close, attach theory then H1.
    for b in broker:
        th, sh_t = _take(theory, b["sym"], b["side"], b["hour"])
        hh, sh_h = _take(h1, b["sym"], b["side"], b["hour"])
        rows.append(
            _row(
                b["sym"],
                b["side"],
                b["day"],
                b["hour"],
                r=b["leg"],
                t=th["leg"] if th else None,
                h=hh["leg"] if hh else None,
                shift=sh_h or sh_t,
            )
        )
    for th in list(theory):
        hh, sh_h = _take(h1, th["sym"], th["side"], th["hour"])
        theory.remove(th)
        rows.append(
            _row(
                th["sym"],
                th["side"],
                th["day"],
                th["hour"],
                t=th["leg"],
                h=hh["leg"] if hh else None,
                shift=sh_h,
            )
        )
    for hh in h1:
        rows.append(
            _row(hh["sym"], hh["side"], hh["day"], hh["hour"], h=hh["leg"])
        )

    def sort_key(r):
        return (r["day"], r["hour"], r["sym"], r["side"])

    rows.sort(key=sort_key)
    for i, r in enumerate(rows):
        r["id"] = i
    n3 = sum(1 for r in rows if r["books"] == "RTH")
    n_rt = sum(1 for r in rows if "R" in r["books"] and "T" in r["books"])
    n_rh = sum(1 for r in rows if "R" in r["books"] and "H" in r["books"])
    return {
        "window": "2026-08-27 → 2026-08-28 UTC",
        "n_broker": len(html["closed"]),
        "n_theory": len(dump.get("theory_closed") or []),
        "n_h1": len(bt.get("trades") or []),
        "n_rows": len(rows),
        "n_triple": n3,
        "n_real_theory": n_rt,
        "n_real_h1": n_rh,
        "rows": rows,
    }


CANVAS_HEAD = r'''import {
  Button,
  Callout,
  Card,
  CardBody,
  CardHeader,
  Divider,
  Grid,
  H1,
  H2,
  Pill,
  Row,
  Select,
  Stack,
  Stat,
  Table,
  Text,
  TextInput,
  useCanvasState,
} from "cursor/canvas";

type Leg = {
  et: string;
  xt: string;
  en: number;
  ex: number;
  sl: number;
  tp: number;
  pnl: number;
  why: string;
};

type CmpRow = {
  id: number;
  day: string;
  hour: string;
  sym: string;
  side: string;
  books: string;
  shift: number;
  disagree: boolean;
  r: Leg | null;
  t: Leg | null;
  h: Leg | null;
  rp: number | null;
  tp: number | null;
  hp: number | null;
};

const META = '''

CANVAS_MID = r''';

const ROWS: CmpRow[] = '''

CANVAS_TAIL = r''';

function money(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  const sign = n > 0 ? "+" : "";
  return `${sign}$${n.toFixed(2)}`;
}

function tonePnl(n: number | null | undefined): "success" | "danger" | undefined {
  if (n === null || n === undefined) return undefined;
  if (n > 0.05) return "success";
  if (n < -0.05) return "danger";
  return undefined;
}

function booksLabel(b: string): string {
  const parts = [];
  if (b.includes("R")) parts.push("Real");
  if (b.includes("T")) parts.push("Theory");
  if (b.includes("H")) parts.push("H1");
  return parts.join(" + ") || "—";
}

function whyDiff(row: CmpRow): string {
  const bits: string[] = [];
  if (!row.r) bits.push("no broker fill");
  if (!row.t) bits.push("no theory");
  if (!row.h) bits.push("no H1");
  const rw = row.r?.why;
  const tw = row.t?.why;
  const hw = row.h?.why;
  if (rw && tw && rw !== tw) bits.push(`exit ${rw} vs theory ${tw}`);
  if (tw && hw && tw !== hw) bits.push(`theory ${tw} vs H1 ${hw}`);
  if (row.shift) bits.push(`hour ±${row.shift}`);
  if (row.r && row.t && Math.abs((row.rp ?? 0) - (row.tp ?? 0)) > 2) {
    bits.push(`PnL gap $${((row.rp ?? 0) - (row.tp ?? 0)).toFixed(1)}`);
  }
  return bits.join(" · ") || "same idea";
}

function LegCard({
  title,
  leg,
}: {
  title: string;
  leg: Leg | null;
}) {
  if (!leg) {
    return (
      <Card>
        <CardHeader>{title}</CardHeader>
        <CardBody>
          <Text tone="secondary">This book has no fill for this pair/hour.</Text>
        </CardBody>
      </Card>
    );
  }
  return (
    <Card>
      <CardHeader trailing={<Pill size="sm">{leg.why}</Pill>}>{title}</CardHeader>
      <CardBody>
        <Stack gap={8}>
          <Stat value={money(leg.pnl)} label="PnL" tone={tonePnl(leg.pnl)} />
          <Text size="small">
            In {leg.et} → out {leg.xt}
          </Text>
          <Text size="small" tone="secondary">
            Fill {leg.en} → {leg.ex}
          </Text>
          <Text size="small" tone="secondary">
            SL {leg.sl} · TP {leg.tp}
          </Text>
        </Stack>
      </CardBody>
    </Card>
  );
}

export default function WeekRealTheoryH1() {
  const [day, setDay] = useCanvasState("day", "all");
  const [view, setView] = useCanvasState("view", "all");
  const [q, setQ] = useCanvasState("q", "");
  const [sel, setSel] = useCanvasState("sel", 0);

  const filtered = ROWS.filter((r) => {
    if (day !== "all" && r.day !== day) return false;
    const qq = q.trim().toUpperCase();
    if (qq && !r.sym.includes(qq) && !r.side.startsWith(qq.toLowerCase())) {
      return false;
    }
    if (view === "triple" && r.books !== "RTH") return false;
    if (view === "rt" && !(r.books.includes("R") && r.books.includes("T"))) return false;
    if (view === "missingH1" && r.books.includes("R") && r.books.includes("T") && !r.books.includes("H")) {
      return true;
    }
    if (view === "missingH1") return false;
    if (view === "onlyH1" && r.books !== "H") return false;
    if (view === "disagree" && !r.disagree) return false;
    return true;
  });

  const shown = filtered.length > 0 ? filtered : ROWS;
  const idx = Math.min(Math.max(0, sel), shown.length - 1);
  const cur = shown[idx];

  return (
    <Stack gap={20}>
      <Stack gap={6}>
        <H1>Week trades: real vs theory vs H1</H1>
        <Text tone="secondary">
          Account 53019882 · {META.window} · match = same pair + UTC hour
          (±1h). Broker times converted GMT+3 → UTC. H1 is the aligned
          offline replay with spread; SL wins if the hour tags both sides.
        </Text>
      </Stack>

      <Grid columns={4} gap={12}>
        <Stat value={String(META.n_broker)} label="Broker closes" />
        <Stat value={String(META.n_theory)} label="Theory closes" />
        <Stat value={String(META.n_h1)} label="H1 closes" />
        <Stat
          value={String(META.n_triple)}
          label="In all three books"
          tone="info"
        />
      </Grid>

      <Callout tone="info" title="Same trade means same pair and hour">
        Real and theory usually line up (same live signal). H1 often does
        not — different ghost occupancy — so many rows are Real+Theory with
        a blank H1, or H1-only. Open a row to see entry, SL/TP, exit, and
        dollar gap.
      </Callout>

      <Row gap={10} align="center" wrap>
        <Select
          value={day}
          onChange={(v) => {
            setDay(v);
            setSel(0);
          }}
          options={[
            { value: "all", label: "Both days" },
            { value: "2026-08-27", label: "Thu 27 Aug" },
            { value: "2026-08-28", label: "Fri 28 Aug" },
          ]}
        />
        <Select
          value={view}
          onChange={(v) => {
            setView(v);
            setSel(0);
          }}
          options={[
            { value: "all", label: "All rows" },
            { value: "triple", label: "In all 3 books" },
            { value: "rt", label: "Real + theory" },
            { value: "missingH1", label: "Real+theory, no H1" },
            { value: "onlyH1", label: "H1 only" },
            { value: "disagree", label: "Exit disagrees" },
          ]}
        />
        <TextInput value={q} onChange={setQ} placeholder="Pair (EURUSD)" />
        <Text size="small" tone="secondary">
          {shown.length} rows
        </Text>
      </Row>

      <H2>
        {cur.sym} {cur.side} · {cur.day} {cur.hour} UTC
      </H2>
      <Text size="small" tone="secondary">
        {booksLabel(cur.books)} · {whyDiff(cur)}
      </Text>
      <Row gap={8} align="center">
        <Button
          variant="secondary"
          onClick={() => setSel(Math.max(0, idx - 1))}
          disabled={idx <= 0}
        >
          Previous
        </Button>
        <Text size="small">
          {idx + 1} / {shown.length}
        </Text>
        <Button
          variant="secondary"
          onClick={() => setSel(Math.min(shown.length - 1, idx + 1))}
          disabled={idx >= shown.length - 1}
        >
          Next
        </Button>
      </Row>

      <Grid columns={3} gap={12}>
        <LegCard title="Real (broker)" leg={cur.r} />
        <LegCard title="Theory (live paper)" leg={cur.t} />
        <LegCard title="H1 backtest" leg={cur.h} />
      </Grid>

      <Divider />
      <H2>All matched ideas this week</H2>
      <Text size="small" tone="secondary">
        Click Open on a row. Source: ReportHistory-53019882 · 8792 theory ·
        data/runs/mirror_2d_8792/backtest.json.
      </Text>
      <Table
        headers={[
          "",
          "UTC",
          "Pair",
          "Side",
          "Books",
          "Real $",
          "Theory $",
          "H1 $",
          "Diff",
        ]}
        columnAlign={[
          "left",
          "left",
          "left",
          "left",
          "left",
          "right",
          "right",
          "right",
          "left",
        ]}
        striped
        stickyHeader
        style={{ maxHeight: 480 }}
        rowTone={shown.map((r) => {
          if (r.books === "RTH") return r.disagree ? "warning" : "info";
          if (r.disagree) return "warning";
          if (!r.r) return "neutral";
          return undefined;
        })}
        rows={shown.map((r, i) => [
          <Button
            variant={i === idx ? "primary" : "ghost"}
            onClick={() => setSel(i)}
          >
            Open
          </Button>,
          `${r.day.slice(8)} ${r.hour}`,
          r.sym,
          r.side,
          r.books,
          money(r.rp),
          money(r.tp),
          money(r.hp),
          whyDiff(r),
        ])}
      />
    </Stack>
  );
}
'''


def write_canvas(bundle: dict) -> None:
    meta = {k: bundle[k] for k in bundle if k != "rows"}
    body = (
        CANVAS_HEAD
        + json.dumps(meta, separators=(",", ":"))
        + CANVAS_MID
        + json.dumps(bundle["rows"], separators=(",", ":"))
        + CANVAS_TAIL
    )
    CANVAS.write_text(body, encoding="utf-8")


def main() -> int:
    bundle = build()
    path = OUT / "trade_compare.json"
    path.write_text(json.dumps(bundle, indent=2), encoding="utf-8")
    write_canvas(bundle)
    print(
        f"rows={bundle['n_rows']} triple={bundle['n_triple']} "
        f"real+theory={bundle['n_real_theory']} real+h1={bundle['n_real_h1']} "
        f"→ {path} → {CANVAS}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
