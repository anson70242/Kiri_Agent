"""A row of a source record sheet: its kind, its values, and the text each cell gets.

    配信者 | 日期 | 项目类型 | 闪传链接
    柊優花 | 2026/11/5 | 录播 | 戦場がマイホームの声優です（有效至 2026/12/23）⏎ 全部下载 (the link)

The link cell never shows the address: the address is behind its label.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum

from backend.tencent_docs import CellValue, Segment

HONG_KONG = timezone(timedelta(hours=8))  # whatever the server's clock is set to, as the archive is dated

# The sheet's headers, found by their text in its first row.
STREAMER = "配信者"
DATE = "日期"
KIND = "项目类型"
LINK = "闪传链接"
HEADERS = (STREAMER, DATE, KIND, LINK)


class RowKind(StrEnum):
    """What a row holds, as written under 项目类型."""

    ARCHIVE = "录播"  # the archive's short download link
    DRIVE_COPY = "网盘"  # the drive copy's drive link


LABELS = {RowKind.ARCHIVE: "全部下载", RowKind.DRIVE_COPY: "网盘链接"}  # the text the link sits behind


@dataclass(frozen=True)
class SheetRow:
    """What one row says about one live."""

    kind: RowKind
    streamer: str  # the streamer's roster name
    date: str  # the archive's date, `YYYYMMDD` (its date folder)
    title: str  # the live's title
    link: str  # the short download link or the drive link: never logged
    expires: float | None = None  # when a short download link stops working (epoch seconds)
    stream_id: str = ""  # with the streamer, tells lives apart (the record of rows written)

    def cells(self) -> dict[str, CellValue]:
        """Each header's cell."""
        first_line = self.title
        if self.expires is not None:
            first_line += f"（有效至 {slashed(datetime.fromtimestamp(self.expires, HONG_KONG).strftime('%Y%m%d'))}）"
        return {STREAMER: self.streamer, DATE: slashed(self.date), KIND: str(self.kind),
                LINK: [Segment(first_line + "\n"), Segment(LABELS[self.kind], self.link)]}


def slashed(date: str) -> str:
    """`20261105` -> `2026/11/5`, as the sheet's own dates; anything else as it is."""
    if len(date) == 8 and date.isdigit():
        return f"{int(date[:4])}/{int(date[4:6])}/{int(date[6:])}"
    return date
