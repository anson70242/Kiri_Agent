"""The filing rules: which group files are kept and in which subfolder, one table matched
by a pure function."""

from __future__ import annotations

import pytest

from backend.group_files.rules import FILING_RULES, filing_subfolder, rule_lines


@pytest.mark.parametrize("name, subfolder", [
    ("【待轴】第3话.mp4", "剪辑"),
    ("【空轴】第3话.ass", "空轴"),
    ("【待校】第3话.ass", "待校"),
    ("【已校】第3话.ass", "已校"),
    ("【熟肉】第3话.mp4", "熟肉"),
])
def test_each_rule_names_its_subfolder(name, subfolder):
    assert filing_subfolder(name) == subfolder


def test_the_tag_counts_wherever_it_stands_and_the_extension_in_any_case():
    assert filing_subfolder("第3话【待校】v2.ass") == "待校"
    assert filing_subfolder("【待校】第3话.ASS") == "待校"
    assert filing_subfolder("【熟肉】第3话.Mp4") == "熟肉"


def test_the_first_matching_rule_wins():
    assert filing_subfolder("【已校】【待校】第3话.ass") == "待校"  # 待校 comes before 已校 in the table
    assert filing_subfolder("【熟肉】【待轴】第3话.mp4") == "剪辑"


@pytest.mark.parametrize("name", ["【待校】第3话.mp4", "【待轴】第3话.ass", "第3话.ass", "第3话.mp4",
                                  "[待校]第3话.ass", "【待校】第3话.ass.txt", "【待校】第3话", ""])
def test_a_tag_with_the_wrong_extension_or_a_name_without_a_tag_matches_nothing(name):
    assert filing_subfolder(name) is None


def test_the_table_is_the_owners_five_rules_in_order():
    assert [(r.tag, r.extension, r.subfolder) for r in FILING_RULES] == [
        ("【待轴】", ".mp4", "剪辑"), ("【空轴】", ".ass", "空轴"), ("【待校】", ".ass", "待校"),
        ("【已校】", ".ass", "已校"), ("【熟肉】", ".mp4", "熟肉")]


def test_the_help_has_one_line_per_rule_with_the_tag_the_extension_and_the_folder():
    lines = rule_lines("章鱼烧字幕组")
    assert len(lines) == 5
    assert "【待校】" in lines[2] and ".ass" in lines[2] and "章鱼烧字幕组/待校/" in lines[2]
    assert "【待轴】" in lines[0] and ".mp4" in lines[0] and "章鱼烧字幕组/剪辑/" in lines[0]
