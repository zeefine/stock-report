#!/usr/bin/env python3
"""Validate scan-market-news output without third-party dependencies."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

REQUIRED_FILES = {
    "news_raw.jsonl",
    "news_events.json",
    "news_sources.json",
    "manifest.json",
}


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def require(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def valid_iso(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def validate(directory: Path) -> list[str]:
    errors: list[str] = []
    missing = sorted(name for name in REQUIRED_FILES if not (directory / name).is_file())
    if missing:
        return [f"missing files: {', '.join(missing)}"]

    try:
        events_payload = load_json(directory / "news_events.json")
        sources_payload = load_json(directory / "news_sources.json")
        manifest = load_json(directory / "manifest.json")
    except (OSError, json.JSONDecodeError) as exc:
        return [f"invalid JSON: {exc}"]

    raw_items = []
    try:
        with (directory / "news_raw.jsonl").open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                require(isinstance(value, dict), f"raw line {line_number} is not an object", errors)
                raw_items.append(value)
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"invalid news_raw.jsonl: {exc}")

    require(events_payload.get("schema_version") == "1.0", "unexpected events schema", errors)
    require(sources_payload.get("schema_version") == "1.0", "unexpected sources schema", errors)
    require(manifest.get("schema_version") == "1.0", "unexpected manifest schema", errors)
    require(events_payload.get("market") in {"US", "HK", "CN", "ALL"}, "invalid market", errors)
    window = events_payload.get("window", {})
    require(valid_iso(window.get("start")), "invalid window.start", errors)
    require(valid_iso(window.get("end")), "invalid window.end", errors)

    item_ids: set[str] = set()
    raw_required = {
        "item_id", "title", "url", "published_at", "source_id", "source_name",
        "source_tier", "source_kind", "markets", "topic", "summary",
        "filter_labels", "fetched_at",
    }
    for index, item in enumerate(raw_items):
        missing_fields = raw_required - item.keys()
        require(not missing_fields, f"raw item {index} missing {sorted(missing_fields)}", errors)
        item_id = item.get("item_id")
        require(isinstance(item_id, str) and bool(item_id), f"raw item {index} invalid item_id", errors)
        require(item_id not in item_ids, f"duplicate raw item_id: {item_id}", errors)
        item_ids.add(item_id)
        require(item.get("published_at") is None or valid_iso(item.get("published_at")),
                f"raw item {index} invalid published_at", errors)
        require(item.get("source_tier") in {1, 2, 3}, f"raw item {index} invalid tier", errors)

    event_ids: set[str] = set()
    event_required = {
        "event_id", "headline", "topic", "status", "first_published_at",
        "last_updated_at", "source_ids", "item_ids", "corroboration_count",
        "source_quality_score", "attention_score", "market_impact_score",
        "market_confirmation", "filter_labels",
    }
    events = events_payload.get("events")
    require(isinstance(events, list), "events must be a list", errors)
    if isinstance(events, list):
        for index, event in enumerate(events):
            missing_fields = event_required - event.keys()
            require(not missing_fields, f"event {index} missing {sorted(missing_fields)}", errors)
            event_id = event.get("event_id")
            require(isinstance(event_id, str) and bool(event_id), f"event {index} invalid id", errors)
            require(event_id not in event_ids, f"duplicate event_id: {event_id}", errors)
            event_ids.add(event_id)
            require(event.get("status") == "candidate", f"event {index} status must be candidate", errors)
            require(event.get("market_impact_score") is None,
                    f"event {index} market_impact_score must be null", errors)
            require(event.get("market_confirmation") == "pending",
                    f"event {index} market_confirmation must be pending", errors)
            require(isinstance(event.get("attention_score"), int) and
                    0 <= event["attention_score"] <= 10,
                    f"event {index} invalid attention_score", errors)
            require(isinstance(event.get("source_quality_score"), int) and
                    1 <= event["source_quality_score"] <= 5,
                    f"event {index} invalid source_quality_score", errors)
            members = event.get("item_ids", [])
            require(bool(members) and all(member in item_ids for member in members),
                    f"event {index} references unknown items", errors)
            require(event.get("corroboration_count") == len(set(event.get("source_ids", []))),
                    f"event {index} corroboration_count mismatch", errors)

    sources = sources_payload.get("sources")
    require(isinstance(sources, list) and bool(sources), "sources must be a non-empty list", errors)
    if isinstance(sources, list):
        valid_statuses = {"success", "empty", "error", "skipped"}
        source_ids = set()
        for index, source in enumerate(sources):
            source_id = source.get("source_id")
            require(source_id not in source_ids, f"duplicate source ledger id: {source_id}", errors)
            source_ids.add(source_id)
            require(source.get("status") in valid_statuses,
                    f"source {index} invalid status", errors)

    stats = manifest.get("stats", {})
    require(stats.get("deduplicated_items") == len(raw_items),
            "manifest deduplicated_items mismatch", errors)
    if isinstance(events, list):
        require(stats.get("events") == len(events), "manifest events mismatch", errors)
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    errors = validate(args.output_dir)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"OK: validated {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

