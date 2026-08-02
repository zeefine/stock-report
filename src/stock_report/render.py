from __future__ import annotations

import html
from pathlib import Path
from typing import Any, Iterable

from .quality import unresolved_placeholders


def html_table(headers: Iterable[Any], rows: Iterable[Iterable[Any]]) -> str:
    head = "".join(f"<th>{html.escape(str(item))}</th>" for item in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render_template(template: Path, values: dict[str, Any], output: Path, strict: bool = True) -> Path:
    text = template.read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", str(value))
    missing = unresolved_placeholders(text)
    if strict and missing:
        raise RuntimeError("模板存在未渲染字段：" + ", ".join(missing))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    return output
