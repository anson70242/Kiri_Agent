"""The 路灯表 as an Excel sheet: what clippers download, and what gets uploaded to
Tencent Docs / Google Sheets (both keep this formatting on import).

    highlights_xlsx(table) -> bytes    # table: the <stem>.highlights.json dict
"""

from __future__ import annotations

import io
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

HEADER = ["开始", "结束", "话题分类", "内容摘要（话题日志）", "切片标题 / 高能点", "分数", "弹幕", "剪辑"]
WIDTHS = [10, 10, 14, 100, 60, 7, 7, 14]  # characters (a CJK character is ~2)

# Same colours as the 路灯表 page (frontend/highlights.html); also used by backend.tencent_docs.
HOT_COLOR = "FFFBEB"  # background of a row with a 🏆 pick
HEAD_COLOR, HEAD_FONT_COLOR = "E8EFFB", "5A6B8C"
HIGH_CHAT_COLOR = "E11D48"  # 弹幕 高
HOT = PatternFill("solid", fgColor=HOT_COLOR)
HEAD = PatternFill("solid", fgColor=HEAD_COLOR)
# Everything vertically centred; every column but 剪辑 (left free for clippers) also
# horizontally centred, except the long text of 内容摘要 / 切片标题 (left-aligned below the header). Data cells wrap so multi-line cells keep one entry per line.
HEAD_ALIGN = Alignment(horizontal="center", vertical="center")
EDIT_HEAD_ALIGN = Alignment(vertical="center")
CELL = Alignment(horizontal="center", vertical="center", wrap_text=True)
EDIT_CELL = Alignment(vertical="center", wrap_text=True)
TEXT_CELL = Alignment(horizontal="left", vertical="center", wrap_text=True)  # long text: 内容摘要, 切片标题


def score_style(score: Any) -> tuple[str, bool] | None:
    """(font colour, bold) of a score cell: red from 7, orange from 5, grey below."""
    if not isinstance(score, (int, float)):
        return None
    return ("E11D48", True) if score >= 7 else ("C2410C", True) if score >= 5 else ("5A6B8C", False)


def score_font(score: Any) -> Font:
    style = score_style(score)
    return Font(color=style[0], bold=style[1]) if style else Font()


def cells(r: dict[str, Any]) -> list[Any]:
    log = [f"{e.get('start', '')}–{e.get('end', '')} {e.get('title', '')} {e.get('detail', '')}".strip()
           for e in r.get("timeline") or []]
    picks = []
    if tp := r.get("top_pick"):
        span = f"{tp['start']}–{tp['end']} " if tp.get("start") and tp.get("end") else ""
        picks.append(f"🏆 {span}{tp.get('title', '')}")
    picks += [f"{h.get('time', '')} [{h.get('type', '')}] {h.get('title', '')}" for h in r.get("highlights") or []]
    return [r.get("start", ""), r.get("end", ""), "\n".join(r.get("topics") or []),
            "\n\n".join(log),  # a blank line between topic-log entries
            "\n".join(picks), r.get("score"), r.get("chat_level", ""), ""]


def highlights_xlsx(table: dict[str, Any]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "路灯表"
    ws.append(HEADER)
    for c in ws[1]:
        c.font, c.fill, c.alignment = Font(bold=True, color=HEAD_FONT_COLOR), HEAD, HEAD_ALIGN
    ws[1][-1].alignment = EDIT_HEAD_ALIGN
    for i, w in enumerate(WIDTHS):
        ws.column_dimensions[chr(ord("A") + i)].width = w
    ws.freeze_panes = "A2"

    for r in table.get("rows") or []:
        ws.append(cells(r))
        row = ws[ws.max_row]
        for c in row:
            c.alignment = CELL
            if r.get("top_pick"):
                c.fill = HOT
        row[3].alignment = row[4].alignment = TEXT_CELL
        row[-1].alignment = EDIT_CELL
        row[5].font = score_font(r.get("score"))
        if r.get("chat_level") == "高":
            row[6].font = Font(bold=True, color=HIGH_CHAT_COLOR)

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
