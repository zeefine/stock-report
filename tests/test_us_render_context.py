import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_report.markets import us

ROOT = Path(__file__).resolve().parents[1]
DAY = "2026-07-31"
CORE = ("SPY", "QQQ", "DIA", "IWM", "RSP")
SECTORS = ("XLK", "XLC", "XLY", "XLP", "XLE", "XLF", "XLV", "XLI", "XLB", "XLRE", "XLU")


def series(change=1, day=DAY):
    return {"rows": [{"date": "2026-07-30", "close": 100, "volume": 100},
                     {"date": day, "close": 100 + change, "volume": 200}]}


class USRenderContextTests(unittest.TestCase):
    def values(self, data):
        payload = {"report_date": DAY, "generated_at": "2026-08-01T01:00:00+00:00", **data}
        with patch.object(us, "render_template") as render, patch.object(us, "REPORT_DATE", "2099-01-01"):
            us.render(payload, [], template=ROOT / "templates" / "us_close.html", output=Path("/tmp/mock-us.html"))
        return render.call_args.args[1]

    def test_dates_come_from_evidence_not_globals(self):
        block = series(2)
        block["rows"].append({"date": "2026-08-03", "close": 999, "volume": 100})
        values = self.values({"SPY": block})
        self.assertEqual(values["REPORT_DATE_ET"], DAY + " ET")
        self.assertEqual(values["SPY_CLOSE"], "102.00")
        self.assertIn("2026-08-01 09:00:00", values["GENERATED_AT_SHANGHAI"])
        self.assertNotIn("2099", str(values))

    def test_fresh_process_produces_identical_html_without_network(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = {"report_date": DAY, "generated_at": "2026-08-01T01:00:00+00:00", "SPY": series(2)}
            data["SPY"]["rows"].append({"date": "2026-08-03", "close": 999, "volume": 1})
            saved = copy.deepcopy(data)
            evidence, output = root / "evidence.json", root / "report.html"
            evidence.write_text(json.dumps(data))
            template = ROOT / "templates" / "us_close.html"
            us.render(data, [], template=template, output=output)
            first = output.read_text()
            script = "from stock_report.markets import us; from pathlib import Path; import json,sys; us.get_json=lambda *a,**k: (_ for _ in ()).throw(AssertionError('network')); us.render(json.loads(Path(sys.argv[1]).read_text()), [], template=Path(sys.argv[2]), output=Path(sys.argv[3]))"
            subprocess.run([sys.executable, "-c", script, str(evidence), str(template), str(output)],
                           env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, check=True, capture_output=True)
            self.assertEqual(first, output.read_text())
            self.assertEqual(data, saved)

    def test_missing_date_is_rejected(self):
        with patch.object(us, "render_template"), self.assertRaises(ValueError):
            us.render({}, [], output=Path("/tmp/mock-us.html"))

    def test_empty_or_partial_data_has_no_direction_or_rank(self):
        for data in ({}, {"SPY": series(), "XLK": series(5)}):
            with self.subTest(data=data):
                values = self.values(data)
                self.assertEqual(values["MARKET_STATUS"], "数据不足")
                self.assertIn("数据不足", values["THEME_ROTATION_HTML"])
                self.assertNotIn("重点观察", values["SECTOR_SUMMARY_HTML"])
                self.assertNotIn("MA20 下方", values["TECHNICAL_COMMENTARY_HTML"])
                self.assertNotIn("+0.00%", values["BREADTH_PROXY_TABLE_HTML"])

    def test_stale_nonfinite_and_future_data_do_not_qualify(self):
        for block in (series(day="2026-07-29"), series(float("nan")), series(day="2026-08-03")):
            values = self.values({s: block for s in CORE + SECTORS})
            self.assertEqual(values["MARKET_STATUS"], "数据不足")
            self.assertIn("数据不足", values["THEME_ROTATION_HTML"])

    def test_covered_market_and_sectors_can_be_classified(self):
        data = {s: series(1) for s in CORE}
        data.update({s: series(i) for i, s in enumerate(SECTORS[:6])})
        values = self.values(data)
        self.assertIn("同步上涨", values["MARKET_STATUS"])
        self.assertIn("金融", values["THEME_ROTATION_HTML"])
        self.assertNotIn("相对弱势：公用事业", values["THEME_ROTATION_HTML"])

    def test_tied_sector_returns_do_not_choose_by_list_order(self):
        values = self.values({s: series(1) for s in CORE + SECTORS})
        self.assertIn("无明显差异", values["THEME_ROTATION_HTML"])
        self.assertNotIn("重点观察", values["SECTOR_SUMMARY_HTML"])
