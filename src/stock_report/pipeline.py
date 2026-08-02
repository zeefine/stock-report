from __future__ import annotations

import json
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from typing import cast

from .common import first_existing, load_config, project_root, validate_date
from .models import Market, PipelineResult, RunContext
from .quality import validate_report


def build_context(
    market: str,
    report_date: str | None = None,
    *,
    root: Path | None = None,
    config_path: Path | None = None,
) -> RunContext:
    if market not in {"us", "cn", "hk"}:
        raise ValueError("market 必须是 us、cn 或 hk")
    base = (root or project_root()).resolve()
    config = load_config(config_path or base / "config.yaml")
    market_config = config["markets"].get(market)
    if not isinstance(market_config, dict):
        raise RuntimeError(f"config.yaml 缺少市场配置：{market}")
    project = config.get("project", {})
    template_name = market_config["template"]
    prompt_name = market_config["prompt"]
    template = first_existing(
        [
            base / project.get("templates_dir", "templates") / template_name,
            base / ("A股收盘日报_template.html" if market == "cn" else "美股收盘日报_template.html"),
        ]
    )
    if template is None:
        raise RuntimeError(f"没有找到 {market} 模板：{template_name}")
    prompt = first_existing(
        [
            base / project.get("prompts_dir", "prompts") / prompt_name,
            base / ("A股收盘日报_prompt_优化版.md" if market == "cn" else "美股收盘日报_prompt_优化版.md"),
        ]
    )
    if prompt is None:
        raise RuntimeError(f"没有找到 {market} prompt：{prompt_name}")
    return RunContext(
        root=base,
        market=cast(Market, market),
        report_date=validate_date(report_date),
        timezone=market_config["timezone"],
        runs_dir=base / project.get("runs_dir", "runs"),
        reports_dir=base / project.get("reports_dir", "reports"),
        template=template,
        prompt=prompt,
        config=config,
    )


def _evidence_path(context: RunContext, report_date: str) -> Path:
    return context.market_run_dir(report_date) / "market_data" / "evidence.json"


def _write_manifest(
    context: RunContext,
    report_date: str,
    report_path: Path,
    evidence_path: Path,
    quality,
) -> Path:
    manifest_path = context.market_run_dir(report_date) / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "market": context.market,
        "report_date": report_date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "report_path": str(report_path),
        "evidence_path": str(evidence_path) if evidence_path.exists() else None,
        "template_path": str(context.template),
        "prompt_path": str(context.prompt),
        "backend": "native-market-module",
        "quality": quality.to_dict(),
    }
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path


def run_report(
    market: str,
    report_date: str | None = None,
    *,
    root: Path | None = None,
    config_path: Path | None = None,
) -> PipelineResult:
    context = build_context(market, report_date, root=root, config_path=config_path)
    adapter = import_module(f"stock_report.markets.{market}")
    resolved_date, report_path = adapter.run(context)
    evidence_path = _evidence_path(context, resolved_date)
    quality = validate_report(report_path, evidence_path)
    manifest_path = _write_manifest(context, resolved_date, report_path, evidence_path, quality)
    return PipelineResult(
        market=cast(Market, market),
        report_date=resolved_date,
        report_path=report_path,
        evidence_path=evidence_path if evidence_path.exists() else None,
        manifest_path=manifest_path,
        quality=quality,
    )


def validate_existing(
    market: str,
    report_date: str,
    *,
    root: Path | None = None,
    config_path: Path | None = None,
):
    context = build_context(market, report_date, root=root, config_path=config_path)
    suffix = "_Asia-Shanghai" if market == "cn" else ""
    prefix = "A股收盘日报" if market == "cn" else "美股收盘日报"
    report_path = context.reports_dir / f"{prefix}_{report_date}{suffix}.html"
    return validate_report(report_path, _evidence_path(context, report_date))
