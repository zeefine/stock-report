from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_news_events(news_dir: Path) -> list[dict[str, Any]]:
    path = news_dir / "news_events.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    events = payload.get("events", []) if isinstance(payload, dict) else []
    if not isinstance(events, list):
        return []
    return sorted(
        events,
        key=lambda event: (
            event.get("attention_score") or 0,
            event.get("corroboration_count") or 0,
        ),
        reverse=True,
    )
