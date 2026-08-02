#!/usr/bin/env python3
"""Small deterministic unit tests for scan_market_news.py."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).with_name("scan_market_news.py")
SPEC = importlib.util.spec_from_file_location("scan_market_news", MODULE_PATH)
assert SPEC and SPEC.loader
NEWS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(NEWS)
VALIDATOR_PATH = Path(__file__).with_name("validate_output.py")
VALIDATOR_SPEC = importlib.util.spec_from_file_location("validate_output", VALIDATOR_PATH)
assert VALIDATOR_SPEC and VALIDATOR_SPEC.loader
VALIDATOR = importlib.util.module_from_spec(VALIDATOR_SPEC)
VALIDATOR_SPEC.loader.exec_module(VALIDATOR)
RENDER_PATH = Path(__file__).with_name("render_html.py")
RENDER_SPEC = importlib.util.spec_from_file_location("render_html", RENDER_PATH)
assert RENDER_SPEC and RENDER_SPEC.loader
RENDER = importlib.util.module_from_spec(RENDER_SPEC)
RENDER_SPEC.loader.exec_module(RENDER)


class ScanMarketNewsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = {
            "id": "test-source",
            "name": "Test Source",
            "topic": "macro",
            "markets": ["US"],
            "source_tier": 2,
            "source_kind": "media",
        }

    def test_parse_rss_and_remove_tracking(self) -> None:
        raw = b"""<?xml version="1.0"?>
        <rss><channel><item>
          <title>Fed updates policy guidance</title>
          <link>https://example.com/story?utm_source=rss&amp;id=7</link>
          <pubDate>Wed, 29 Jul 2026 20:00:00 GMT</pubDate>
          <description><![CDATA[<b>Short</b> summary]]></description>
        </item></channel></rss>"""
        rows = NEWS.extract_entries(raw, self.source, datetime(2026, 7, 29, 21, tzinfo=timezone.utc))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["url"], "https://example.com/story?id=7")
        self.assertEqual(rows[0]["summary"], "Short summary")
        self.assertEqual(rows[0]["published_at"], "2026-07-29T20:00:00Z")

    def test_title_similarity(self) -> None:
        score = NEWS.title_similarity(
            "Company raises full-year earnings guidance",
            "Company raises its full year earnings guidance",
        )
        self.assertGreater(score, 0.75)

    def test_legacy_user_config_is_supported(self) -> None:
        legacy = {
            "fetch": {"timeout": 15},
            "sources": [{
                "name": "OpenAI",
                "hint": "ai",
                "type": "rss",
                "url": "https://openai.com/news/rss.xml",
            }],
            "redline_keywords": ["crypto"],
        }
        normalized = NEWS.normalize_legacy_config(legacy)
        NEWS.validate_config(normalized)
        self.assertEqual(normalized["sources"][0]["topic"], "ai")
        self.assertEqual(normalized["sources"][0]["source_tier"], 1)
        self.assertEqual(normalized["fetch"]["timeout_seconds"], 15)
        self.assertEqual(normalized["filter_keywords"], ["crypto"])

    def test_cluster_and_score_do_not_invent_market_impact(self) -> None:
        now = datetime(2026, 7, 29, 21, tzinfo=timezone.utc)
        first = NEWS.extract_entries(
            b"""<rss><channel><item><title>Fed updates policy guidance</title>
            <link>https://a.example/story</link>
            <pubDate>Wed, 29 Jul 2026 20:00:00 GMT</pubDate></item></channel></rss>""",
            self.source,
            now,
        )[0]
        second_source = dict(self.source, id="second-source", name="Second Source")
        second = NEWS.extract_entries(
            b"""<rss><channel><item><title>Fed updates its policy guidance</title>
            <link>https://b.example/story</link>
            <pubDate>Wed, 29 Jul 2026 20:05:00 GMT</pubDate></item></channel></rss>""",
            second_source,
            now,
        )[0]
        clusters = NEWS.cluster_items([first, second], 0.56, 72)
        events = NEWS.build_events(clusters, "US", now)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["corroboration_count"], 2)
        self.assertIsNone(events[0]["market_impact_score"])
        self.assertEqual(events[0]["market_confirmation"], "pending")

    def test_pipeline_writes_a_valid_event_pack(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "sources.json"
            output_dir = root / "output"
            config = {
                "fetch": {
                    "per_source": 6,
                    "timeout_seconds": 5,
                    "max_workers": 1,
                    "max_response_bytes": 100000,
                    "cluster_similarity": 0.56,
                    "cluster_window_hours": 72,
                },
                "sources": [{
                    **self.source,
                    "type": "rss",
                    "url": "https://example.com/feed.xml",
                    "enabled": True,
                }],
                "filter_keywords": [],
            }
            config_path.write_text(json.dumps(config), encoding="utf-8")
            item = NEWS.extract_entries(
                b"""<rss><channel><item><title>Fed updates policy guidance</title>
                <link>https://example.com/story</link>
                <pubDate>Wed, 29 Jul 2026 20:00:00 GMT</pubDate></item></channel></rss>""",
                self.source,
                datetime(2026, 7, 29, 21, tzinfo=timezone.utc),
            )[0]
            ledger = {
                "source_id": "test-source",
                "name": "Test Source",
                "url": "https://example.com/feed.xml",
                "status": "success",
                "items_received": 1,
                "error": None,
                "fetched_at": "2026-07-29T21:00:00Z",
            }
            with patch.object(NEWS, "fetch_source", return_value=([item], ledger)):
                result = NEWS.main([
                    "--market", "US",
                    "--start", "2026-07-29T19:00:00Z",
                    "--end", "2026-07-29T22:00:00Z",
                    "--config", str(config_path),
                    "--output-dir", str(output_dir),
                ])
            self.assertEqual(result, 0)
            self.assertEqual(VALIDATOR.validate(output_dir), [])
            dashboard = RENDER.render_dashboard(
                output_dir,
                output_dir / "news_dashboard.html",
            )
            rendered = dashboard.read_text(encoding="utf-8")
            self.assertIn('"market":"US"', rendered)
            self.assertIn("Fed updates policy guidance", rendered)
            self.assertNotIn(RENDER.PLACEHOLDER, rendered)


if __name__ == "__main__":
    unittest.main()
