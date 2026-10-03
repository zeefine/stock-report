from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .pipeline import run_report, validate_existing


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stock-report", description="生成和校验多市场收盘日报")
    parser.add_argument("--config", type=Path, help="配置文件路径，默认使用项目 config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="执行取数、计算、校验和 HTML 渲染")
    run.add_argument("--market", choices=["cn", "us", "hk"], required=True)
    run.add_argument("--date", help="报告日 YYYY-MM-DD；省略时自动选择最近完整交易日")
    run.add_argument("--mode", choices=["auto", "intraday", "close"], default="auto",
                     help="A股支持盘中/收盘；其他市场当前只支持收盘")
    run.add_argument("--as-of", help="盘中截止时间 HH:MM，默认使用当前时间")
    run.add_argument("--refresh", action="store_true", help="忽略可复用缓存并重新取数")
    validate = sub.add_parser("validate", help="校验已有报告与证据文件")
    validate.add_argument("--market", choices=["cn", "us"], required=True)
    validate.add_argument("--date", required=True, help="报告日 YYYY-MM-DD")
    validate.add_argument("--mode", choices=["intraday", "close"], default="close")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            result = run_report(
                args.market, args.date, mode=args.mode, as_of=args.as_of,
                refresh=args.refresh, config_path=args.config,
            )
            payload = {
                "market": result.market,
                "report_date": result.report_date,
                "mode": result.mode,
                "report": str(result.report_path),
                "evidence": str(result.evidence_path) if result.evidence_path else None,
                "manifest": str(result.manifest_path),
                "quality": result.quality.to_dict(),
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0 if result.quality.passed else 2
        quality = validate_existing(
            args.market, args.date, mode=args.mode, config_path=args.config
        )
        print(json.dumps(quality.to_dict(), ensure_ascii=False, indent=2))
        return 0 if quality.passed else 2
    except (RuntimeError, ValueError, NotImplementedError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
