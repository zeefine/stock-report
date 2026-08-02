from __future__ import annotations

from statistics import mean
from typing import Any, Iterable


def pct_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous in (None, 0):
        return None
    return (current / previous - 1) * 100


def sma(values: Iterable[float], window: int) -> float | None:
    data = list(values)
    return mean(data[-window:]) if window > 0 and len(data) >= window else None


def rsi(values: Iterable[float], window: int = 14) -> float | None:
    data = list(values)
    if window <= 0 or len(data) < window + 1:
        return None
    changes = [b - a for a, b in zip(data[-window - 1 : -1], data[-window:])]
    avg_gain = mean(max(change, 0) for change in changes)
    avg_loss = mean(max(-change, 0) for change in changes)
    if avg_loss == 0:
        return 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def volume_ratio(volumes: Iterable[float], lookback: int = 20) -> float | None:
    data = list(volumes)
    if lookback <= 0 or len(data) < lookback + 1:
        return None
    baseline = mean(data[-lookback - 1 : -1])
    return data[-1] / baseline if baseline else None


def technical_snapshot(rows: list[dict[str, Any]]) -> dict[str, float | str | None]:
    valid = [row for row in rows if row.get("close") is not None]
    closes = [float(row["close"]) for row in valid]
    volumes = [float(row.get("volume") or 0) for row in valid]
    return {
        "close": closes[-1] if closes else None,
        "day": pct_change(closes[-1], closes[-2]) if len(closes) >= 2 else None,
        "r5": pct_change(closes[-1], closes[-6]) if len(closes) >= 6 else None,
        "ma20": sma(closes, 20),
        "ma50": sma(closes, 50),
        "ma200": sma(closes, 200),
        "rsi14": rsi(closes, 14),
        "volume_ratio20": volume_ratio(volumes, 20),
        "last_date": valid[-1].get("date") if valid else None,
    }
