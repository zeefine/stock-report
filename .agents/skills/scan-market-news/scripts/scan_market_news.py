#!/usr/bin/env python3
"""Fetch public RSS/Atom feeds and build a deterministic market-news event pack."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import ipaddress
import json
import os
import re
import socket
import sys
import tempfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "1.0"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = SCRIPT_DIR.parent / "references" / "sources.json"
TRACKING_KEYS = {
    "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src", "source",
    "spm", "igshid", "mkt_tok",
}
EVENT_KEYWORDS = {
    "earnings", "guidance", "forecast", "merger", "acquisition", "takeover",
    "regulation", "regulator", "investigation", "probe", "tariff", "sanction",
    "recall", "bankruptcy", "restructuring", "layoff", "funding", "offering",
    "rate cut", "rate hike", "inflation", "employment", "jobs report", "cpi",
    "fomc", "fed", "ipo", "approval", "launch",
    "财报", "业绩", "指引", "并购", "收购", "监管", "调查", "关税", "制裁",
    "召回", "破产", "重组", "裁员", "融资", "增发", "降息", "加息", "通胀",
    "就业", "上市", "获批", "发布",
}
SYNTHETIC_DNS_RANGES = (ipaddress.ip_network("198.18.0.0/15"),)
OFFICIAL_SOURCE_NAMES = {"sec", "federal reserve", "nasa"}
COMPANY_SOURCE_NAMES = {
    "openai", "google research", "hugging face", "deepmind", "github blog",
}
RESEARCH_NAME_MARKERS = {"arxiv", "ieee", "nature", "mit news", "bair"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso_z(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def strip_html(value: str | None, limit: int = 280) -> str:
    text = html.unescape(value or "")
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def node_text(node: ET.Element) -> str:
    return "".join(node.itertext()).strip()


def canonicalize_url(value: str | None) -> str:
    if not value:
        return ""
    value = html.unescape(value.strip())
    parts = urllib.parse.urlsplit(value)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return ""
    host = parts.hostname.lower().rstrip(".")
    port = parts.port
    netloc = host
    if port and not ((parts.scheme.lower() == "http" and port == 80) or
                     (parts.scheme.lower() == "https" and port == 443)):
        netloc = f"{host}:{port}"
    query = []
    for key, val in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered.startswith("utm_") or lowered in TRACKING_KEYS:
            continue
        query.append((key, val))
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    return urllib.parse.urlunsplit(
        (parts.scheme.lower(), netloc, path, urllib.parse.urlencode(query, doseq=True), "")
    )


def ensure_public_url(value: str) -> None:
    parts = urllib.parse.urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("only public http/https URLs are allowed")
    host = parts.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise ValueError("local host is not allowed")
    # In managed environments, urllib may route through an explicit HTTP(S)
    # proxy whose DNS layer maps public hosts to a reserved proxy range such
    # as 198.18.0.0/15. Validate the URL shape above, but do not reject that
    # proxy mapping as if it were the destination address.
    if any(os.environ.get(key) for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")):
        return
    default_port = 443 if parts.scheme == "https" else 80
    try:
        addresses = socket.getaddrinfo(host, parts.port or default_port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"DNS lookup failed: {exc}") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if any(ip in network for network in SYNTHETIC_DNS_RANGES):
            # Some managed runners use 198.18.0.0/15 as a synthetic mapping
            # for public egress. It is not a reachable destination address.
            continue
        if not ip.is_global:
            raise ValueError(f"non-public address is not allowed: {ip}")


def normalized_title(value: str) -> str:
    value = html.unescape(value).casefold()
    value = re.sub(r"https?://\S+", " ", value)
    value = re.sub(r"[^\w\u4e00-\u9fff]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def title_tokens(value: str) -> set[str]:
    normalized = normalized_title(value)
    tokens = set(re.findall(r"[a-z0-9]{2,}", normalized))
    cjk_chunks = re.findall(r"[\u4e00-\u9fff]+", normalized)
    for chunk in cjk_chunks:
        if len(chunk) == 1:
            tokens.add(chunk)
        else:
            tokens.update(chunk[index:index + 2] for index in range(len(chunk) - 1))
    return tokens


def title_similarity(left: str, right: str) -> float:
    left_norm = normalized_title(left)
    right_norm = normalized_title(right)
    if not left_norm or not right_norm:
        return 0.0
    left_tokens = title_tokens(left)
    right_tokens = title_tokens(right)
    union = left_tokens | right_tokens
    jaccard = len(left_tokens & right_tokens) / len(union) if union else 0.0
    sequence = SequenceMatcher(None, left_norm, right_norm).ratio()
    return max(jaccard, sequence)


def stable_id(prefix: str, *parts: str) -> str:
    material = "\x1f".join(parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(material).hexdigest()[:16]}"


def normalize_legacy_config(config: dict[str, Any]) -> dict[str, Any]:
    """Accept the user's original hint/redline/timeout source format."""
    sources = config.get("sources")
    if not isinstance(sources, list) or not sources:
        return config
    if all("id" in source for source in sources):
        return config
    normalized = dict(config)
    normalized_sources = []
    for source in sources:
        name = str(source.get("name", "")).strip()
        url = str(source.get("url", "")).strip()
        lowered = name.casefold()
        if lowered in OFFICIAL_SOURCE_NAMES:
            source_kind, source_tier = "official", 1
        elif lowered in COMPANY_SOURCE_NAMES:
            source_kind, source_tier = "company", 1
        elif any(marker in lowered for marker in RESEARCH_NAME_MARKERS):
            source_kind, source_tier = "research", 2
        else:
            source_kind, source_tier = "media", 2
        normalized_sources.append({
            "id": stable_id("source", name, url),
            "name": name,
            "topic": source.get("topic") or source.get("hint") or "general",
            "type": source.get("type", "rss"),
            "url": url,
            "markets": source.get("markets") or ["US", "HK", "CN", "GLOBAL"],
            "source_tier": source.get("source_tier", source_tier),
            "source_kind": source.get("source_kind", source_kind),
            "enabled": source.get("enabled", True),
        })
    normalized["sources"] = normalized_sources
    if "filter_keywords" not in normalized:
        normalized["filter_keywords"] = config.get("redline_keywords", [])
    fetch = dict(config.get("fetch", {}))
    if "timeout" in fetch and "timeout_seconds" not in fetch:
        fetch["timeout_seconds"] = fetch["timeout"]
    normalized["fetch"] = fetch
    return normalized


def extract_entries(raw: bytes, source: dict[str, Any], fetched_at: datetime) -> list[dict[str, Any]]:
    root = ET.fromstring(raw)
    entries = [node for node in root.iter() if local_name(node.tag) in {"item", "entry"}]
    output: list[dict[str, Any]] = []
    for entry in entries:
        title = ""
        url = ""
        raw_date = ""
        summary = ""
        for child in entry:
            tag = local_name(child.tag)
            text = node_text(child)
            if tag == "title" and not title:
                title = strip_html(text, 500)
            elif tag == "link" and not url:
                url = child.attrib.get("href", "").strip() or text
            elif tag in {"pubdate", "published", "updated", "date"} and not raw_date:
                raw_date = text
            elif tag in {"description", "summary", "content", "encoded"} and not summary:
                summary = strip_html(text)
        if not title:
            continue
        canonical_url = canonicalize_url(url)
        published = parse_datetime(raw_date)
        item_id = stable_id(
            "item",
            source["id"],
            canonical_url or normalized_title(title),
            iso_z(published) or "undated",
        )
        output.append({
            "item_id": item_id,
            "title": title,
            "url": canonical_url,
            "published_at": iso_z(published),
            "source_id": source["id"],
            "source_name": source["name"],
            "source_tier": source["source_tier"],
            "source_kind": source["source_kind"],
            "markets": source["markets"],
            "topic": source["topic"],
            "summary": summary,
            "filter_labels": [],
            "fetched_at": iso_z(fetched_at),
        })
    return output


def read_response(response: Any, max_bytes: int) -> bytes:
    content_length = response.headers.get("Content-Length")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                raise ValueError(f"response exceeds {max_bytes} bytes")
        except ValueError as exc:
            if "exceeds" in str(exc):
                raise
    raw = response.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError(f"response exceeds {max_bytes} bytes")
    if response.headers.get("Content-Encoding", "").lower() == "gzip":
        raw = gzip.decompress(raw)
        if len(raw) > max_bytes:
            raise ValueError(f"decompressed response exceeds {max_bytes} bytes")
    return raw


def fetch_source(
    source: dict[str, Any],
    start: datetime,
    end: datetime,
    timeout: int,
    max_bytes: int,
    per_source: int,
    filter_keywords: list[str],
    filter_mode: str,
    user_agent: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    fetched_at = utc_now()
    ledger = {
        "source_id": source["id"],
        "name": source["name"],
        "url": source["url"],
        "status": "error",
        "items_received": 0,
        "error": None,
        "fetched_at": iso_z(fetched_at),
    }
    try:
        ensure_public_url(source["url"])
        request = urllib.request.Request(
            source["url"],
            headers={
                "User-Agent": user_agent,
                "Accept": "application/rss+xml,application/atom+xml,application/xml,text/xml,*/*",
                "Accept-Encoding": "identity",
            },
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            ensure_public_url(response.geturl())
            raw = read_response(response, max_bytes)
        items = extract_entries(raw, source, fetched_at)
        selected = []
        for item in items:
            published = parse_datetime(item["published_at"])
            if published is not None and not (start <= published <= end):
                continue
            blob = f"{item['title']} {item['summary']}".casefold()
            labels = sorted({keyword for keyword in filter_keywords if keyword in blob})
            item["filter_labels"] = labels
            if labels and filter_mode == "drop":
                continue
            selected.append(item)
        selected.sort(
            key=lambda item: parse_datetime(item["published_at"]) or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        selected = selected[:per_source]
        ledger["items_received"] = len(selected)
        ledger["status"] = "success" if selected else "empty"
        return selected, ledger
    except Exception as exc:  # keep the run alive; disclose failure in the source ledger
        ledger["error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
        return [], ledger


def validate_config(config: dict[str, Any]) -> None:
    if not isinstance(config.get("sources"), list) or not config["sources"]:
        raise ValueError("config.sources must be a non-empty list")
    ids: set[str] = set()
    for source in config["sources"]:
        required = {
            "id", "name", "topic", "type", "url", "markets",
            "source_tier", "source_kind", "enabled",
        }
        missing = required - source.keys()
        if missing:
            raise ValueError(f"source missing fields {sorted(missing)}: {source!r}")
        if source["id"] in ids:
            raise ValueError(f"duplicate source id: {source['id']}")
        ids.add(source["id"])
        if source["type"] != "rss":
            raise ValueError(f"unsupported source type for {source['id']}: {source['type']}")
        if source["source_tier"] not in {1, 2, 3}:
            raise ValueError(f"invalid source tier for {source['id']}")
        if not isinstance(source["markets"], list) or not source["markets"]:
            raise ValueError(f"markets must be a non-empty list for {source['id']}")
        canonical = canonicalize_url(source["url"])
        if not canonical:
            raise ValueError(f"invalid URL for {source['id']}")


def select_sources(config: dict[str, Any], market: str) -> list[dict[str, Any]]:
    selected = []
    for source in config["sources"]:
        if not source.get("enabled", True):
            continue
        markets = set(source["markets"])
        if market == "ALL" or market in markets or "GLOBAL" in markets:
            selected.append(source)
    return selected


def event_date_distance_hours(left: dict[str, Any], right: dict[str, Any]) -> float | None:
    left_dt = parse_datetime(left["published_at"])
    right_dt = parse_datetime(right["published_at"])
    if left_dt is None or right_dt is None:
        return None
    return abs((left_dt - right_dt).total_seconds()) / 3600


def should_cluster(
    item: dict[str, Any],
    representative: dict[str, Any],
    threshold: float,
    window_hours: int,
) -> bool:
    if item["url"] and item["url"] == representative["url"]:
        return True
    distance = event_date_distance_hours(item, representative)
    if distance is not None and distance > window_hours:
        return False
    similarity = title_similarity(item["title"], representative["title"])
    if item["topic"] != representative["topic"]:
        return similarity >= max(0.84, threshold + 0.2)
    return similarity >= threshold


def cluster_items(
    items: list[dict[str, Any]], threshold: float, window_hours: int
) -> list[list[dict[str, Any]]]:
    ordered = sorted(
        items,
        key=lambda item: parse_datetime(item["published_at"]) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    clusters: list[list[dict[str, Any]]] = []
    for item in ordered:
        for cluster in clusters:
            representative = min(
                cluster,
                key=lambda candidate: (candidate["source_tier"], -len(candidate["title"])),
            )
            if should_cluster(item, representative, threshold, window_hours):
                cluster.append(item)
                break
        else:
            clusters.append([item])
    return clusters


def source_quality_score(cluster: list[dict[str, Any]]) -> int:
    best_tier = min(item["source_tier"] for item in cluster)
    independent_sources = len({item["source_id"] for item in cluster})
    if best_tier == 1:
        return 5
    if best_tier == 2 and independent_sources >= 2:
        return 4
    if best_tier == 2:
        return 3
    return 2


def attention_score(
    cluster: list[dict[str, Any]], source_quality: int, window_end: datetime
) -> int:
    score = 3 if source_quality >= 5 else 2 if source_quality >= 3 else 1
    independent_sources = len({item["source_id"] for item in cluster})
    score += min(2, max(0, independent_sources - 1))
    dates = [parse_datetime(item["published_at"]) for item in cluster]
    dates = [date for date in dates if date is not None]
    if dates:
        age_hours = max(0.0, (window_end - max(dates)).total_seconds() / 3600)
        score += 2 if age_hours <= 12 else 1 if age_hours <= 36 else 0
    blob = " ".join(f"{item['title']} {item['summary']}" for item in cluster).casefold()
    matches = sum(1 for keyword in EVENT_KEYWORDS if keyword in blob)
    score += 2 if matches >= 2 else 1 if matches == 1 else 0
    markets = {market for item in cluster for market in item["markets"]}
    if cluster[0]["topic"] == "macro" or len(markets - {"GLOBAL"}) >= 2:
        score += 1
    return min(10, score)


def build_events(
    clusters: list[list[dict[str, Any]]], market: str, window_end: datetime
) -> list[dict[str, Any]]:
    events = []
    for cluster in clusters:
        representative = min(
            cluster,
            key=lambda item: (item["source_tier"], -len(item["title"])),
        )
        dates = [parse_datetime(item["published_at"]) for item in cluster]
        dated = sorted(date for date in dates if date is not None)
        quality = source_quality_score(cluster)
        sources = sorted({item["source_id"] for item in cluster})
        labels = sorted({label for item in cluster for label in item["filter_labels"]})
        date_key = dated[0].strftime("%Y%m%d") if dated else "undated"
        event_id = stable_id(
            f"event-{market.lower()}-{date_key}",
            normalized_title(representative["title"]),
        )
        events.append({
            "event_id": event_id,
            "headline": representative["title"],
            "topic": representative["topic"],
            "status": "candidate",
            "first_published_at": iso_z(dated[0]) if dated else None,
            "last_updated_at": iso_z(dated[-1]) if dated else None,
            "source_ids": sources,
            "item_ids": sorted(item["item_id"] for item in cluster),
            "corroboration_count": len(sources),
            "source_quality_score": quality,
            "attention_score": attention_score(cluster, quality, window_end),
            "market_impact_score": None,
            "market_confirmation": "pending",
            "filter_labels": labels,
        })
    events.sort(
        key=lambda event: (
            event["attention_score"],
            event["source_quality_score"],
            event["last_updated_at"] or "",
        ),
        reverse=True,
    )
    return events


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def atomic_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", choices=["US", "HK", "CN", "ALL"], required=True)
    parser.add_argument("--start", required=True, help="UTC/offset-aware ISO 8601 start")
    parser.add_argument("--end", required=True, help="UTC/offset-aware ISO 8601 end")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--per-source", type=int)
    parser.add_argument("--timeout", type=int)
    parser.add_argument("--workers", type=int)
    parser.add_argument("--filter-mode", choices=["label", "drop"], default="label")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    start = parse_datetime(args.start)
    end = parse_datetime(args.end)
    if start is None or end is None:
        print("start and end must be valid offset-aware ISO 8601 values", file=sys.stderr)
        return 2
    if start >= end:
        print("start must be earlier than end", file=sys.stderr)
        return 2
    config_path = args.config.resolve()
    try:
        with config_path.open(encoding="utf-8") as handle:
            config = json.load(handle)
        config = normalize_legacy_config(config)
        validate_config(config)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"invalid config: {exc}", file=sys.stderr)
        return 2

    fetch_config = config.get("fetch", {})
    per_source = args.per_source or int(fetch_config.get("per_source", 6))
    timeout = args.timeout or int(fetch_config.get("timeout_seconds", 20))
    workers = args.workers or int(fetch_config.get("max_workers", 12))
    max_bytes = int(fetch_config.get("max_response_bytes", 2_097_152))
    threshold = float(fetch_config.get("cluster_similarity", 0.56))
    window_hours = int(fetch_config.get("cluster_window_hours", 72))
    if per_source < 1 or timeout < 1 or workers < 1 or max_bytes < 1024:
        print("fetch limits must be positive", file=sys.stderr)
        return 2

    sources = select_sources(config, args.market)
    if not sources:
        print(f"no enabled sources for market {args.market}", file=sys.stderr)
        return 2
    keywords = [str(value).casefold() for value in config.get("filter_keywords", [])]
    user_agent = os.environ.get(
        "MARKET_NEWS_USER_AGENT",
        "scan-market-news/1.0 (public RSS research; configure MARKET_NEWS_USER_AGENT)",
    )

    all_items: list[dict[str, Any]] = []
    ledgers: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(workers, len(sources))) as executor:
        future_map = {
            executor.submit(
                fetch_source,
                source,
                start,
                end,
                timeout,
                max_bytes,
                per_source,
                keywords,
                args.filter_mode,
                user_agent,
            ): source
            for source in sources
        }
        for future in as_completed(future_map):
            items, ledger = future.result()
            all_items.extend(items)
            ledgers.append(ledger)

    ledgers.sort(key=lambda row: row["source_id"])
    successful = [row for row in ledgers if row["status"] in {"success", "empty"}]
    if not successful:
        source_payload = {
            "schema_version": SCHEMA_VERSION,
            "market": args.market,
            "sources": ledgers,
        }
        atomic_json(args.output_dir / "news_sources.json", source_payload)
        print("all selected sources failed; see news_sources.json", file=sys.stderr)
        return 3

    # Retain per-source records while removing repeated IDs within malformed feeds.
    unique_items = {item["item_id"]: item for item in all_items}
    items = sorted(
        unique_items.values(),
        key=lambda item: parse_datetime(item["published_at"]) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    clusters = cluster_items(items, threshold, window_hours)
    events = build_events(clusters, args.market, end)
    generated_at = utc_now()

    raw_path = args.output_dir / "news_raw.jsonl"
    events_path = args.output_dir / "news_events.json"
    sources_path = args.output_dir / "news_sources.json"
    manifest_path = args.output_dir / "manifest.json"
    atomic_jsonl(raw_path, items)
    atomic_json(events_path, {
        "schema_version": SCHEMA_VERSION,
        "market": args.market,
        "window": {"start": iso_z(start), "end": iso_z(end)},
        "events": events,
    })
    atomic_json(sources_path, {
        "schema_version": SCHEMA_VERSION,
        "market": args.market,
        "sources": ledgers,
    })
    failures = sum(row["status"] == "error" for row in ledgers)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "market": args.market,
        "window": {"start": iso_z(start), "end": iso_z(end)},
        "generated_at": iso_z(generated_at),
        "config_path": str(config_path),
        "config_sha256": sha256_file(config_path),
        "filter_mode": args.filter_mode,
        "stats": {
            "selected_sources": len(sources),
            "successful_sources": len(successful),
            "failed_sources": failures,
            "raw_items": len(all_items),
            "deduplicated_items": len(items),
            "events": len(events),
        },
        "outputs": {
            "raw": raw_path.name,
            "events": events_path.name,
            "sources": sources_path.name,
        },
    }
    atomic_json(manifest_path, manifest)
    print(
        f"{args.market}: {len(successful)}/{len(sources)} sources completed, "
        f"{len(items)} items, {len(events)} candidate events, {failures} failures"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
