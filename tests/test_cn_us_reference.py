import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from stock_report.markets import cn


class USReferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runs = Path(self.tmp.name) / "custom_runs"

    def pack(self, day, rows):
        path = self.runs / "us" / day / "market_data" / "evidence.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"report_date": day,
            "generated_at": f"{day}T21:00:00+00:00",
            **{s: {"rows": rows} for s in ("SPY", "QQQ", "SMH")}}))
        return path

    def reference(self, stamp):
        return cn.us_reference(self.runs, datetime.fromisoformat(stamp))

    def test_uses_configured_root_and_excludes_future_rows_and_packs(self):
        path = self.pack("2026-07-31", [{"date": "2026-07-30", "close": 100},
                                      {"date": "2026-07-31", "close": 101},
                                      {"date": "2026-08-03", "close": 999}])
        self.pack("2026-08-03", [{"date": "2026-08-03", "close": 999}])
        result = self.reference("2026-08-03T15:30:00+08:00")
        self.assertEqual(result["expected_date"], "2026-07-31")
        self.assertEqual(result["assets"]["SPY"]["date"], "2026-07-31")
        self.assertEqual(result["assets"]["SPY"]["close"], 101)
        self.assertEqual(result["assets"]["SPY"]["evidence_path"], str(path))
        self.assertFalse(result["assets"]["SPY"]["stale"])

    def test_stale_data_keeps_actual_date(self):
        self.pack("2026-07-29", [{"date": "2026-07-28", "close": 100},
                                 {"date": "2026-07-29", "close": 101}])
        result = self.reference("2026-08-03T15:30:00+08:00")
        self.assertTrue(result["assets"]["SPY"]["stale"])
        self.assertEqual(result["assets"]["SPY"]["date"], "2026-07-29")

    def test_calendar_respects_holiday_dst_and_early_close(self):
        for cutoff, expected in (("2026-07-06T10:00:00+08:00", "2026-07-02"),
                                 ("2026-08-04T03:59:00+08:00", "2026-07-31"),
                                 ("2026-08-04T04:00:00+08:00", "2026-08-03"),
                                 ("2026-11-28T02:00:00+08:00", "2026-11-27"),
                                 ("2026-12-25T02:00:00+08:00", "2026-12-24"),
                                 ("2028-01-01T10:00:00+08:00", "2027-12-31")):
            with self.subTest(cutoff=cutoff):
                self.assertEqual(self.reference(cutoff)["expected_date"], expected)

    def test_empty_directory_is_missing_not_a_default_historical_value(self):
        result = self.reference("2026-08-03T15:30:00+08:00")
        self.assertEqual(result["assets"], {})

    def test_pack_saved_before_close_does_not_supply_partial_close(self):
        path = self.pack("2026-07-31", [{"date": "2026-07-30", "close": 100},
                                      {"date": "2026-07-31", "close": 999}])
        data = json.loads(path.read_text())
        data["generated_at"] = "2026-07-31T18:00:00+00:00"
        path.write_text(json.dumps(data))
        result = self.reference("2026-08-03T15:30:00+08:00")
        self.assertEqual(result["assets"]["SPY"]["date"], "2026-07-30")
        self.assertTrue(result["assets"]["SPY"]["stale"])

    def test_return_with_missing_previous_session_is_not_called_daily_change(self):
        self.pack("2026-07-31", [{"date": "2026-07-29", "close": 100},
                                 {"date": "2026-07-31", "close": 101}])
        result = self.reference("2026-08-03T15:30:00+08:00")
        self.assertIsNone(result["assets"]["SPY"]["change_pct"])

    def test_render_displays_actual_us_date_and_staleness(self):
        self.pack("2026-07-29", [{"date": "2026-07-29", "close": 101}])
        cutoff = datetime.fromisoformat("2026-08-03T15:30:00+08:00")
        reference = self.reference(cutoff.isoformat())
        with patch.object(cn, "REPORTS", self.runs / "reports"), patch.object(cn, "render_template") as render:
            cn.render("2026-08-03", cutoff, {}, {}, {}, {}, [], [], [], [], [], [], [], [], [], {},
                      self.runs, self.runs, "", {}, {}, as_of=cutoff, cross_market=reference)
        values = render.call_args.args[1]
        self.assertIn("2026-07-29", values["CROSS_ASSET_TABLE_HTML"])
        self.assertIn("过期", values["CROSS_ASSET_TABLE_HTML"])
        self.assertIn("2026-07-31", values["CROSS_ASSET_TABLE_HTML"])
