import json
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from stock_report.markets import cn
from stock_report.quality import validate_report

SH = cn.SH_TZ
DAY = "2026-08-03"
ROOT = Path(__file__).resolve().parents[1]


class CutoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_report(self, mode="intraday", cutoff="10:32", quote_time="20260803150000", now=None,
                   omit_daily=False):
        daily = [
            {"date": "2026-07-31", "open": 90, "close": 100, "high": 101, "low": 89, "volume": 1000},
            {"date": DAY, "open": 101, "close": 110, "high": 111, "low": 100, "volume": 2000},
            {"date": "2026-08-04", "open": 900, "close": 999, "high": 999, "low": 900, "volume": 3000},
        ]
        if omit_daily:
            daily = [row for row in daily if row["date"] != DAY]
        minutes = [
            {"time": f"{DAY}T{hm}:00+08:00", "minute": hm, "open": 101,
             "close": price, "high": price, "low": 100, "volume_lots": 10}
            for hm, price in [("09:35", 102), ("10:30", 105), ("15:00", 110)]
        ]
        def quotes(codes):
            return {c: {"name": c, "price": 999, "high": 999, "amount_wan": 999,
                        "change_pct": 899, "time": quote_time} for c in codes}
        patches = {
            "kline": {"return_value": daily},
            "minute_kline": {"return_value": minutes},
            "tencent_quotes": {"side_effect": quotes},
            "em_quotes": {"return_value": {"sh000001": {"price": 999}}},
            "industry_rows": {"return_value": [{"name": "future-industry"}]},
            "board_flow": {"return_value": [{"name": "future-flow"}]},
            "pool": {"return_value": [{"c": "600999", "name": "future-limit"}]},
            "dragon_tiger": {"return_value": []},
            "fetch_announcements": {"return_value": {}},
            "news_events": {"return_value": []},
            "collect_timestamped_events": {"return_value": {"events": []}},
        }
        with ExitStack() as stack:
            if now:
                class Clock(datetime):
                    @classmethod
                    def now(cls, tz=None):
                        return now.astimezone(tz)
                stack.enter_context(patch.object(cn, "datetime", Clock))
            for name, kwargs in patches.items():
                stack.enter_context(patch.object(cn, name, **kwargs))
            output = cn.main(
                DAY, mode=mode,
                as_of=datetime.fromisoformat(f"{DAY}T{cutoff}:00+08:00") if cutoff else None,
                template=ROOT / "templates" / ("cn_intraday.html" if mode == "intraday" else "cn_close.html"),
                reports_dir=self.root / "reports", runs_dir=self.root / "runs",
            )
        variant = "intraday_latest" if mode == "intraday" else "close"
        path = self.root / "runs" / "cn" / DAY / variant / "market_data" / "evidence.json"
        return json.loads(path.read_text()), output

    def test_intraday_rebuilds_price_without_later_quote_fields(self):
        evidence, _ = self.run_report()
        quote = evidence["quotes"]["sh000001"]
        self.assertEqual(quote["price"], 105)
        self.assertIsNone(quote.get("amount_wan"))
        self.assertIsNone(quote.get("turnover_pct"))
        self.assertIsNone(quote.get("vol_ratio"))
        self.assertNotIn("600519", evidence["quotes"])
        self.assertIsNone(evidence["technical"]["600519"]["close"])

    def test_historical_close_uses_exact_day_and_omits_current_flows(self):
        evidence, _ = self.run_report("close", None, "20260804150000")
        self.assertEqual(evidence["quotes"]["sh000001"]["price"], 110)
        self.assertEqual(evidence["as_of"][:10], DAY)
        self.assertEqual(evidence["industry_flow"], [])
        self.assertEqual(evidence["eastmoney_quotes"], {})
        self.assertIsNone(evidence["limit_up_count"])

    def test_exact_cutoff_quote_is_accepted(self):
        evidence, _ = self.run_report(quote_time="20260803103200")
        self.assertEqual(evidence["quotes"]["sh000001"]["price"], 999)

    def test_undated_quote_is_not_accepted(self):
        evidence, _ = self.run_report(quote_time="")
        self.assertEqual(evidence["quotes"]["sh000001"]["price"], 105)

    def test_previous_day_quote_is_not_accepted(self):
        evidence, _ = self.run_report(quote_time="20260731150000")
        self.assertEqual(evidence["quotes"]["sh000001"]["price"], 105)

    def test_live_run_saves_snapshot_and_replay_uses_it(self):
        evidence, output = self.run_report(cutoff=None, quote_time="20260803103200",
                                          now=datetime(2026, 8, 3, 10, 32, tzinfo=SH))
        self.assertEqual(evidence["quotes"]["sh000001"]["price"], 999)
        self.assertEqual(evidence["industry_flow"], [{"name": "future-flow"}])
        directory = self.root / "runs" / "cn" / DAY / "snapshots"
        self.assertEqual(len(list(directory.glob("*.json"))), 1)
        path = self.root / "runs" / "cn" / DAY / "intraday_latest" / "market_data" / "evidence.json"
        self.assertTrue(validate_report(output, path).passed)
        evidence, _ = self.run_report(cutoff="10:33")
        self.assertEqual(evidence["quotes"]["sh000001"]["price"], 999)
        self.assertEqual(evidence["industry_flow"], [{"name": "future-flow"}])

    def test_evidence_started_before_cutoff_but_finished_after_is_excluded(self):
        directory = self.root / "runs" / "cn" / DAY / "intraday_latest" / "market_data"
        directory.mkdir(parents=True)
        (directory / "evidence.json").write_text(json.dumps({
            "report_date": DAY, "generated_at": f"{DAY}T10:25:00+08:00",
            "as_of": f"{DAY}T10:35:00+08:00", "industry_flow": [{"name": "late"}],
        }))
        evidence, _ = self.run_report()
        self.assertEqual(evidence["industry_flow"], [])

    def test_close_does_not_reconstruct_from_a_different_day(self):
        result = cn.quotes_at_cutoff(["600519"], {}, {},
                                    {"600519": [{"date": "2026-07-31", "close": 100}]}, {},
                                    datetime(2026, 8, 3, 16, tzinfo=SH), "close")
        self.assertEqual(result, {})

    def test_close_missing_daily_bar_does_not_display_previous_day_technicals(self):
        evidence, _ = self.run_report("close", None, "20260804150000", omit_daily=True)
        self.assertIsNone(evidence["technical"]["sh000001"]["close"])
        self.assertIsNone(evidence["technical"]["600519"]["day"])

    def test_close_rejects_morning_snapshot(self):
        directory = self.root / "runs" / "cn" / DAY / "snapshots"
        directory.mkdir(parents=True)
        (directory / "morning.json").write_text(json.dumps({
            "report_date": DAY, "captured_at": f"{DAY}T10:30:00+08:00",
            "industry_flow": [{"name": "morning"}],
        }))
        evidence, _ = self.run_report("close", None, "20260804150000")
        self.assertEqual(evidence["industry_flow"], [])

    def test_news_cluster_updated_after_cutoff_is_excluded(self):
        events = [{"first_published_at": f"{DAY}T10:00:00+08:00",
                   "last_updated_at": f"{DAY}T14:00:00+08:00"},
                  {"published_at": f"{DAY}T10:30:00+08:00"}]
        self.assertEqual(cn.events_at_cutoff(events, datetime(2026, 8, 3, 10, 32, tzinfo=SH)), events[1:])

    def test_saved_snapshot_before_cutoff_is_preferred(self):
        directory = self.root / "runs" / "cn" / DAY / "snapshots"
        directory.mkdir(parents=True)
        for suffix, captured, price in [("early", "10:25", 103), ("late", "15:00", 999)]:
            (directory / f"{suffix}.json").write_text(json.dumps({
                "report_date": DAY, "captured_at": f"{DAY}T{captured}:00+08:00",
                "quotes": {"sh000001": {"price": price, "time": f"{DAY}T{captured}:00+08:00"}},
                "industry_flow": [{"name": suffix}],
            }))
        evidence, _ = self.run_report()
        self.assertEqual(evidence["quotes"]["sh000001"]["price"], 103)
        self.assertEqual(evidence["industry_flow"], [{"name": "early"}])

    def test_quality_rejects_future_quote(self):
        report = self.root / f"report_{DAY}.html"
        report.write_text("<html>ok</html>")
        evidence = self.root / "evidence.json"
        evidence.write_text(json.dumps({
            "market": "cn", "report_type": "intraday", "report_date": DAY,
            "as_of": f"{DAY}T10:32:00+08:00",
            "quotes": {"sh000001": {"price": 999, "time": "20260803150000"}},
        }))
        self.assertFalse(validate_report(report, evidence).passed)

    def test_quality_rejects_future_flow_snapshot(self):
        report = self.root / f"report_{DAY}.html"
        report.write_text("<html>ok</html>")
        evidence = self.root / "evidence.json"
        evidence.write_text(json.dumps({
            "market": "cn", "report_type": "intraday", "report_date": DAY,
            "as_of": f"{DAY}T10:32:00+08:00", "snapshot_as_of": f"{DAY}T15:00:00+08:00",
            "industry_flow": [{"name": "future"}], "quotes": {},
        }))
        result = validate_report(report, evidence)
        self.assertFalse(result.passed)
        self.assertIn("snapshot_outside_cutoff", [issue.code for issue in result.issues])


if __name__ == "__main__":
    unittest.main()
