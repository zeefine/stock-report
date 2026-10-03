from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

Market = Literal["us", "cn", "hk"]
ReportMode = Literal["intraday", "close"]


@dataclass(frozen=True)
class RunContext:
    root: Path
    market: Market
    report_date: str | None
    mode: ReportMode
    as_of: datetime
    refresh: bool
    timezone: str
    runs_dir: Path
    reports_dir: Path
    template: Path
    prompt: Path
    config: dict[str, Any] = field(repr=False)
    as_of_explicit: bool = False

    def market_run_dir(self, report_date: str) -> Path:
        base = self.runs_dir / self.market / report_date
        if self.market == "cn":
            return base / ("intraday_latest" if self.mode == "intraday" else "close")
        return base


@dataclass(frozen=True)
class QualityIssue:
    code: str
    message: str
    severity: Literal["warning", "error"] = "error"


@dataclass
class QualityResult:
    passed: bool
    issues: list[QualityIssue] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "issues": [asdict(item) for item in self.issues],
            "checks": self.checks,
        }


@dataclass(frozen=True)
class PipelineResult:
    market: Market
    report_date: str
    mode: ReportMode
    report_path: Path
    evidence_path: Path | None
    manifest_path: Path
    quality: QualityResult
