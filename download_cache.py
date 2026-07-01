#!/usr/bin/env python3
"""Descarga datos de Yahoo a data/cache/ para backtests sin repetir requests."""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

from borex.data.cache import DEFAULT_CACHE_DIR, cache_path, download_to_cache


def _parse_command_line(command: str) -> tuple[str, str, str] | None:
    sym = re.search(r'-s\s+"([^"]+)"', command) or re.search(r"-s\s+(\S+)", command)
    period = re.search(r"-p\s+(\S+)", command)
    interval = re.search(r"-i\s+(\S+)", command)
    if not sym:
        return None
    return (
        sym.group(1),
        period.group(1) if period else "30d",
        interval.group(1) if interval else "1h",
    )


def datasets_from_commands(commands_file: Path) -> list[tuple[str, str, str]]:
    seen: set[tuple[str, str, str]] = set()
    datasets: list[tuple[str, str, str]] = []
    skipped = 0

    for line in commands_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|", 5)
        if len(parts) < 3:
            continue
        command = parts[2]
        expect = parts[4].strip().lower() if len(parts) >= 5 else "ok"

        if "--csv" in command:
            continue
        # Tests que deben fallar (símbolo inválido, CSV missing, etc.) no necesitan cache
        if expect == "fail":
            skipped += 1
            continue

        parsed = _parse_command_line(command)
        if parsed and parsed not in seen:
            seen.add(parsed)
            datasets.append(parsed)

    # Default run uses EURUSD without explicit -s in BT-001/002
    default = ("EURUSD=X", "30d", "1h")
    if default not in seen:
        datasets.insert(0, default)

    return datasets


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Descargar OHLCV a cache local")
    parser.add_argument("--symbol", "-s", help="Símbolo Yahoo (ej. GBPUSD=X)")
    parser.add_argument("--period", "-p", default="30d")
    parser.add_argument("--interval", "-i", default="1h")
    parser.add_argument(
        "--suite",
        action="store_true",
        help="Descargar todos los datasets únicos de tests/commands.txt",
    )
    parser.add_argument(
        "--commands-file",
        default="tests/commands.txt",
        help="Archivo de comandos del test suite",
    )
    parser.add_argument(
        "--cache-dir",
        default=str(DEFAULT_CACHE_DIR),
        help="Directorio de cache (default: data/cache)",
    )
    parser.add_argument(
        "--force", action="store_true", help="Re-descargar aunque exista cache"
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Segundos entre descargas (evitar rate limit Yahoo)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cache_dir = Path(args.cache_dir)

    if args.suite:
        cmd_file = Path(args.commands_file)
        if not cmd_file.is_file():
            print(f"No se encontró {cmd_file}", file=sys.stderr)
            return 1
        datasets = datasets_from_commands(cmd_file)
        print(f"Descargando {len(datasets)} datasets únicos a {cache_dir}/")
        print("(Tests con expect=fail se omiten — no necesitan cache)")
        print("(1 request por combo — luego los 120 tests usan disco local)\n")
        ok, fail = 0, 0
        for i, (symbol, period, interval) in enumerate(datasets, 1):
            try:
                path = download_to_cache(
                    symbol, period, interval, cache_dir, force=args.force
                )
                print(f"  [{i}/{len(datasets)}] OK  {symbol} {period} {interval} -> {path.name}")
                ok += 1
            except Exception as exc:
                print(f"  [{i}/{len(datasets)}] ERR {symbol} {period} {interval}: {exc}")
                fail += 1
            if i < len(datasets) and args.delay > 0:
                time.sleep(args.delay)
        print(f"\nListo: {ok} OK, {fail} errores")
        return 0 if fail == 0 else 1

    if not args.symbol:
        print("Usa --symbol o --suite", file=sys.stderr)
        return 1

    path = download_to_cache(
        args.symbol, args.period, args.interval, cache_dir, force=args.force
    )
    print(f"Guardado: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
