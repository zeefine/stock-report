from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Load the dependency-free JSON subset of YAML used by this project."""
    target = path or project_root() / "config.yaml"
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"配置文件不存在：{target}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"config.yaml 必须是有效的 JSON/YAML 子集：{exc}") from exc
    if not isinstance(data, dict) or "markets" not in data:
        raise RuntimeError("config.yaml 缺少 markets 配置")
    return data


def validate_date(value: str | None) -> str | None:
    if value is None:
        return None
    if not DATE_RE.fullmatch(value):
        raise ValueError("日期必须使用 YYYY-MM-DD 格式")
    date.fromisoformat(value)
    return value


def now_in(timezone_name: str) -> datetime:
    return datetime.now(ZoneInfo(timezone_name))


def next_weekdays(start: str, count: int, include_start: bool = False) -> list[str]:
    current = date.fromisoformat(start)
    out: list[str] = []
    if not include_start:
        current += timedelta(days=1)
    while len(out) < count:
        if current.weekday() < 5:
            out.append(current.isoformat())
        current += timedelta(days=1)
    return out


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    day = date(year, month, 1)
    day += timedelta(days=(weekday - day.weekday()) % 7 + 7 * (occurrence - 1))
    return day


def _last_weekday(year: int, month: int, weekday: int) -> date:
    next_month = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    day = next_month - timedelta(days=1)
    return day - timedelta(days=(day.weekday() - weekday) % 7)


def _easter_sunday(year: int) -> date:
    # Anonymous Gregorian algorithm.
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f, g = (b + 8) // 25, (b - (b + 8) // 25 + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    return date(year, month, (h + l - 7 * m + 114) % 31 + 1)


def us_exchange_holidays(year: int) -> set[date]:
    holidays = {
        _observed(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),       # MLK Day
        _nth_weekday(year, 2, 0, 3),       # Presidents Day
        _easter_sunday(year) - timedelta(days=2),
        _last_weekday(year, 5, 0),         # Memorial Day
        _observed(date(year, 6, 19)),
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),       # Labor Day
        _nth_weekday(year, 11, 3, 4),      # Thanksgiving
        _observed(date(year, 12, 25)),
    }
    # A Saturday New Year's Day is observed on Dec 31 of the prior year.
    next_new_year = _observed(date(year + 1, 1, 1))
    if next_new_year.year == year:
        holidays.add(next_new_year)
    return holidays


def next_us_trading_days(start: str, count: int) -> list[str]:
    current = date.fromisoformat(start) + timedelta(days=1)
    out: list[str] = []
    while len(out) < count:
        if current.weekday() < 5 and current not in us_exchange_holidays(current.year):
            out.append(current.isoformat())
        current += timedelta(days=1)
    return out


def first_existing(paths: Iterable[Path]) -> Path | None:
    return next((path for path in paths if path.exists()), None)
