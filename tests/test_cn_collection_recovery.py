import json
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from stock_report.markets import cn
from test_cn_render_modes import minute_rows, daily_rows, quotes

DAY = "2026-08-03"
ROOT = Path(__file__).resolve().parents[1]


class MinuteCacheTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.cache = self.directory / "minute_indices.json"

    def seed(self, symbols, day=DAY):
        self.cache.write_text(json.dumps({"report_date": day, "interval": "5m", "symbols": symbols}))

    def test_empty_cache_recovers_on_normal_rerun(self):
        with patch.object(cn, "minute_kline", return_value=[]):
            cn.collect_intraday_klines(DAY, self.directory)
        with patch.object(cn, "minute_kline", return_value=minute_rows()) as fetch:
            result = cn.collect_intraday_klines(DAY, self.directory)
        self.assertEqual(fetch.call_count, len(cn.INTRADAY_INDEXES))
        self.assertTrue(all(len(rows) == 48 for rows in result["symbols"].values()))
        self.assertEqual(json.loads(self.cache.read_text()), result)

    def test_only_incomplete_symbols_are_retried(self):
        symbols = {s: minute_rows() for s in cn.INTRADAY_INDEXES}
        symbols["sh000001"] = []
        symbols["sh000300"] = minute_rows()[:24]
        self.seed(symbols)
        with patch.object(cn, "minute_kline", return_value=minute_rows()) as fetch:
            result = cn.collect_intraday_klines(DAY, self.directory)
        self.assertEqual({call.args[0] for call in fetch.call_args_list}, {"sh000001", "sh000300"})
        self.assertEqual(result["symbols"], {s: minute_rows() for s in cn.INTRADAY_INDEXES})

    def test_complete_cache_is_reused_without_ttl(self):
        self.seed({s: minute_rows() for s in cn.INTRADAY_INDEXES})
        with patch.object(cn, "minute_kline") as fetch:
            result = cn.collect_intraday_klines(DAY, self.directory, cache_seconds=-1)
        fetch.assert_not_called()
        self.assertEqual(len(result["symbols"]["sh000001"]), 48)

    def test_failed_retry_preserves_complete_symbols_and_retries_next_run(self):
        symbols = {s: minute_rows() for s in cn.INTRADAY_INDEXES if s != "sh000001"}
        self.seed(symbols)
        with patch.object(cn, "minute_kline", return_value=[]) as fetch:
            first = cn.collect_intraday_klines(DAY, self.directory)
        fetch.assert_called_once_with("sh000001")
        self.assertEqual(first["symbols"]["sh000300"], minute_rows())
        with patch.object(cn, "minute_kline", return_value=minute_rows()) as fetch:
            second = cn.collect_intraday_klines(DAY, self.directory)
        fetch.assert_called_once_with("sh000001")
        self.assertEqual(second["symbols"]["sh000001"], minute_rows())

    def test_intraday_ttl_and_cutoff_are_preserved(self):
        self.seed({s: minute_rows() for s in cn.INTRADAY_INDEXES})
        cutoff = datetime(2026, 8, 3, 10, 30, tzinfo=cn.SH_TZ)
        with patch.object(cn, "minute_kline") as fetch:
            result = cn.collect_intraday_klines(DAY, self.directory, mode="intraday", as_of=cutoff)
        fetch.assert_not_called()
        self.assertEqual(len(result["symbols"]["sh000001"]), 12)
        self.assertEqual(len(json.loads(self.cache.read_text())["symbols"]["sh000001"]), 48)
        with patch.object(cn, "minute_kline", return_value=minute_rows()) as fetch:
            cn.collect_intraday_klines(DAY, self.directory, mode="intraday", as_of=cutoff, cache_seconds=-1)
        self.assertEqual(fetch.call_count, len(cn.INTRADAY_INDEXES))

    def test_duplicates_wrong_dates_and_invalid_prices_are_not_complete(self):
        wrong_day = [{**r, "time": r["time"].replace(DAY, "2026-07-31")} for r in minute_rows()]
        for rows in ([minute_rows()[0]] * 48, wrong_day,
                     [{**r, "close": None} for r in minute_rows()]):
            with self.subTest(rows=rows[:1]):
                self.seed({s: rows for s in cn.INTRADAY_INDEXES})
                with patch.object(cn, "minute_kline", return_value=minute_rows()):
                    result = cn.collect_intraday_klines(DAY, self.directory)
                self.assertEqual(result["symbols"]["sh000001"], minute_rows())

    def test_bad_cache_shape_is_refetched(self):
        for old in ([], {"report_date": DAY, "symbols": []},
                    {"report_date": DAY, "symbols": {"sh000001": "bad"}}):
            with self.subTest(old=old):
                self.cache.write_text(json.dumps(old))
                with patch.object(cn, "minute_kline", return_value=minute_rows()):
                    result = cn.collect_intraday_klines(DAY, self.directory)
                self.assertEqual(result["symbols"]["sh000001"], minute_rows())

    def test_explicit_refresh_and_wrong_report_date_refetch_all(self):
        for refresh, day in ((True, DAY), (False, "2026-07-31")):
            with self.subTest(refresh=refresh, day=day):
                self.seed({s: minute_rows() for s in cn.INTRADAY_INDEXES}, day)
                with patch.object(cn, "minute_kline", return_value=minute_rows()) as fetch:
                    cn.collect_intraday_klines(DAY, self.directory, refresh=refresh)
                self.assertEqual(fetch.call_count, len(cn.INTRADAY_INDEXES))


class LimitPoolTests(unittest.TestCase):
    def test_business_failure_or_invalid_structure_is_missing(self):
        for payload in ({"rc": 0, "data": None}, {"rc": 0, "data": {}},
                        {"rc": 1, "data": {"pool": []}}, {"data": {"pool": []}},
                        {"rc": 0, "data": {"pool": None}},
                        {"rc": 0, "data": {"pool": {}}},
                        {"rc": 0, "data": {"pool": [None]}},
                        {"rc": 0, "data": {"pool": [{}]}}, []):
            with self.subTest(payload=payload), patch.object(cn, "get_json", return_value=payload):
                self.assertIsNone(cn.pool("getTopicZTPool", DAY))

    def test_successful_empty_pool_is_zero_not_missing(self):
        with patch.object(cn, "get_json", return_value={"rc": 0, "data": {"pool": []}}):
            self.assertEqual(cn.pool("getTopicZTPool", DAY), [])

    def test_successful_nonempty_pool_is_preserved(self):
        rows = [{"c": "600519", "n": "贵州茅台"}]
        with patch.object(cn, "get_json", return_value={"rc": 0, "data": {"pool": rows}}):
            self.assertEqual(cn.pool("getTopicZTPool", DAY), rows)

    def test_network_failure_is_missing(self):
        with patch.object(cn, "get_json", side_effect=TimeoutError):
            self.assertIsNone(cn.pool("getTopicZTPool", DAY))

    def test_live_report_distinguishes_business_failure_from_empty_pool(self):
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 8, 3, 15, 30, tzinfo=cn.SH_TZ).astimezone(tz)

        for data, expected in ((None, None), ({"pool": []}, 0)):
            with self.subTest(data=data), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                root = Path(directory)
                stubs = {"datetime": Clock, "ROOT": root, "DATA_ROOT": root / "runs" / "cn",
                         "REPORTS": root / "reports", "TEMPLATE": ROOT / "templates" / "cn_close.html"}
                for name, value in stubs.items():
                    stack.enter_context(patch.object(cn, name, value))
                for name, value in {"kline": daily_rows(), "minute_kline": minute_rows(),
                                    "em_quotes": {}, "industry_rows": [], "board_flow": [],
                                    "dragon_tiger": [], "fetch_announcements": {}, "news_events": [],
                                    "collect_timestamped_events": {"events": []},
                                    "get_json": {"rc": 0, "data": data}}.items():
                    stack.enter_context(patch.object(cn, name, return_value=value))
                stack.enter_context(patch.object(cn, "tencent_quotes", side_effect=quotes))
                output = cn.main(DAY, template=ROOT / "templates" / "cn_close.html")
                evidence = json.loads((root / "runs" / "cn" / DAY / "close" / "market_data" / "evidence.json").read_text())
                self.assertEqual(evidence["limit_pool_available"], expected == 0)
                self.assertEqual(evidence["limit_up_count"], expected)
                self.assertEqual(evidence["limit_down_count"], expected)
                html = output.read_text()
                if expected is None:
                    self.assertIn("涨跌停池未取得", html)
                    self.assertNotIn("涨停0家", html)
                    self.assertNotIn("跌停0家", html)
                else:
                    self.assertIn("涨停0家", html)
