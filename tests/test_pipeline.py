import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from stock_report.pipeline import build_context, run_report
from stock_report.common import next_us_trading_days


def write_project(root: Path):
    (root / "templates").mkdir()
    (root / "prompts").mkdir()
    (root / "templates" / "cn_close.html").write_text("<html>ok</html>", encoding="utf-8")
    (root / "prompts" / "cn.md").write_text("facts only", encoding="utf-8")
    config = {
        "project": {"runs_dir": "runs", "reports_dir": "reports", "templates_dir": "templates", "prompts_dir": "prompts"},
        "pipeline": {"llm_enabled": False},
        "markets": {"cn": {"timezone": "Asia/Shanghai", "template": "cn_close.html", "prompt": "cn.md"}},
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

    def test_run_writes_manifest_and_quality(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_project(root)

            def fake_run(context):
                report = context.reports_dir / "A股收盘日报_2026-07-30_Asia-Shanghai.html"
                evidence = context.runs_dir / "cn" / "2026-07-30" / "market_data" / "evidence.json"
                report.parent.mkdir(parents=True)
                evidence.parent.mkdir(parents=True)
                report.write_text("<html>ok</html>", encoding="utf-8")
                evidence.write_text(json.dumps({"report_date": "2026-07-30"}), encoding="utf-8")
                return "2026-07-30", report

            with patch("stock_report.pipeline.import_module", return_value=SimpleNamespace(run=fake_run)):
                result = run_report("cn", "2026-07-30", root=root)
            self.assertTrue(result.quality.passed)
            self.assertTrue(result.manifest_path.exists())
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["market"], "cn")


if __name__ == "__main__":
    unittest.main()
