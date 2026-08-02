from __future__ import annotations

from stock_report.models import RunContext


def run(context: RunContext):
    raise NotImplementedError(
        "港股已预留配置、模板和模块边界，但采集器尚未迁移；请先使用 cn 或 us。"
    )
