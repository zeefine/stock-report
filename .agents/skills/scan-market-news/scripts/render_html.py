#!/usr/bin/env python3
"""Render scan-market-news outputs into one self-contained HTML dashboard."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_TEMPLATE = SCRIPT_DIR.parent / "assets" / "news-dashboard.html"
REQUIRED_FILES = (
    "news_raw.jsonl",
    "news_events.json",
    "news_sources.json",
    "manifest.json",
)
PLACEHOLDER = "__MARKET_NEWS_DATA__"


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path.name} line {line_number} is not an object")
            rows.append(value)
    return rows


def safe_embedded_json(payload: Any) -> str:
    return (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def render_dashboard(input_dir: Path, output: Path, template: Path = DEFAULT_TEMPLATE) -> Path:
    missing = [name for name in REQUIRED_FILES if not (input_dir / name).is_file()]
    if missing:
        raise FileNotFoundError(f"missing input files: {', '.join(missing)}")
    template_text = template.read_text(encoding="utf-8")
    if template_text.count(PLACEHOLDER) != 1:
        raise ValueError(f"template must contain exactly one {PLACEHOLDER}")
    payload = {
        "raw": load_jsonl(input_dir / "news_raw.jsonl"),
        "events": load_json(input_dir / "news_events.json"),
        "sources": load_json(input_dir / "news_sources.json"),
        "manifest": load_json(input_dir / "manifest.json"),
    }
    rendered = template_text.replace(PLACEHOLDER, safe_embedded_json(payload))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=output.parent, delete=False
    ) as handle:
        handle.write(rendered)
        temporary = Path(handle.name)
    os.replace(temporary, output)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    args = parser.parse_args()
    output = args.output or args.input_dir / "news_dashboard.html"
    try:
        result = render_dashboard(args.input_dir, output, args.template)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(f"HTML generated: {result.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
