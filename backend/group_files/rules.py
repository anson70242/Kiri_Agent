"""The filing rules: which group files are kept on the drive, and in which subfolder of
their group folder. One table, matched by `filing_subfolder` and written out for the help
by `rule_lines`, so a sixth rule is one line here."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FilingRule:
    tag: str  # in the file's name, anywhere, full-width brackets exactly as written
    extension: str  # the end of the file's name, in any case
    subfolder: str  # in the group folder


# In the owner's order: the first rule that matches a name wins.
FILING_RULES: tuple[FilingRule, ...] = (
    FilingRule("【待轴】", ".mp4", "剪辑"),
    FilingRule("【空轴】", ".ass", "空轴"),
    FilingRule("【待校】", ".ass", "待校"),
    FilingRule("【已校】", ".ass", "已校"),
    FilingRule("【熟肉】", ".mp4", "熟肉"),
)


def filing_subfolder(name: str) -> str | None:
    """The subfolder a group file of this name goes to, or None when no rule keeps it."""
    folded = name.casefold()
    for rule in FILING_RULES:
        if rule.tag.casefold() in folded and folded.endswith(rule.extension.casefold()):
            return rule.subfolder
    return None


def rule_lines(folder: str) -> list[str]:
    """One help line per rule, for the group whose group folder is `folder`."""
    return [f"- 文件名含{r.tag}的 {r.extension} 文件 → 网盘 {folder}/{r.subfolder}/" for r in FILING_RULES]
