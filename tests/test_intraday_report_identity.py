import json
import tempfile
import unittest
from pathlib import Path

from stock_report.pipeline import validate_existing
from test_pipeline import write_project

DAY = "2026-08-03"
ASOF = DAY + "T10:30:00+08:00"


def page(day=DAY, asof=ASOF):
    return (f'<html><head><meta name="stock-report-date" content="{day}">'
            f'<meta name="stock-report-as-of" content="{asof}">'
            '<meta name="stock-report-market" content="cn">'
            '<meta name="stock-report-mode" content="intraday"></head></html>')


class IntradayIdentityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        write_project(self.root)
        run = self.root / "runs" / "cn" / DAY / "intraday_latest"
        data = run / "market_data"
        data.mkdir(parents=True)
        reports = self.root / "reports"
        reports.mkdir()
        self.report = reports / f"A股盘中快报_{DAY}_1030.html"
        self.report.write_text(page())
        (reports / "A股盘中快报_latest.html").write_text(page("2026-08-04", "2026-08-04T10:30:00+08:00"))
        self.evidence = data / "evidence.json"
        self.evidence.write_text(json.dumps({"market": "cn", "report_type": "intraday", "report_date": DAY, "as_of": ASOF}))
        self.manifest = run / "manifest.json"
        self.manifest.write_text(json.dumps({"market": "cn", "mode": "intraday", "report_date": DAY,
                                             "as_of": ASOF, "report_path": str(self.report)}))

    def validate(self):
        return validate_existing("cn", DAY, mode="intraday", root=self.root)

    def test_reads_dated_manifest_target_not_latest_alias(self):
        (self.root / "reports" / "A股盘中快报_latest.html").write_text("{{WRONG_PAGE}}")
        self.assertTrue(self.validate().passed)

    def test_wrong_page_date_or_time_is_rejected(self):
        for html in (page("2026-08-04"), page(asof=DAY + "T10:35:00+08:00"), "<html>legacy</html>"):
            with self.subTest(html=html):
                self.report.write_text(html)
                self.assertFalse(self.validate().passed)

    def test_wrong_evidence_cutoff_or_manifest_metadata_is_rejected(self):
        evidence = json.loads(self.evidence.read_text())
        evidence["as_of"] = DAY + "T10:35:00+08:00"
        self.evidence.write_text(json.dumps(evidence))
        self.assertFalse(self.validate().passed)
        manifest = json.loads(self.manifest.read_text())
        manifest["report_date"] = "2026-08-04"
        self.manifest.write_text(json.dumps(manifest))
        self.assertFalse(self.validate().passed)

    def test_missing_or_corrupt_manifest_never_falls_back_to_latest(self):
        self.manifest.unlink()
        self.assertFalse(self.validate().passed)
        self.manifest.write_text("[]")
        self.assertFalse(self.validate().passed)

    def test_missing_evidence_cannot_verify_page_identity(self):
        self.evidence.unlink()
        self.assertFalse(self.validate().passed)
