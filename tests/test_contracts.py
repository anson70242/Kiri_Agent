"""Contracts a reviewer would otherwise check by eye, read off the source itself:

- artifact suffixes are spelled only inside the Archive module;
- identifiers stay clear of the multi-word terms the glossary avoids.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ARCHIVE_MODULE = REPO / "backend" / "archive.py"

# The tests' own list, so a suffix added to the Archive module is added here on purpose.
ARTIFACT_SUFFIXES = (".mp4", ".live_chat.json", ".chat.json", ".comments.json", ".chat_parsed.json",
                     ".transcript.json", ".zh.transcript.json", ".highlights.json")


def python_files(*folders: str) -> list[Path]:
    return sorted(f for folder in folders for f in (REPO / folder).rglob("*.py"))


def string_literals(tree: ast.AST) -> Iterator[ast.Constant]:
    """Every string in the code except docstrings (those describe files to a person)."""
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            yield node


def suffix_spellings(file: Path) -> list[str]:
    """`file:line: text` for each string that ends in an artifact suffix (a name being built)."""
    tree = ast.parse(file.read_text(encoding="utf-8"))
    return [f"{file.relative_to(REPO).as_posix()}:{s.lineno}: {s.value[-40:]!r}"
            for s in string_literals(tree) if s.value.endswith(ARTIFACT_SUFFIXES)]


def test_artifact_suffixes_are_spelled_only_in_the_archive_module():
    assert suffix_spellings(ARCHIVE_MODULE), "the check no longer sees the suffixes where they are defined"
    outside = [hit for f in python_files("backend", "scripts") if f != ARCHIVE_MODULE for hit in suffix_spellings(f)]
    assert outside == [], "ask backend.archive (artifact_name, Archive.file, Artifact.matches) instead"


def avoided_terms() -> list[str]:
    """The multi-word terms on the glossary's `_Avoid_` lines, as they would appear in a
    snake_case identifier. Single words (download, output, config...) have other honest
    uses and stay a reviewer's call."""
    terms = []
    for line in (REPO / "GLOSSARY.md").read_text(encoding="utf-8").splitlines():
        if line.startswith("_Avoid_:"):
            terms += [t.strip().lower() for t in line.split(":", 1)[1].split(",")]
    return [t.replace(" ", "_") for t in terms if " " in t]


def identifiers(tree: ast.AST) -> Iterator[tuple[int, str]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            yield node.lineno, node.id
        elif isinstance(node, ast.arg):
            yield node.lineno, node.arg
        elif isinstance(node, ast.Attribute):
            yield node.lineno, node.attr
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node.lineno, node.name
        elif isinstance(node, ast.keyword) and node.arg:
            yield node.lineno, node.arg


def avoided_identifiers(source: str, terms: list[str]) -> list[tuple[int, str]]:
    avoided = re.compile("|".join(map(re.escape, terms)))
    snake = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")  # ZhTranscript -> Zh_Transcript
    return [(line, name) for line, name in identifiers(ast.parse(source)) if avoided.search(snake.sub("_", name).lower())]


def test_identifiers_use_no_multi_word_term_the_glossary_avoids():
    terms = avoided_terms()
    assert "zh_transcript" in terms and "vod_folder" in terms  # the glossary is still being read
    assert avoided_identifiers("def read(zh_transcript): ZhTranscript = VOD_FOLDER", terms) == [
        (1, "zh_transcript"), (1, "ZhTranscript"), (1, "VOD_FOLDER")]

    found = [f"{f.relative_to(REPO).as_posix()}:{line}: {name}"
             for f in python_files("backend", "scripts", "tests") if f != Path(__file__).resolve()
             for line, name in avoided_identifiers(f.read_text(encoding="utf-8"), terms)]
    assert found == [], "use the GLOSSARY.md term"
