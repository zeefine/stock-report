import json
import tempfile
import unittest
from pathlib import Path

from stock_report.quality import unresolved_placeholders, validate_report


class QualityTests(unittest.TestCase):
    def test_finds_only_report_placeholders(self):
        self.assertEqual(unresolved_placeholders("x {{FOO}} {{FOO}} {{BAR_2}}"), ["{{BAR_2}}", "{{FOO}}"])

    def test_report_and_evidence_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "report.html"
            evidence = root / "evidence.json"
            report.write_text("<html><body>已渲染</body></html>", encoding="utf-8")
            evidence.write_text(json.dumps({"report_date": "2026-07-30"}), encoding="utf-8")
            result = validate_report(report, evidence)
            self.assertTrue(result.passed)

    def test_unresolved_placeholder_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.html"
            report.write_text("{{MISSING}}", encoding="utf-8")
            result = validate_report(report)
            self.assertFalse(result.passed)
            self.assertEqual(result.issues[0].code, "unresolved_placeholders")


if __name__ == "__main__":
    unittest.main()
