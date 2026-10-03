from __future__ import annotations

from typing import Any
from datetime import datetime

from .common import CN_CORE_QUOTES, cn_timestamp, finite_number


def classify_market(index_changes: dict[str, float | None]) -> str:
    values = list(index_changes.values())
    if not values or not all(finite_number(value) for value in values):
        return "数据不足"
    positive = sum(value > 0 for value in values)
    negative = sum(value < 0 for value in values)
    if positive == len(values):
        return "核心指数同步上涨"
    if negative == len(values):
        return "核心指数同步下跌"
    if not positive and not negative:
        return "核心指数持平"
    return "核心指数涨跌不一"


def cn_market_assessment(quotes: dict, technical: dict, baskets: dict,
                         cutoff: datetime, mode: str) -> dict:
    """Deterministic index direction and adequately covered basket comparisons."""
    day = cutoff.date().isoformat()
    changes = {}
    for symbol, quote in quotes.items():
        if not isinstance(quote, dict):
            continue
        stamp = cn_timestamp(quote.get("time"))
        if (stamp is not None and stamp.date() == cutoff.date() and stamp <= cutoff
                and (mode != "close" or stamp.hour >= 15)
                and finite_number(quote.get("price")) and quote["price"] > 0
                and finite_number(quote.get("change_pct"))):
            changes[symbol] = quote["change_pct"]
    core = {symbol: changes.get(symbol) for symbol in CN_CORE_QUOTES}
    sectors = []
    coverage = {}
    for name, members in baskets.items():
        valid = [symbol for symbol in members if symbol in changes]
        coverage[name] = {"valid": len(valid), "total": len(members)}
        if len(valid) < 2 or len(valid) / len(members) < 0.5:
            continue
        r5 = [technical.get(symbol, {}).get("r5") for symbol in valid
              if technical.get(symbol, {}).get("last_date") == day]
        r5 = [value for value in r5 if finite_number(value)]
        sectors.append({"name": name, "day": sum(changes[s] for s in valid) / len(valid),
                        "r5": sum(r5) / len(r5) if len(r5) == len(valid) else None,
                        "n": len(valid), "total": len(members)})
    sectors.sort(key=lambda row: (-row["day"], row["name"]))
    ranked = len(sectors) >= 2 and sectors[0]["day"] - sectors[-1]["day"] > 1e-9
    for row in sectors:
        row["rank"] = 1 + sum(other["day"] > row["day"] + 1e-9 for other in sectors) if ranked else None
    leaders = [row for row in sectors if ranked and abs(row["day"] - sectors[0]["day"]) <= 1e-9]
    laggards = [row for row in sectors if ranked and abs(row["day"] - sectors[-1]["day"]) <= 1e-9]
    return {"status": classify_market(core), "core_changes": core,
            "valid_core_count": sum(value is not None for value in core.values()),
            "sectors": sectors, "sector_coverage": coverage, "sector_ranked": ranked,
            "leaders": leaders, "laggards": laggards,
            "focus": [row["name"] for row in leaders if row["day"] > 0],
            "rules": "四个核心指数同日有效报价齐全才判断方向；篮子至少2只且覆盖率≥50%；至少2个合格篮子且收益有差异才排名；仅正收益领先篮子列为关注，不代表资金流入。"}


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
