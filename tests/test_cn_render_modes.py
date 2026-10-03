import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from stock_report.markets import cn

SH = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parents[1]


def daily_rows():
    start = date(2025, 9, 18)
    return [
        {"date": (start + timedelta(days=i)).isoformat(), "open": 99.0, "close": 100.0,
         "high": 101.0, "low": 98.0, "volume": 1_000_000.0}
        for i in range(320)
    ]


def minute_rows():
    rows = []
    for start, count in ((datetime(2026, 8, 3, 9, 35, tzinfo=SH), 24),
                         (datetime(2026, 8, 3, 13, 5, tzinfo=SH), 24)):
        for i in range(count):
            moment = start + timedelta(minutes=5 * i)
            rows.append({
                "time": moment.isoformat(), "minute": moment.strftime("%H:%M"),
                "open": 100.0, "close": 100.0, "high": 100.2, "low": 99.8,
                "volume_lots": 100.0, "estimated_amount": 1_000_000.0,
                "turnover_basis_points": 1.0,
            })
    return rows


def quotes(codes):
    return {
        code: {
            "name": str(code), "price": 100.0, "last_close": 99.0, "open": 99.5,
            "change_pct": 1.01, "high": 101.0, "low": 98.5,
            "amount_wan": 100_000.0, "turnover_pct": 1.2, "vol_ratio": 1.1,
        }
        for code in codes
    }


def events(report_date, data_dir, *args, **kwargs):
    pack = {
        "report_date": report_date,
        "mode": kwargs.get("mode"),
        "events": [
            {
                "event_id": f"event-{i}", "title": "国务院发布测试政策",
                "published_at": datetime(2026, 8, 3, 10, i * 10, tzinfo=SH).isoformat(),
                "source": "测试源", "source_url": "https://example.com",
                "verification": "timestamped_candidate", "a_share_relevant": True,
            }
            for i in range(3)
        ],
    }
    (data_dir / "intraday_events.json").write_text(json.dumps(pack), encoding="utf-8")
    return pack


class CnRenderModeTests(unittest.TestCase):
    def run_mode(self, mode, as_of):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        patchers = [
            patch.object(cn, "kline", side_effect=lambda *args, **kwargs: daily_rows()),
            patch.object(cn, "minute_kline", side_effect=lambda *args, **kwargs: minute_rows()),
            patch.object(cn, "tencent_quotes", side_effect=quotes),
            patch.object(cn, "em_quotes", return_value={}),
            patch.object(cn, "industry_rows", return_value=[]),
            patch.object(cn, "board_flow", return_value=[]),
            patch.object(cn, "pool", return_value=[]),
            patch.object(cn, "dragon_tiger", return_value=[]),
            patch.object(cn, "announcements", return_value=[]),
            patch.object(cn, "news_events", return_value=[]),
            patch.object(cn, "collect_timestamped_events", side_effect=events),
        ]
        for item in patchers:
            item.start()
        try:
            output = cn.main(
                "2026-08-03", mode=mode, as_of=as_of, refresh=True,
                template=ROOT / "templates" / ("cn_intraday.html" if mode == "intraday" else "cn_close.html"),
                reports_dir=root / "reports", runs_dir=root / "runs",
            )
        finally:
            for item in reversed(patchers):
                item.stop()
        return temp, root, output

    def test_intraday_writes_latest_directory_and_report(self):
        temp, root, output = self.run_mode("intraday", datetime(2026, 8, 3, 10, 32, tzinfo=SH))
        try:
            data = root / "runs" / "cn" / "2026-08-03" / "intraday_latest" / "market_data"
            self.assertTrue((data / "evidence.json").exists())
            self.assertTrue((data / "minute_indices.json").exists())
            self.assertTrue((root / "reports" / "A股盘中快报_latest.html").exists())
            self.assertIn("A股盘中快报_2026-08-03_1032.html", output.name)
            text = output.read_text(encoding="utf-8")
            self.assertIn("盘中数据，未经收盘确认", text)
            self.assertNotIn("{{", text)
        finally:
            temp.cleanup()

    def test_close_writes_close_directory_and_report(self):
        temp, root, output = self.run_mode("close", datetime(2026, 8, 3, 15, 20, tzinfo=SH))
        try:
            data = root / "runs" / "cn" / "2026-08-03" / "close" / "market_data"
            evidence = json.loads((data / "evidence.json").read_text(encoding="utf-8"))
            self.assertEqual(evidence["report_type"], "close")
            self.assertTrue(evidence["is_final"])
            self.assertEqual(output.name, "A股收盘日报_2026-08-03_Asia-Shanghai.html")
            self.assertNotIn("{{", output.read_text(encoding="utf-8"))
        finally:
            temp.cleanup()


if __name__ == "__main__":
    unittest.main()
