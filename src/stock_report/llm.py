from __future__ import annotations

from pathlib import Path
from typing import Any


def narrative_enabled(config: dict[str, Any]) -> bool:
    return bool(config.get("pipeline", {}).get("llm_enabled", False))


def build_narrative(prompt: Path, evidence: dict[str, Any], config: dict[str, Any]) -> dict[str, str]:
    """Reserved boundary for optional prose enhancement.

    Deterministic reports do not call an LLM. A future provider must return only
    named narrative fields and may not alter evidence, metrics or source records.
    """
    if not narrative_enabled(config):
        return {}
    raise RuntimeError("LLM 叙事增强尚未配置；请保持 config.yaml 中 llm_enabled=false")
