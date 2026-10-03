import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from stock_report.pipeline import build_context, run_report
from stock_report.common import next_us_trading_days
from test_cn_close_quality import close_fixture


def write_project(root: Path):
    (root / "templates").mkdir()
    (root / "prompts").mkdir()
    (root / "templates" / "cn_close.html").write_text("<html>ok</html>", encoding="utf-8")
    (root / "templates" / "cn_intraday.html").write_text("<html>ok</html>", encoding="utf-8")
    (root / "prompts" / "cn.md").write_text("facts only", encoding="utf-8")
    config = {
        "project": {"runs_dir": "runs", "reports_dir": "reports", "templates_dir": "templates", "prompts_dir": "prompts"},
        "pipeline": {"llm_enabled": False},
        "markets": {"cn": {"timezone": "Asia/Shanghai", "close_template": "cn_close.html", "intraday_template": "cn_intraday.html", "prompt": "cn.md"}},
    }
    (root / "config.yaml").write_text(json.dumps(config), encoding="utf-8")


class PipelineTests(unittest.TestCase):
    def test_us_calendar_skips_exchange_holiday(self):
        self.assertEqual(next_us_trading_days("2026-07-02", 2), ["2026-07-06", "2026-07-07"])

    def test_build_context_resolves_project_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_project(root)
            context = build_context("cn", "2026-07-30", root=root)
            self.assertEqual(context.template, (root / "templates" / "cn_close.html").resolve())
            self.assertEqual(context.report_date, "2026-07-30")
            self.assertEqual(context.mode, "close")
            self.assertFalse(context.as_of_explicit)
            self.assertEqual(context.market_run_dir("2026-07-30"), root.resolve() / "runs" / "cn" / "2026-07-30" / "close")

    def test_intraday_context_uses_requested_directory_and_template(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_project(root)
            context = build_context(
                "cn", "2026-08-03", mode="intraday", as_of="10:30", root=root
            )
            self.assertEqual(context.mode, "intraday")
            self.assertEqual(context.as_of.strftime("%H:%M"), "10:30")
            self.assertTrue(context.as_of_explicit)
            self.assertEqual(context.template, (root / "templates" / "cn_intraday.html").resolve())
            self.assertEqual(
                context.market_run_dir("2026-08-03"),
                root.resolve() / "runs" / "cn" / "2026-08-03" / "intraday_latest",
            )

    @patch("stock_report.pipeline.now_in")
    def test_current_close_is_rejected_before_1515(self, mocked_now):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        mocked_now.return_value = datetime(2026, 8, 3, 15, 5, tzinfo=ZoneInfo("Asia/Shanghai"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_project(root)
            with self.assertRaisesRegex(RuntimeError, "15:15"):
                build_context("cn", "2026-08-03", mode="close", root=root)

    def test_run_writes_manifest_and_quality(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_project(root)

            def fake_run(context):
                report = context.reports_dir / "A股收盘日报_2026-07-30_Asia-Shanghai.html"
                evidence = context.market_run_dir("2026-07-30") / "market_data" / "evidence.json"
                report.parent.mkdir(parents=True)
                evidence.parent.mkdir(parents=True)
                report.write_text("<html>ok</html>", encoding="utf-8")
                payload, minutes = close_fixture("2026-07-30")
                evidence.write_text(json.dumps(payload), encoding="utf-8")
                (evidence.parent / "minute_indices.json").write_text(json.dumps(minutes), encoding="utf-8")
                return "2026-07-30", report

            with patch("stock_report.pipeline.import_module", return_value=SimpleNamespace(run=fake_run)):
                result = run_report("cn", "2026-07-30", root=root)
            self.assertTrue(result.quality.passed)
            self.assertTrue(result.manifest_path.exists())
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["market"], "cn")
            self.assertEqual(manifest["mode"], "close")
            self.assertEqual(result.mode, "close")

    def test_intraday_run_writes_requested_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_project(root)

            def fake_run(context):
                report = context.reports_dir / "A股盘中快报_2026-08-03_1030.html"
                evidence = context.market_run_dir("2026-08-03") / "market_data" / "evidence.json"
                report.parent.mkdir(parents=True)
                evidence.parent.mkdir(parents=True)
                report.write_text('<html><head><meta name="stock-report-date" content="2026-08-03">'
                                  '<meta name="stock-report-as-of" content="2026-08-03T10:30:00+08:00">'
                                  '<meta name="stock-report-market" content="cn">'
                                  '<meta name="stock-report-mode" content="intraday"></head></html>', encoding="utf-8")
                evidence.write_text(json.dumps({"market": "cn", "report_date": "2026-08-03",
                    "report_type": "intraday", "as_of": "2026-08-03T10:30:00+08:00"}), encoding="utf-8")
                return "2026-08-03", report

            with patch("stock_report.pipeline.import_module", return_value=SimpleNamespace(run=fake_run)):
                result = run_report(
                    "cn", "2026-08-03", mode="intraday", as_of="10:30", root=root
                )
            expected = root.resolve() / "runs" / "cn" / "2026-08-03" / "intraday_latest"
            self.assertEqual(result.mode, "intraday")
            self.assertTrue(result.quality.passed)
            self.assertEqual(result.manifest_path, expected / "manifest.json")
            self.assertEqual(result.evidence_path, expected / "market_data" / "evidence.json")


if __name__ == "__main__":
    unittest.main()
