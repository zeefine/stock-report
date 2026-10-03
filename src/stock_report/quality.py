from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .models import QualityIssue, QualityResult
from .common import cn_timestamp, cn_close_quote_errors, cn_close_minute_checks, cn_close_price_mismatches

PLACEHOLDER_RE = re.compile(r"\{\{[A-Z0-9_]+\}\}")


def unresolved_placeholders(text: str) -> list[str]:
    return sorted(set(PLACEHOLDER_RE.findall(text)))


def _validate_cn_close(payload, evidence_path, issues, checks):
    if payload.get("market") != "cn" or payload.get("report_type") != "close":
        issues.append(QualityIssue("close_metadata_invalid", "A股收盘证据必须标明 market=cn、report_type=close"))
    if payload.get("is_final") is not True:
        issues.append(QualityIssue("close_not_final", "核心收盘行情尚未确认，不能视为正式收盘报告"))
    cutoff = cn_timestamp(payload.get("as_of"))
    quotes = payload.get("quotes")
    quotes = quotes if isinstance(quotes, dict) else {}
    if cutoff is None or cutoff.date().isoformat() != payload.get("report_date"):
        issues.append(QualityIssue("invalid_close_cutoff", "收盘截止时间缺失或与报告日不一致"))
        return
    invalid = cn_close_quote_errors(quotes, cutoff)
    checks["invalid_core_close_quotes"] = invalid
    if invalid:
        issues.append(QualityIssue("core_close_quotes_invalid", "核心指数报价缺失、数值无效或不是报告日收盘：" + ", ".join(invalid)))
    try:
        minute_pack = json.loads((evidence_path.parent / "minute_indices.json").read_text(encoding="utf-8"))
        if not isinstance(minute_pack, dict) or minute_pack.get("report_date") != payload.get("report_date"):
            raise ValueError("分钟线报告日期不一致")
        symbols = minute_pack.get("symbols")
        if not isinstance(symbols, dict):
            raise ValueError("缺少分钟线 symbols")
        minute_checks = cn_close_minute_checks(symbols, quotes, payload["report_date"])
        coverage = sum(row["valid"] for row in minute_checks.values()) / len(minute_checks)
        checks["verified_close_minute_coverage"] = coverage
        mismatches = cn_close_price_mismatches(minute_checks)
        checks["core_close_price_mismatches"] = mismatches
        if mismatches:
            issues.append(QualityIssue("core_close_price_mismatch", "核心报价与分钟收盘价偏差超过0.2%：" + ", ".join(mismatches)))
        if coverage < 0.8:
            issues.append(QualityIssue("core_close_minutes_incomplete", "核心指数完整分钟线覆盖率不足80%，或与收盘价不一致"))
    except (OSError, ValueError, TypeError) as exc:
        issues.append(QualityIssue("close_minutes_invalid", f"无法核验报告日分钟线：{exc}"))


def validate_report(report_path: Path, evidence_path: Path | None = None, *,
                    market: str | None = None, mode: str | None = None,
                    report_date: str | None = None) -> QualityResult:
    issues: list[QualityIssue] = []
    checks: dict[str, Any] = {}
    expected_cn_close = (market == "cn" and mode == "close") or report_path.name.startswith("A股收盘日报_")
    if not report_path.exists():
        issues.append(QualityIssue("report_missing", f"报告不存在：{report_path}"))
        return QualityResult(False, issues, checks)
    text = report_path.read_text(encoding="utf-8")
    placeholders = unresolved_placeholders(text)
    checks.update({"report_bytes": report_path.stat().st_size, "unresolved_placeholders": placeholders})
    if placeholders:
        issues.append(QualityIssue("unresolved_placeholders", "仍有模板字段未渲染：" + ", ".join(placeholders)))
    if "�" in text:
        issues.append(QualityIssue("replacement_character", "HTML 中出现 Unicode 替换字符，可能存在编码错误"))
    if evidence_path is None or not evidence_path.exists():
        issues.append(QualityIssue("evidence_missing", "未找到 evidence.json", "error" if expected_cn_close else "warning"))
        checks["evidence_exists"] = False
    else:
        checks["evidence_exists"] = True
        try:
            payload = json.loads(evidence_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                issues.append(QualityIssue("evidence_invalid", "证据必须是JSON对象"))
                return QualityResult(False, issues, checks)
            evidence_date = payload.get("report_date") if isinstance(payload, dict) else None
            if report_date is not None and evidence_date != report_date:
                issues.append(QualityIssue("requested_date_mismatch", "证据日期与请求日期不一致"))
            if market and (market == "cn" or "market" in payload) and payload.get("market") != market:
                issues.append(QualityIssue("market_mismatch", "证据市场与请求市场不一致"))
            if mode and payload.get("report_type") != mode and market == "cn":
                issues.append(QualityIssue("mode_mismatch", "证据模式与请求模式不一致"))
            if expected_cn_close or (payload.get("market") == "cn" and payload.get("report_type") == "close"):
                _validate_cn_close(payload, evidence_path, issues, checks)
            checks["evidence_report_date"] = evidence_date
            filename_date = re.search(r"\d{4}-\d{2}-\d{2}", report_path.name)
            if evidence_date and filename_date and evidence_date != filename_date.group(0):
                issues.append(QualityIssue("date_mismatch", f"报告文件日期 {filename_date.group(0)} 与证据日期 {evidence_date} 不一致"))
            if isinstance(payload, dict) and payload.get("market") == "cn":
                cutoff = cn_timestamp(payload.get("as_of"))
                if cutoff is None or cutoff.date().isoformat() != evidence_date:
                    issues.append(QualityIssue("invalid_cutoff", "A股截止时间缺失或不属于报告日期"))
                else:
                    invalid_quotes = []
                    quotes = payload.get("quotes") or {}
                    if not isinstance(quotes, dict):
                        quotes = {}
                        issues.append(QualityIssue("quotes_invalid", "quotes 必须是JSON对象"))
                    for symbol, quote in quotes.items():
                        if not isinstance(quote, dict):
                            invalid_quotes.append(symbol)
                            continue
                        observed = cn_timestamp(quote.get("time"))
                        if (observed is None or observed.date() != cutoff.date() or observed > cutoff
                                or (payload.get("report_type") == "close" and observed.hour < 15)):
                            invalid_quotes.append(symbol)
                    checks["quotes_outside_cutoff"] = invalid_quotes
                    if invalid_quotes:
                        issues.append(QualityIssue("quotes_outside_cutoff", "报价时间缺失或超出报告时段：" + ", ".join(invalid_quotes)))
                    if any(payload.get(key) for key in ("eastmoney_quotes", "industries", "industry_flow", "concept_flow")) or payload.get("limit_pool_available"):
                        captured = cn_timestamp(payload.get("snapshot_as_of"))
                        if captured is None or captured.date() != cutoff.date() or captured > cutoff:
                            issues.append(QualityIssue("snapshot_outside_cutoff", "快照字段缺少截止前同日采集时间"))
            intraday = payload.get("intraday_quality") if isinstance(payload, dict) else None
            if isinstance(intraday, dict):
                checks["report_mode"] = intraday.get("mode")
                checks["is_final"] = intraday.get("is_final")
                checks["intraday_ready"] = intraday.get("ready")
                checks["intraday_core_index_coverage"] = intraday.get("core_index_coverage")
                checks["intraday_relevant_events"] = intraday.get("a_share_relevant_event_count")
                if not intraday.get("ready"):
                    message = (
                        "分钟线尚未覆盖到最近完成的5分钟周期"
                        if intraday.get("mode") == "intraday"
                        else "盘中节奏/事件章节证据不足，已降级；核心收盘行情另行校验"
                    )
                    issues.append(QualityIssue("intraday_not_ready", message, "warning"))
            if isinstance(payload, dict) and "SPY" in payload:
                core_rows = {
                    symbol: len((payload.get(symbol) or {}).get("rows", []))
                    for symbol in ("SPY", "QQQ", "DIA", "IWM", "RSP")
                }
                checks["core_daily_rows"] = core_rows
                if any(count < 260 for count in core_rows.values()):
                    issues.append(QualityIssue("insufficient_daily_history", "核心美股 ETF 少于260个有效交易日，MA200可能不可用"))
        except (json.JSONDecodeError, OSError) as exc:
            issues.append(QualityIssue("evidence_invalid", f"证据文件无法解析：{exc}"))
    return QualityResult(not any(item.severity == "error" for item in issues), issues, checks)
