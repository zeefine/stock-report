from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CN_CORE_QUOTES = ("sh000001", "sz399001", "sh000300", "sz399006")
CN_MINUTE_INDEXES = ("sh000001", "sh000300", "sh000905", "sz399852", "sz399006", "sh000688")


def finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def cn_close_quote_errors(quotes: dict, cutoff: datetime) -> list[str]:
    invalid = []
    for symbol in CN_CORE_QUOTES:
        quote = quotes.get(symbol) or {}
        if not isinstance(quote, dict):
            invalid.append(symbol)
            continue
        observed = cn_timestamp(quote.get("time"))
        if (not finite_number(quote.get("price")) or quote["price"] <= 0
                or not finite_number(quote.get("change_pct"))
                or observed is None or observed.date() != cutoff.date()
                or observed.hour < 15 or observed > cutoff):
            invalid.append(symbol)
    return invalid


def cn_close_minute_checks(symbols: dict, quotes: dict, report_date: str) -> dict:
    day = datetime.fromisoformat(report_date).replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    expected = {day.replace(hour=hour, minute=minute) + timedelta(minutes=5 * i)
                for hour, minute in ((9, 35), (13, 5)) for i in range(24)}
    checks = {}
    for symbol in CN_MINUTE_INDEXES:
        rows = symbols.get(symbol) or []
        rows = rows if isinstance(rows, list) else []
        valid_rows = {cn_timestamp(r.get("time")): r for r in rows if isinstance(r, dict)
                      and finite_number(r.get("close")) and r["close"] > 0}
        final = valid_rows.get(day.replace(hour=15)) or {}
        quote = quotes.get(symbol) or {}
        price = quote.get("price") if isinstance(quote, dict) else None
        error = (abs(final["close"] / price - 1) * 100
                 if final and finite_number(price) and price > 0 else None)
        checks[symbol] = {"valid": set(valid_rows) == expected and len(rows) == 48
                         and error is not None and error <= 0.2,
                         "bars": len(rows), "close_error_pct": error}
    return checks


def cn_close_price_mismatches(checks: dict) -> list[str]:
    return [symbol for symbol in CN_CORE_QUOTES
            if (checks.get(symbol, {}).get("close_error_pct") or 0) > 0.2]


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


def cn_timestamp(value: Any) -> datetime | None:
    """Parse a provider timestamp; compact Tencent times are Shanghai local time."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = (datetime.strptime(value, "%Y%m%d%H%M%S")
                  if re.fullmatch(r"\d{14}", value) else datetime.fromisoformat(value))
        if parsed.tzinfo is None:
            # Only the provider's documented compact format implies a timezone.
            if not re.fullmatch(r"\d{14}", value):
                return None
            parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        return parsed.astimezone(ZoneInfo("Asia/Shanghai"))
    except ValueError:
        return None


def cn_session_state(moment: datetime) -> str:
    """Classify the regular A-share session using Asia/Shanghai local time."""
    local = moment.astimezone(ZoneInfo("Asia/Shanghai"))
    if local.weekday() >= 5:
        return "closed"
    current = local.time().replace(tzinfo=None)
    if current < time(9, 15):
        return "pre_market"
    if current < time(9, 30):
        return "auction"
    if current <= time(11, 30):
        return "morning"
    if current < time(13, 0):
        return "lunch_break"
    if current <= time(15, 0):
        return "afternoon"
    if current < time(15, 15):
        return "closing_pending"
    return "closed"


def resolve_report_mode(
    market: str,
    requested: str,
    report_date: str | None,
    moment: datetime,
) -> str:
    if requested not in {"auto", "intraday", "close"}:
        raise ValueError("mode 必须是 auto、intraday 或 close")
    if market != "cn":
        if requested == "intraday":
            raise ValueError("当前仅 A 股支持 intraday 模式")
        return "close"
    if requested != "auto":
        return requested
    local = moment.astimezone(ZoneInfo("Asia/Shanghai"))
    if report_date and report_date != local.date().isoformat():
        return "close"
    return "intraday" if cn_session_state(local) in {
        "auction", "morning", "lunch_break", "afternoon", "closing_pending"
    } else "close"


def parse_as_of(value: str | None, timezone_name: str, report_date: str | None,
                moment: datetime) -> datetime:
    local = moment.astimezone(ZoneInfo(timezone_name))
    if value is None:
        return local
    try:
        parsed = datetime.strptime(value, "%H:%M").time()
    except ValueError as exc:
        raise ValueError("as-of 必须使用 HH:MM 格式") from exc
    target_date = date.fromisoformat(report_date) if report_date else local.date()
    return datetime.combine(target_date, parsed, ZoneInfo(timezone_name))


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
    # NYSE does not observe Saturday New Year's Day on the preceding Friday.
    new_year = date(year, 1, 1)
    if new_year.weekday() != 5:
        holidays.add(_observed(new_year))
    return holidays


def next_us_trading_days(start: str, count: int) -> list[str]:
    current = date.fromisoformat(start) + timedelta(days=1)
    out: list[str] = []
    while len(out) < count:
        if current.weekday() < 5 and current not in us_exchange_holidays(current.year):
            out.append(current.isoformat())
        current += timedelta(days=1)
    return out


def us_session_close(day: date) -> datetime | None:
    if day.weekday() >= 5 or day in us_exchange_holidays(day.year):
        return None
    # NYSE holiday/early-closing calendar: https://www.nyse.com/trade/hours-calendars
    early = (day == _nth_weekday(day.year, 11, 3, 4) + timedelta(days=1)
             or (day.month, day.day) in {(7, 3), (12, 24)})
    return datetime.combine(day, time(13 if early else 16), ZoneInfo("America/New_York"))


def latest_completed_us_session(cutoff: datetime) -> str:
    day = cutoff.astimezone(ZoneInfo("America/New_York")).date()
    while True:
        close = us_session_close(day)
        if close is not None and close <= cutoff:
            return day.isoformat()
        day -= timedelta(days=1)


def first_existing(paths: Iterable[Path]) -> Path | None:
    return next((path for path in paths if path.exists()), None)
