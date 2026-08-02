from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

Market = Literal["us", "cn", "hk"]


@dataclass(frozen=True)
class RunContext:
    root: Path
    market: Market
    report_date: str | None
    timezone: str
    runs_dir: Path
    reports_dir: Path
    template: Path
    prompt: Path
    config: dict[str, Any] = field(repr=False)

    def market_run_dir(self, report_date: str) -> Path:
        return self.runs_dir / self.market / report_date


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
    report_path: Path
    evidence_path: Path | None
    manifest_path: Path
    quality: QualityResult
