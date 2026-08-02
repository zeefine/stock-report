from __future__ import annotations

from typing import Any


def classify_market(index_changes: dict[str, float | None]) -> str:
    values = [value for value in index_changes.values() if value is not None]
    if not values:
        return "数据不足"
    positive = sum(value > 0 for value in values)
    negative = sum(value < 0 for value in values)
    if positive == len(values):
        return "普涨/风险偏好改善"
    if negative == len(values):
        return "普跌/风险偏好承压"
    return "结构性分化/板块轮动"


def ranked_candidates(rows: list[dict[str, Any]], limit: int = 15) -> list[dict[str, Any]]:
    """Deterministic candidate ordering; no LLM judgment is used."""
    return sorted(
        rows,
        key=lambda row: (
            abs(row.get("change_pct") or 0),
            row.get("volume_ratio") or 0,
            row.get("amount") or 0,
        ),
        reverse=True,
    )[:limit]
