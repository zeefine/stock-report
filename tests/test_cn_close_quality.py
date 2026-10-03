import json
import math
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace

from stock_report import cli
from stock_report.markets import cn
from stock_report.quality import validate_report

DAY = "2026-08-03"


def close_fixture(day=DAY):
    rows = []
    for hour, minute in ((9, 35), (13, 5)):
        start = datetime.fromisoformat(day).replace(hour=hour, minute=minute, tzinfo=cn.SH_TZ)
        for index in range(24):
            stamp = start + timedelta(minutes=5 * index)
            rows.append({"time": stamp.isoformat(), "minute": stamp.strftime("%H:%M"),
                         "close": 100, "volume_lots": 10})
    quotes = {symbol: {"price": 100, "change_pct": 0, "time": f"{day}T15:00:00+08:00"}
              for symbol in set(cn.INTRADAY_INDEXES) | {"sz399001"}}
    minutes = {"report_date": day, "symbols": {symbol: list(rows) for symbol in cn.INTRADAY_INDEXES}}
    evidence = {"market": "cn", "report_type": "close", "report_date": day,
                "as_of": f"{day}T15:20:00+08:00", "is_final": True, "quotes": quotes,
                "intraday_quality": {"mode": "close", "ready": False, "is_final": True,
                                     "core_index_coverage": 1, "a_share_relevant_event_count": 0}}
    return evidence, minutes


class CloseQualityTests(unittest.TestCase):
    def check_report(self, evidence, minutes=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / f"A股收盘日报_{DAY}_Asia-Shanghai.html"
            report.write_text("<html>report</html>")
            path = root / "evidence.json"
            if evidence is not None:
                path.write_text(json.dumps(evidence))
            if minutes is not None:
                (root / "minute_indices.json").write_text(json.dumps(minutes))
            return validate_report(report, path)

    def test_empty_or_absent_evidence_cannot_pass_close_gate(self):
        for evidence in (None, {}, [], {"report_date": DAY},
                         {"market": "cn", "report_type": "close", "report_date": DAY,
                          "as_of": f"{DAY}T15:20:00+08:00", "quotes": {}, "is_final": False}):
            with self.subTest(evidence=evidence):
                self.assertFalse(self.check_report(evidence).passed)

    def test_missing_core_quote_and_invalid_prices_fail(self):
        for value in (None, 0, -1, math.nan, math.inf):
            evidence, minutes = close_fixture()
            evidence["quotes"]["sz399001"]["price"] = value
            with self.subTest(value=value):
                self.assertFalse(self.check_report(evidence, minutes).passed)
        evidence, minutes = close_fixture()
        del evidence["quotes"]["sh000001"]
        self.assertFalse(self.check_report(evidence, minutes).passed)

    def test_intraday_or_wrong_day_quote_is_not_a_close(self):
        for stamp in ("20260803143000", "20260731150000", "20260804150000"):
            evidence, minutes = close_fixture()
            evidence["quotes"]["sh000001"]["time"] = stamp
            with self.subTest(stamp=stamp):
                self.assertFalse(self.check_report(evidence, minutes).passed)

    def test_minute_coverage_is_checked_from_rows_not_declared_flag(self):
        evidence, minutes = close_fixture()
        for minute_pack in (None, {"report_date": DAY, "symbols": {}},
                            {"report_date": DAY, "symbols": {s: [r[0]] * 48 for s, r in minutes["symbols"].items()}}):
            with self.subTest(minute_pack=minute_pack):
                self.assertFalse(self.check_report(evidence, minute_pack).passed)
        minutes["symbols"]["sh000001"] = []
        minutes["symbols"]["sh000300"] = []
        self.assertFalse(self.check_report(evidence, minutes).passed)

    def test_complete_market_data_with_no_news_passes_with_warning(self):
        evidence, minutes = close_fixture()
        result = self.check_report(evidence, minutes)
        self.assertTrue(result.passed)
        self.assertTrue(any(issue.severity == "warning" for issue in result.issues))
        quality = cn.intraday_quality(DAY, minutes, {"events": []}, evidence["quotes"],
                                      mode="close", as_of=datetime.fromisoformat(evidence["as_of"]))
        self.assertTrue(quality["is_final"])
        self.assertFalse(quality["ready"])

    def test_unconfirmed_close_and_mode_mismatch_fail(self):
        for field, value in (("is_final", False), ("report_type", "intraday"), ("market", "us"),
                             ("report_date", "2026-08-04")):
            evidence, minutes = close_fixture()
            evidence[field] = value
            with self.subTest(field=field):
                self.assertFalse(self.check_report(evidence, minutes).passed)

    def test_core_quote_disagrees_with_full_minute_close(self):
        evidence, minutes = close_fixture()
        evidence["quotes"]["sh000001"]["price"] = 200
        self.assertFalse(self.check_report(evidence, minutes).passed)

    def test_one_missing_secondary_minute_series_can_degrade(self):
        evidence, minutes = close_fixture()
        minutes["symbols"]["sh000688"] = []
        self.assertTrue(self.check_report(evidence, minutes).passed)

    def test_wrong_day_minutes_fail_even_if_report_date_matches(self):
        evidence, minutes = close_fixture()
        for rows in minutes["symbols"].values():
            for row in rows:
                row["time"] = row["time"].replace(DAY, "2026-07-31")
        self.assertFalse(self.check_report(evidence, minutes).passed)

    def test_failed_quality_causes_cli_nonzero_exit(self):
        result = self.check_report({"report_date": DAY})
        run_result = SimpleNamespace(market="cn", report_date=DAY, mode="close",
                                    report_path=Path("report.html"), evidence_path=None,
                                    manifest_path=Path("manifest.json"), quality=result)
        with patch.object(cli, "run_report", return_value=run_result), redirect_stdout(StringIO()):
            self.assertEqual(cli.main(["run", "--market", "cn", "--mode", "close"]), 2)
