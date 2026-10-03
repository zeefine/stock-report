import copy
import json
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from stock_report.markets import cn, us
from stock_report.common import CN_CORE_QUOTES

ROOT = Path(__file__).resolve().parents[1]
DAY = "2026-08-03"


class USMoverEvidenceTests(unittest.TestCase):
    def test_secondary_daily_data_is_saved_before_render_and_replays_offline(self):
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            root = Path(folder)
            for key in ("REPORT_DATE", "GENERATED_AT", "ASOF", "DATA_DIR", "NEWS_DIR", "TEMPLATE", "OUT", "CALENDAR_DATES"):
                stack.enter_context(patch.object(us, key, getattr(us, key)))
            def chart(symbol, range_, interval, prepost):
                if symbol == "FAILED":
                    return {"symbol": symbol, "rows": [], "meta": {}, "error": "timeout"}
                rows = ([{"date": "2026-07-31", "close": 100, "volume": 100},
                         {"date": DAY, "close": 123, "volume": 200}]
                        if interval == "1d" else [])
                return {"symbol": symbol, "rows": rows, "meta": {}}
            fetch = stack.enter_context(patch.object(us, "chart", side_effect=chart))
            stack.enter_context(patch.object(us, "earnings_day", return_value=[]))
            stack.enter_context(patch.object(us, "treasury", return_value={}))
            stack.enter_context(patch.object(us, "screener", return_value=[{"symbol": "OUTSIDE", "shortName": "Test mover"}, {"symbol": "FAILED"}]))
            stack.enter_context(patch.object(us, "news_pack", return_value=[]))
            evidence_path = root / "runs" / "us" / DAY / "market_data" / "evidence.json"
            render = us.render
            def verify_then_render(data, events, **context):
                saved = json.loads(evidence_path.read_text())
                self.assertIn("OUTSIDE", saved)
                self.assertEqual(saved["FAILED"]["error"], "timeout")
                self.assertEqual(saved, data)
                with patch.object(us, "chart", side_effect=AssertionError("render must be offline")):
                    return render(data, events, **context)
            stack.enter_context(patch.object(us, "render", side_effect=verify_then_render))
            output = us.main(DAY, template=ROOT / "templates" / "us_close.html",
                             reports_dir=root / "reports", runs_dir=root / "runs")
            first = output.read_text()
            self.assertIn("Test mover", first)
            self.assertEqual(sum(c.args[0] == "OUTSIDE" and c.args[2] == "1d" for c in fetch.call_args_list), 1)
            saved = json.loads(evidence_path.read_text())
            before = copy.deepcopy(saved)
            with patch.object(us, "chart", side_effect=AssertionError("offline replay")):
                render(saved, [], template=ROOT / "templates" / "us_close.html", output=output)
            self.assertEqual(first, output.read_text())
            self.assertEqual(saved, before)

    def test_render_missing_mover_does_not_fetch(self):
        with patch.object(us, "OUT", Path("/tmp/us-render-not-written.html")), \
                patch.object(us, "chart", side_effect=AssertionError("render must not fetch")), \
                patch.object(us, "render_template") as output:
            us.render({"report_date": DAY, "gainers": [{"symbol": "MISSING"}], "after": {}}, [], output=Path("/tmp/us-render-not-written.html"))
        self.assertIn("MISSING", output.call_args.args[1]["MOVERS_TABLE_HTML"])
        self.assertIn("未取得", output.call_args.args[1]["MOVERS_TABLE_HTML"])


class CNConclusionTests(unittest.TestCase):
    def quotes(self, changes):
        return {symbol: {"price": 100, "change_pct": value, "time": f"{DAY}T15:00:00+08:00"}
                for symbol, value in changes.items()}

    def values(self, changes, tech=None, mode="close", quotes=None):
        with tempfile.TemporaryDirectory() as folder, patch.object(cn, "REPORTS", Path(folder)), \
                patch.object(cn, "render_template") as render:
            root = Path(folder)
            cutoff = datetime.fromisoformat(f"{DAY}T15:30:00+08:00")
            cn.render(DAY, cutoff, self.quotes(changes) if quotes is None else quotes,
                      {}, tech or {}, {}, [], [], [], [], [], [], [], [], [], {},
                      root, root, "", {}, {}, as_of=cutoff, mode=mode)
            return render.call_args.args[1]

    def test_positive_negative_mixed_and_flat_have_distinct_conclusions(self):
        for numbers, label in (([1, 2, 1, 3], "核心指数同步上涨"),
                               ([-1, -2, -1, -3], "核心指数同步下跌"),
                               ([1, -1, 2, -2], "核心指数涨跌不一"),
                               ([0, 0, 0, 0], "核心指数持平")):
            with self.subTest(numbers=numbers):
                values = self.values(dict(zip(CN_CORE_QUOTES, numbers)))
                for key in ("MARKET_STATUS", "MARKET_STAGE", "FINAL_CONCLUSION_HTML"):
                    self.assertIn(label, values[key])
                self.assertNotIn("板块轮动与风险偏好分化", values["FINAL_CONCLUSION_HTML"])

    def test_empty_partial_stale_and_nonfinite_data_abstains(self):
        valid = self.quotes({s: 1 for s in CN_CORE_QUOTES})
        stale = {s: {**q, "time": "2026-07-31T15:00:00+08:00"} for s, q in valid.items()}
        for quotes in ({}, {CN_CORE_QUOTES[0]: valid[CN_CORE_QUOTES[0]]}, stale,
                       {**valid, "sh000001": {**valid["sh000001"], "change_pct": float("nan")}}):
            with self.subTest(quotes=quotes):
                values = self.values({}, quotes=quotes)
                self.assertIn("数据不足", values["FINAL_CONCLUSION_HTML"])
                self.assertNotIn("相对强势", values["SECTOR_SUMMARY_HTML"])
                self.assertNotIn("科技与高端制造", values["SECTOR_SUMMARY_HTML"])

    def test_missing_and_sparse_baskets_are_not_ranked(self):
        values = self.values({"600519": 8}, {"600519": {"day": 8, "r5": 10, "last_date": DAY}})
        self.assertIn("数据不足", values["SECTOR_SUMMARY_HTML"])
        self.assertNotIn("相对强势", values["SECTOR_SUMMARY_HTML"])
        self.assertNotIn("<td>1</td>", values["INDUSTRY_ROTATION_TABLE_HTML"])

    def test_sector_ranking_uses_current_quotes_and_does_not_impute_missing_r5(self):
        changes = {s: 1 for s in CN_CORE_QUOTES}
        for name, members in cn.SECTOR_BASKETS.items():
            changes.update({s: (3 if name == "消费" else -1) for s in members})
        values = self.values(changes)
        self.assertIn("消费", values["SECTOR_SUMMARY_HTML"])
        self.assertIn("+3.00%", values["SECTOR_SUMMARY_HTML"])
        self.assertIn("消费", values["FIRST_READ_HTML"])
        self.assertNotIn("0.00%", values["INDUSTRY_ROTATION_TABLE_HTML"])

    def test_identical_baskets_do_not_choose_winners_by_config_order(self):
        changes = {s: 1 for members in cn.SECTOR_BASKETS.values() for s in members}
        values = self.values(changes)
        self.assertNotIn("相对强势", values["SECTOR_SUMMARY_HTML"])
        self.assertNotIn("<td>1</td>", values["INDUSTRY_ROTATION_TABLE_HTML"])

    def test_intraday_does_not_rank_using_yesterday_returns(self):
        technical = {s: {"day": 9, "r5": 5, "last_date": "2026-07-31"}
                     for members in cn.SECTOR_BASKETS.values() for s in members}
        values = self.values({}, technical, mode="intraday")
        self.assertIn("数据不足", values["SECTOR_SUMMARY_HTML"])

    def test_tied_leaders_are_all_reported_and_negative_leaders_are_not_focus(self):
        from stock_report.analysis import cn_market_assessment
        baskets = {"甲": ["a", "b"], "乙": ["c", "d"], "丙": ["e", "f"]}
        cutoff = datetime.fromisoformat(f"{DAY}T15:30:00+08:00")
        for first, last in ((2, -1), (-1, -2)):
            data = self.quotes({"a": first, "b": first, "c": first, "d": first, "e": last, "f": last})
            assessment = cn_market_assessment(data, {}, baskets, cutoff, "close")
            self.assertEqual({r["name"] for r in assessment["leaders"]}, {"甲", "乙"})
            self.assertEqual([r["rank"] for r in assessment["sectors"]], [1, 1, 3])
            self.assertEqual(set(assessment["focus"]), {"甲", "乙"} if first > 0 else set())
