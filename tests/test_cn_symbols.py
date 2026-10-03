import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from stock_report.markets import cn


class EastmoneySymbolTests(unittest.TestCase):
    def test_same_code_in_different_markets_keeps_both_quotes(self):
        rows = [
            {"f12": "000001", "f13": 1, "f14": "上证指数", "f2": 3500,
             "f3": 1, "f104": 1000, "f105": 800},
            {"f12": "000001", "f13": 0, "f14": "平安银行", "f2": 12, "f3": -3},
        ]
        for response in (rows, list(reversed(rows)), {str(i): row for i, row in enumerate(rows)}):
            with self.subTest(response=response), patch.object(cn, "get_json", return_value={"data": {"diff": response}}) as fetch:
                result = cn.em_quotes(["SH000001", "000001"])
                self.assertEqual(set(result), {"sh000001", "sz000001"})
                self.assertEqual(result["sh000001"]["price"], 3500)
                self.assertEqual(result["sz000001"]["price"], 12)
                self.assertEqual(result["sh000001"]["up_count"], 1000)
                params = fetch.call_args.kwargs["params"]
                self.assertIn("f13", params["fields"].split(","))
                self.assertEqual(params["secids"], "1.000001,0.000001")

    def test_missing_or_unrequested_market_identity_is_discarded(self):
        rows = [{"f12": "000001", "f2": 12},
                {"f12": "000001", "f13": 0, "f2": 12},
                {"f12": "000001", "f13": 99, "f2": 999}]
        with patch.object(cn, "get_json", return_value={"data": {"diff": rows}}):
            self.assertEqual(cn.em_quotes(["sh000001"]), {})

    def test_requested_market_prefix_is_preserved_for_sh_sz_bj(self):
        rows = [{"f12": "600519", "f13": "1", "f2": 1500},
                {"f12": "300750", "f13": "0", "f2": 200},
                {"f12": "920001", "f13": 0, "f2": 20}]
        with patch.object(cn, "get_json", return_value={"data": {"diff": rows}}):
            result = cn.em_quotes(["600519", "sz300750", "bj920001"])
        self.assertEqual(set(result), {"sh600519", "sz300750", "bj920001"})

    def render_values(self, quotes):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(cn, "ROOT", root), patch.object(cn, "REPORTS", root), patch.object(cn, "render_template") as render:
                cn.render(
                    report_date="2026-08-03", now=datetime(2026, 8, 3, 16, tzinfo=cn.SH_TZ),
                    tq={}, eq=quotes, tech={}, kl={}, inds=[], flow_ind=[], flow_con=[],
                    zt=[], zb=[], dt=[], yzt=[], lhb=[], ev=[], announcements_pack={},
                    news_dir=root, data_dir=root, intraday_section="", quality={},
                    timestamped_events_pack={},
                )
                return render.call_args.args[1]

    def test_render_fallback_and_breadth_use_market_identity(self):
        values = self.render_values({
            "sh000001": {"price": 3500, "change_pct": 1, "up_count": 1000, "down_count": 800},
            "sz000001": {"name": "平安银行", "price": 12, "change_pct": -3},
            "sh000300": {"price": 4000, "up_count": 200, "down_count": 100},
        })
        self.assertIn("3,500.00", values["MARKET_OVERVIEW_TABLE_HTML"])
        self.assertNotIn("12.00", values["MARKET_OVERVIEW_TABLE_HTML"])
        self.assertIn("平安银行", values["MOVERS_TABLE_HTML"])
        self.assertIn("-3.00%", values["MOVERS_TABLE_HTML"])
        self.assertIn("1000 / 800", values["BREADTH_TABLE_HTML"])
        self.assertIn("200 / 100", values["BREADTH_TABLE_HTML"])

    def test_missing_index_never_falls_back_to_bank_or_legacy_bare_code(self):
        values = self.render_values({"sz000001": {"price": 12}, "000001": {"price": 99}})
        self.assertNotIn("12.00", values["MARKET_OVERVIEW_TABLE_HTML"])
        self.assertNotIn("99.00", values["MARKET_OVERVIEW_TABLE_HTML"])

    def test_legacy_snapshot_bare_codes_are_not_guessed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "snapshots").mkdir()
            (root / "snapshots" / "old.json").write_text(json.dumps({
                "report_date": "2026-08-03", "captured_at": "2026-08-03T10:00:00+08:00",
                "eastmoney_quotes": {"000001": {"price": 12}, "sh000300": {"price": 4000}},
            }))
            snapshot = cn.market_snapshot(root, datetime(2026, 8, 3, 10, 30, tzinfo=cn.SH_TZ))
        self.assertEqual(snapshot["eastmoney_quotes"], {"sh000300": {"price": 4000}})


if __name__ == "__main__":
    unittest.main()
