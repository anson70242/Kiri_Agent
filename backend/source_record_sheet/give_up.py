"""The owner's message when a row is given up on (the operator alert, Discord): which group's
sheet, which live, which kind of row, and why, so that the row can be added by hand. In
Chinese, like the other operator messages. It carries no link: not the row's, and not an
address that Tencent Docs' reason may hold (`without_addresses`, which the log uses too). No
QQ group is ever told about a row.
"""

from __future__ import annotations

import re

from .jobs import RowJob
from .row import slashed

REASON_KEPT = 500  # characters of the reason that are told
ADDRESS = re.compile(r"https?://\S+")
SHARE_HINT = "腾讯文档 token 的账号没有这张表的编辑权限：请把这张表共享给这个账号、给编辑权限"


def given_up_message(job: RowJob, reason: str, *, denied: bool = False) -> str:
    """`reason`: Tencent Docs' (or the missing header's); `denied`: the token's account may
    not edit the sheet, and the message says to share it with that account."""
    row = job.row
    sheet = f"QQ 群「{job.group}」的录播源记录表" if job.group else "录播源记录表"
    lines = [f"{sheet}没写进这一行，已放弃，请手动补上",
             f"配信者：{row.streamer}",
             f"日期：{slashed(row.date)}",
             f"直播：{row.title}",
             f"项目类型：{row.kind}",
             f"原因：{without_addresses(reason)[:REASON_KEPT]}"]
    if denied:
        lines.append(SHARE_HINT)
    return "\n".join(lines)


def without_addresses(reason: str) -> str:
    """Tencent Docs' reason with every address in it left out: an error may echo the link it
    was given (a run's link_url), and a link never reaches the log or the owner's message."""
    return ADDRESS.sub("（地址略）", reason)
