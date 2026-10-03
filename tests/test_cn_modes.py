import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from stock_report.common import cn_session_state, resolve_report_mode
from stock_report.markets.cn import INTRADAY_INDEXES, intraday_quality

SH = ZoneInfo("Asia/Shanghai")


def minute_rows(day: str, until: str):
    base = datetime.fromisoformat(f"{day}T09:35:00+08:00")
    end = datetime.fromisoformat(f"{day}T{until}:00+08:00")
    rows = []
    current = base
    while current <= min(end, current.replace(hour=11, minute=30)):
        rows.append({"time": current.isoformat(), "minute": current.strftime("%H:%M"), "close": 100.0, "volume_lots": 10})
        current += timedelta(minutes=5)
    if end >= base.replace(hour=13, minute=5):
        current = base.replace(hour=13, minute=5)
        while current <= min(end, base.replace(hour=15, minute=0)):
            rows.append({"time": current.isoformat(), "minute": current.strftime("%H:%M"), "close": 100.0, "volume_lots": 10})
            current += timedelta(minutes=5)
    return rows


class CnModeTests(unittest.TestCase):
    def test_session_state(self):
        self.assertEqual(cn_session_state(datetime(2026, 8, 3, 10, 30, tzinfo=SH)), "morning")
        self.assertEqual(cn_session_state(datetime(2026, 8, 3, 12, 0, tzinfo=SH)), "lunch_break")
        self.assertEqual(cn_session_state(datetime(2026, 8, 3, 15, 20, tzinfo=SH)), "closed")

    def test_auto_mode(self):
        self.assertEqual(resolve_report_mode("cn", "auto", None, datetime(2026, 8, 3, 10, 30, tzinfo=SH)), "intraday")
        self.assertEqual(resolve_report_mode("cn", "auto", None, datetime(2026, 8, 3, 15, 20, tzinfo=SH)), "close")
        self.assertEqual(resolve_report_mode("cn", "auto", "2026-07-31", datetime(2026, 8, 3, 10, 30, tzinfo=SH)), "close")

    def test_intraday_quality_only_requires_completed_bars(self):
        rows = minute_rows("2026-08-03", "10:30")
        pack = {"symbols": {symbol: rows for symbol in INTRADAY_INDEXES}}
        quotes = {symbol: {"price": 100.0} for symbol in INTRADAY_INDEXES}
        quality = intraday_quality(
            "2026-08-03", pack, {"events": []}, quotes,
            mode="intraday", as_of=datetime(2026, 8, 3, 10, 32, tzinfo=SH),
        )
        self.assertTrue(quality["ready"])
        self.assertEqual(quality["rules"]["minimum_bars_per_index"], 12)
        self.assertEqual(quality["rules"]["required_session_end"], "10:30")
        self.assertFalse(quality["is_final"])

    def test_close_quality_requires_full_session_and_events(self):
        rows = minute_rows("2026-08-03", "15:00")
        pack = {"symbols": {symbol: rows for symbol in INTRADAY_INDEXES}}
        quotes = {symbol: {"price": 100.0, "change_pct": 0,
                           "time": "2026-08-03T15:00:00+08:00"}
                  for symbol in INTRADAY_INDEXES + ["sz399001"]}
        events = {"events": [{"a_share_relevant": True}] * 3}
        quality = intraday_quality(
            "2026-08-03", pack, events, quotes, mode="close",
            as_of=datetime(2026, 8, 3, 15, 20, tzinfo=SH),
        )
        self.assertTrue(quality["ready"])
        self.assertTrue(quality["is_final"])
        self.assertEqual(quality["rules"]["minimum_bars_per_index"], 48)


if __name__ == "__main__":
    unittest.main()
