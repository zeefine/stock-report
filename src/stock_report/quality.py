from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .models import QualityIssue, QualityResult

PLACEHOLDER_RE = re.compile(r"\{\{[A-Z0-9_]+\}\}")


def unresolved_placeholders(text: str) -> list[str]:
    return sorted(set(PLACEHOLDER_RE.findall(text)))


def validate_report(report_path: Path, evidence_path: Path | None = None) -> QualityResult:
    issues: list[QualityIssue] = []
    checks: dict[str, Any] = {}
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
        issues.append(QualityIssue("evidence_missing", "未找到 evidence.json", "warning"))
        checks["evidence_exists"] = False
    else:
        checks["evidence_exists"] = True
        try:
            payload = json.loads(evidence_path.read_text(encoding="utf-8"))
            evidence_date = payload.get("report_date") if isinstance(payload, dict) else None
            checks["evidence_report_date"] = evidence_date
            filename_date = re.search(r"\d{4}-\d{2}-\d{2}", report_path.name)
            if evidence_date and filename_date and evidence_date != filename_date.group(0):
                issues.append(QualityIssue("date_mismatch", f"报告文件日期 {filename_date.group(0)} 与证据日期 {evidence_date} 不一致"))
            intraday = payload.get("intraday_quality") if isinstance(payload, dict) else None
            if isinstance(intraday, dict):
                checks["intraday_ready"] = intraday.get("ready")
                checks["intraday_core_index_coverage"] = intraday.get("core_index_coverage")
                checks["intraday_relevant_events"] = intraday.get("a_share_relevant_event_count")
                if not intraday.get("ready"):
                    issues.append(QualityIssue("intraday_not_ready", "分钟线或带时间戳事件未通过盘中章节质量门", "warning"))
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
