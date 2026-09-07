#!/usr/bin/env python3
"""Render a complete iOS A-side audit report as a print-friendly PDF."""

from __future__ import annotations

import html
import os
from pathlib import Path
from typing import Any, Mapping


FONT_NAME = "AuditCJK"
STATUS_LABELS = {
    "PASS": "通过 PASS",
    "FAIL": "未通过 FAIL",
    "NOT_VERIFIABLE": "静态无法确认 NOT_VERIFIABLE",
    "WARN": "警告 WARN",
}
STATUS_SHORT_LABELS = {
    "PASS": "通过",
    "FAIL": "未通过",
    "NOT_VERIFIABLE": "需复核",
    "WARN": "警告",
}
STATUS_COLORS = {
    "PASS": "#16794D",
    "FAIL": "#C63D3D",
    "NOT_VERIFIABLE": "#53657D",
    "WARN": "#B8790A",
}
FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
)


def _reportlab_modules() -> dict[str, Any]:
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER, TA_LEFT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import (
            LongTable,
            Paragraph,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )
    except ImportError as error:
        raise RuntimeError(
            "缺少 ReportLab。请使用 Codex bundled Python，或为当前解释器安装 reportlab。"
        ) from error
    return locals()


def _font_candidates() -> list[Path]:
    configured = os.environ.get("IOS_ASIDE_REVIEW_PDF_FONT")
    candidates = [Path(configured).expanduser()] if configured else []
    candidates.extend(Path(item) for item in FONT_CANDIDATES)
    return candidates


def _register_font(modules: Mapping[str, Any]) -> Path:
    pdfmetrics = modules["pdfmetrics"]
    TTFont = modules["TTFont"]
    if FONT_NAME in pdfmetrics.getRegisteredFontNames():
        for candidate in _font_candidates():
            if candidate.is_file():
                return candidate

    errors: list[str] = []
    for candidate in _font_candidates():
        if not candidate.is_file():
            continue
        try:
            kwargs = {"subfontIndex": 0} if candidate.suffix.lower() in {".ttc", ".otc"} else {}
            pdfmetrics.registerFont(TTFont(FONT_NAME, str(candidate), **kwargs))
            return candidate
        except Exception as error:  # ReportLab raises several font parser types.
            errors.append(f"{candidate}: {error}")
    detail = "；".join(errors[:3]) if errors else "未找到候选字体文件"
    raise RuntimeError(
        "没有可嵌入的中文字体。可通过 IOS_ASIDE_REVIEW_PDF_FONT 指定 TTF/TTC 字体。"
        + f" 详情：{detail}"
    )


def preflight_pdf() -> Path:
    """Validate PDF dependencies and return the selected CJK font path."""
    modules = _reportlab_modules()
    return _register_font(modules)


def _clean(value: Any) -> str:
    text = str(value if value not in (None, "") else "—")
    return text.replace("\u2011", "-").replace("\u2013", "-").replace("\u2014", "-")


def _paragraph(value: Any, style: Any, Paragraph: Any) -> Any:
    escaped = html.escape(_clean(value)).replace("\n", "<br/>")
    return Paragraph(escaped, style)


def _status_display(status: str) -> str:
    return f"{STATUS_SHORT_LABELS.get(status, status)}\n{status}"


def render_audit_pdf(report: Mapping[str, Any], output_path: Path) -> None:
    modules = _reportlab_modules()
    _register_font(modules)

    colors = modules["colors"]
    TA_CENTER = modules["TA_CENTER"]
    TA_LEFT = modules["TA_LEFT"]
    A4 = modules["A4"]
    ParagraphStyle = modules["ParagraphStyle"]
    getSampleStyleSheet = modules["getSampleStyleSheet"]
    mm = modules["mm"]
    LongTable = modules["LongTable"]
    Paragraph = modules["Paragraph"]
    SimpleDocTemplate = modules["SimpleDocTemplate"]
    Spacer = modules["Spacer"]
    Table = modules["Table"]
    TableStyle = modules["TableStyle"]

    navy = colors.HexColor("#18324A")
    ink = colors.HexColor("#17212B")
    muted = colors.HexColor("#617184")
    line = colors.HexColor("#D8E0E7")
    white = colors.white

    base = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "AuditTitle",
        parent=base["Title"],
        fontName=FONT_NAME,
        fontSize=22,
        leading=29,
        textColor=white,
        alignment=TA_LEFT,
        spaceAfter=0,
    )
    eyebrow_style = ParagraphStyle(
        "AuditEyebrow",
        parent=base["Normal"],
        fontName=FONT_NAME,
        fontSize=8,
        leading=11,
        textColor=colors.HexColor("#C9DAE8"),
        tracking=0.8,
    )
    heading_style = ParagraphStyle(
        "AuditHeading",
        parent=base["Heading2"],
        fontName=FONT_NAME,
        fontSize=14,
        leading=19,
        textColor=navy,
        spaceBefore=8,
        spaceAfter=8,
    )
    body_style = ParagraphStyle(
        "AuditBody",
        parent=base["BodyText"],
        fontName=FONT_NAME,
        fontSize=8.5,
        leading=13,
        textColor=ink,
        wordWrap="CJK",
    )
    small_style = ParagraphStyle(
        "AuditSmall",
        parent=body_style,
        fontSize=7.3,
        leading=10.5,
        textColor=muted,
    )
    table_header_style = ParagraphStyle(
        "AuditTableHeader",
        parent=body_style,
        fontSize=7.5,
        leading=10,
        textColor=white,
        alignment=TA_CENTER,
    )
    status_style = ParagraphStyle(
        "AuditStatus",
        parent=body_style,
        fontSize=6.6,
        leading=8.2,
        textColor=white,
        alignment=TA_CENTER,
    )
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_name(output_path.name + ".tmp")
    temp_path.unlink(missing_ok=True)

    document = SimpleDocTemplate(
        str(temp_path),
        pagesize=A4,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=15 * mm,
        bottomMargin=16 * mm,
        title="iOS A 面审核检查报告",
        author="ios-aside-review",
    )
    content_width = A4[0] - document.leftMargin - document.rightMargin
    story: list[Any] = []

    title_band = Table(
        [[_paragraph("IOS APP STORE REVIEW", eyebrow_style, Paragraph)],
         [_paragraph("iOS A 面审核检查报告", title_style, Paragraph)]],
        colWidths=[content_width],
    )
    title_band.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), navy),
        ("LEFTPADDING", (0, 0), (-1, -1), 16),
        ("RIGHTPADDING", (0, 0), (-1, -1), 16),
        ("TOPPADDING", (0, 0), (-1, 0), 10),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 2),
        ("TOPPADDING", (0, 1), (-1, 1), 2),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 11),
    ]))
    story.extend([title_band, Spacer(1, 5 * mm)])

    story.append(_paragraph("完整检查清单", heading_style, Paragraph))

    overview_data: list[list[Any]] = [[
        _paragraph("状态", table_header_style, Paragraph),
        _paragraph("规则", table_header_style, Paragraph),
        _paragraph("检查项", table_header_style, Paragraph),
        _paragraph("结论", table_header_style, Paragraph),
    ]]
    for finding in report.get("findings", []):
        status = str(finding.get("status", "NOT_VERIFIABLE"))
        overview_data.append([
            _paragraph(_status_display(status), status_style, Paragraph),
            _paragraph(finding.get("id"), small_style, Paragraph),
            _paragraph(finding.get("title"), body_style, Paragraph),
            _paragraph(finding.get("actual"), small_style, Paragraph),
        ])
    overview = LongTable(
        overview_data,
        colWidths=[35 * mm, 25 * mm, 38 * mm, content_width - 98 * mm],
        repeatRows=1,
    )
    overview_styles: list[tuple[Any, ...]] = [
        ("BACKGROUND", (0, 0), (-1, 0), navy),
        ("BOX", (0, 0), (-1, -1), 0.5, line),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, line),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    for row, finding in enumerate(report.get("findings", []), start=1):
        status = str(finding.get("status", "NOT_VERIFIABLE"))
        overview_styles.append(("BACKGROUND", (0, row), (0, row), colors.HexColor(STATUS_COLORS.get(status, "#53657D"))))
        if row % 2 == 0:
            overview_styles.append(("BACKGROUND", (1, row), (-1, row), colors.HexColor("#F8FAFB")))
    overview.setStyle(TableStyle(overview_styles))
    story.append(overview)

    def draw_page(canvas: Any, doc: Any) -> None:
        canvas.saveState()
        canvas.setStrokeColor(line)
        canvas.setLineWidth(0.5)
        canvas.line(doc.leftMargin, 11 * mm, A4[0] - doc.rightMargin, 11 * mm)
        canvas.setFillColor(muted)
        canvas.setFont(FONT_NAME, 7)
        canvas.drawString(doc.leftMargin, 7 * mm, "iOS A 面审核检查报告")
        canvas.drawRightString(A4[0] - doc.rightMargin, 7 * mm, f"第 {canvas.getPageNumber()} 页")
        canvas.restoreState()

    try:
        document.build(story, onFirstPage=draw_page, onLaterPages=draw_page)
        temp_path.replace(output_path)
    except Exception as error:
        temp_path.unlink(missing_ok=True)
        raise RuntimeError(f"PDF 渲染失败：{error}") from error
