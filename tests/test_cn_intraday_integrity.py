import unittest
from datetime import datetime

from stock_report.markets import cn
from test_cn_modes import minute_rows

DAY = "2026-08-03"


class IntradayIntegrityTests(unittest.TestCase):
    def quality(self, rows):
        return cn.intraday_quality(DAY, {"symbols": {s: rows for s in cn.INTRADAY_INDEXES}},
                                  {"events": []}, {}, mode="intraday",
                                  as_of=datetime.fromisoformat(f"{DAY}T10:32:00+08:00"))

    def test_duplicate_wrong_day_off_grid_and_invalid_rows_fail(self):
        full = minute_rows(DAY, "10:30")
        cases = ([full[0]] * 11 + [full[-1]],
                 [{**r, "time": r["time"].replace(DAY, "2026-07-31")} for r in full],
                 [{**r, "time": r["time"].replace(":35:", ":36:")} for r in full],
                 [{**r, "close": None} for r in full], full + [full[-1]], list(reversed(full)))
        for rows in cases:
            with self.subTest(rows=rows[:1]):
                self.assertFalse(self.quality(rows)["ready"])

    def test_valid_grid_passes(self):
        self.assertTrue(self.quality(minute_rows(DAY, "10:30"))["ready"])

    def align(self, published, until, alter=None):
        rows = minute_rows(DAY, until)
        rows = alter(rows) if alter else rows
        event = {"event_id": "x", "title": "国务院发布政策", "published_at": f"{DAY}T{published}:00+08:00",
                 "a_share_relevant": True, "source": "test", "verification": "candidate"}
        pack = {"symbols": {s: rows for s in cn.INTRADAY_INDEXES}}
        return cn.align_intraday_events(pack, {"events": [event]})[0]

    def test_unfinished_event_is_pending_not_thirty_minute_return(self):
        result = self.align("10:25", "10:30")
        self.assertIsNone(result["moves_30m_pct"]["sh000001"])
        self.assertIsNone(result["volume_ratio"])
        self.assertEqual(result["alignment_strength"], "窗口待完成或数据不足")

    def test_complete_window_can_be_scored(self):
        result = self.align("10:25", "11:00")
        self.assertEqual(result["moves_30m_pct"]["sh000001"], 0)
        self.assertEqual(result["volume_ratio"], 1)

    def test_lunch_close_and_gaps_do_not_count_as_complete_windows(self):
        for published, until, alter in (("11:20", "15:00", None), ("14:50", "15:00", None),
                                        ("10:25", "11:00", lambda rows: [r for r in rows if r["minute"] != "10:40"])):
            with self.subTest(published=published):
                result = self.align(published, until, alter)
                self.assertIsNone(result["moves_30m_pct"]["sh000001"])

    def test_non_grid_event_reports_actual_observation_interval(self):
        result = self.align("10:27", "11:00")
        self.assertEqual(result["windows"]["sh000001"]["actual_start"], f"{DAY}T10:25:00+08:00")
        self.assertEqual(result["windows"]["sh000001"]["actual_end"], f"{DAY}T10:55:00+08:00")
