from __future__ import annotations

import json
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from typing import cast

from .common import (
    cn_session_state, first_existing, load_config, now_in, parse_as_of, project_root,
    resolve_report_mode, validate_date,
)
from .models import Market, PipelineResult, ReportMode, RunContext
from .quality import validate_report


def build_context(
    market: str,
    report_date: str | None = None,
    *,
    mode: str = "auto",
    as_of: str | None = None,
    refresh: bool = False,
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
    validated_date = validate_date(report_date)
    moment = now_in(market_config["timezone"])
    resolved_mode = resolve_report_mode(market, mode, validated_date, moment)
    if (
        market == "cn" and resolved_mode == "close"
        and (validated_date is None or validated_date == moment.date().isoformat())
        and cn_session_state(moment) in {
            "auction", "morning", "lunch_break", "afternoon", "closing_pending"
        }
    ):
        raise RuntimeError("当日收盘数据尚未稳定；请使用 --mode intraday，或在15:15后运行 --mode close")
    template_name = market_config.get(
        "intraday_template" if resolved_mode == "intraday" else "close_template",
        market_config.get("template"),
    )
    if not template_name:
        raise RuntimeError(f"config.yaml 缺少 {market} 的 {resolved_mode} 模板")
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
        report_date=validated_date,
        mode=cast(ReportMode, resolved_mode),
        as_of=parse_as_of(as_of, market_config["timezone"], validated_date, moment),
        refresh=refresh,
        timezone=market_config["timezone"],
        runs_dir=base / project.get("runs_dir", "runs"),
        reports_dir=base / project.get("reports_dir", "reports"),
        template=template,
        prompt=prompt,
        config=config,
        as_of_explicit=as_of is not None,
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
    effective_as_of = context.as_of.isoformat()
    if evidence_path.exists():
        try:
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            if isinstance(evidence, dict):
                effective_as_of = evidence.get("as_of") or effective_as_of
        except (OSError, json.JSONDecodeError):
            pass
    payload = {
        "schema_version": "1.0",
        "market": context.market,
        "report_date": report_date,
        "mode": context.mode,
        "as_of": effective_as_of,
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
    mode: str = "auto",
    as_of: str | None = None,
    refresh: bool = False,
    root: Path | None = None,
    config_path: Path | None = None,
) -> PipelineResult:
    context = build_context(
        market, report_date, mode=mode, as_of=as_of, refresh=refresh,
        root=root, config_path=config_path,
    )
    adapter = import_module(f"stock_report.markets.{market}")
    resolved_date, report_path = adapter.run(context)
    evidence_path = _evidence_path(context, resolved_date)
    quality = validate_report(report_path, evidence_path, market=context.market,
                              mode=context.mode, report_date=resolved_date)
    manifest_path = _write_manifest(context, resolved_date, report_path, evidence_path, quality)
    return PipelineResult(
        market=cast(Market, market),
        report_date=resolved_date,
        mode=context.mode,
        report_path=report_path,
        evidence_path=evidence_path if evidence_path.exists() else None,
        manifest_path=manifest_path,
        quality=quality,
    )


def validate_existing(
    market: str,
    report_date: str,
    *,
    mode: str = "close",
    root: Path | None = None,
    config_path: Path | None = None,
):
    context = build_context(
        market, report_date, mode=mode, root=root, config_path=config_path
    )
    if market == "cn" and context.mode == "intraday":
        report_path = context.reports_dir / "A股盘中快报_latest.html"
    else:
        suffix = "_Asia-Shanghai" if market == "cn" else ""
        prefix = "A股收盘日报" if market == "cn" else "美股收盘日报"
        report_path = context.reports_dir / f"{prefix}_{report_date}{suffix}.html"
    return validate_report(report_path, _evidence_path(context, report_date),
                           market=context.market, mode=context.mode, report_date=report_date)
