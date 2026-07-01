from __future__ import annotations

from datetime import timedelta

# Minutos por intervalo de Yahoo/yfinance
INTERVAL_MINUTES: dict[str, int] = {
    "1m": 1,
    "2m": 2,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "60m": 60,
    "1h": 60,
    "4h": 240,
    "90m": 90,
    "1d": 1440,
    "5d": 7200,
    "1wk": 10080,
    "1mo": 43200,
}

# Escalera MTF: todos los TF superiores deben alinearse (incl. 1d y 1wk)
MTF_ALIGNMENT_LADDER = ["15m", "30m", "1h", "4h", "1d", "1wk"]

MTF_MIN_EXECUTION_INTERVAL = "15m"


def interval_to_minutes(interval: str) -> int:
    key = interval.strip().lower()
    if key not in INTERVAL_MINUTES:
        raise ValueError(
            f"Intervalo no soportado: {interval!r}. "
            f"Usa uno de: {', '.join(sorted(INTERVAL_MINUTES))}"
        )
    return INTERVAL_MINUTES[key]


def interval_to_timedelta(interval: str) -> timedelta:
    return timedelta(minutes=interval_to_minutes(interval))


def validate_higher_timeframe(execution_interval: str, filter_interval: str) -> None:
    exec_m = interval_to_minutes(execution_interval)
    filter_m = interval_to_minutes(filter_interval)
    if filter_m <= exec_m:
        raise ValueError(
            f"El timeframe de filtro ({filter_interval}) debe ser mayor que "
            f"el de ejecución ({execution_interval}). "
            f"Ej: -i 1h --mtf"
        )


def filter_intervals_for_execution(execution_interval: str) -> list[str]:
    """Timeframes superiores que deben alinearse (desde exec+1 hasta 1wk)."""
    exec_m = interval_to_minutes(execution_interval)
    min_m = interval_to_minutes(MTF_MIN_EXECUTION_INTERVAL)

    if exec_m < min_m:
        raise ValueError(
            f"MTF requiere timeframe de ejecución >= {MTF_MIN_EXECUTION_INTERVAL} "
            f"(recibido: {execution_interval})"
        )

    filters = [
        tf
        for tf in MTF_ALIGNMENT_LADDER
        if interval_to_minutes(tf) > exec_m
    ]
    if not filters:
        raise ValueError(
            f"No hay timeframes superiores para {execution_interval}. "
            f"Usa un TF de ejecución menor (ej. 15m, 1h, 4h)."
        )
    return filters
