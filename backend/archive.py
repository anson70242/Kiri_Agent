"""Archive (录播): one stream's folder on disk and the artifacts in it.

This module owns the on-disk layout; other code asks it instead of spelling an artifact
suffix or counting folder levels:

    <downloads root>/<creator>/<YYYYMMDD>/<title>/<stem><suffix>

where the stem is `[YYYYMMDD][creator] title` made safe for file names.

Lookup rule: an artifact is the file named video stem + its suffix; when that file is absent,
the first file in sorted name order with that suffix. The video of an archive opened from a
folder is its largest `.mp4`.

An `Archive` is a snapshot of its folder taken when it is opened; open it again to see
files written since. `Downloads` is the downloads root: listing every archive, and opening
one by the folder reference a client sends.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class Artifact(Enum):
    """One kind of file of an archive, by its file name suffix(es)."""

    VIDEO = (".mp4",)
    RAW_CHAT = (".live_chat.json", ".chat.json")  # YouTube, Twitch: as downloaded
    CHAT = (".chat_parsed.json",)  # cleaned chat
    TRANSCRIPT = (".transcript.json",)  # Japanese subtitles (backend.asr)
    ZH_TRANSCRIPT = (".zh.transcript.json",)  # Chinese subtitles (backend.translate)
    HIGHLIGHTS = (".highlights.json",)  # 路灯表 (backend.analysis)

    @property
    def suffixes(self) -> tuple[str, ...]:
        return self.value

    def matches(self, name: str) -> bool:
        """Whether a file name carries this artifact's suffix. The transcript's suffix is
        also the end of the Chinese transcript's; those never count as the transcript."""
        if self is Artifact.TRANSCRIPT and Artifact.ZH_TRANSCRIPT.matches(name):
            return False
        return name.endswith(self.suffixes)

    def stem_of(self, name: str) -> str:
        """`name` without this artifact's suffix (the stem it was named after)."""
        for suffix in self.suffixes:
            if name.endswith(suffix):
                return name.removesuffix(suffix)
        return name

    @staticmethod
    def transcript(lang: str) -> Artifact:
        """The transcript in `lang`: "zh" is the Chinese transcript, anything else Japanese."""
        return Artifact.ZH_TRANSCRIPT if lang == "zh" else Artifact.TRANSCRIPT


DOWNLOADING_SUFFIX = ".part"  # a download in progress (the video, or Twitch raw chat being fetched)
SAVING_SUFFIX = ".tmp"  # an artifact being saved by a stage: never a download in progress


@dataclass(frozen=True)
class _Entry:
    name: str
    size: int
    mtime: float
    mtime_ns: int


@dataclass(frozen=True)
class _Snapshot:
    entries: tuple[_Entry, ...]  # the plain files directly in the folder, sorted by name
    folder_mtime: float


def _scan(folder: Path) -> _Snapshot:
    """Raises OSError when the folder is gone (e.g. deleted while being listed)."""
    folder_mtime = folder.stat().st_mtime
    entries = []
    with os.scandir(folder) as it:
        for e in it:
            if e.is_file():
                st = e.stat()
                entries.append(_Entry(e.name, st.st_size, st.st_mtime, st.st_mtime_ns))
    return _Snapshot(tuple(sorted(entries, key=lambda e: e.name)), folder_mtime)


def _largest_video(entries: tuple[_Entry, ...]) -> _Entry | None:
    """The finished video: the largest `.mp4` (yt-dlp may leave format fragments around)."""
    mp4s = [e for e in entries if Artifact.VIDEO.matches(e.name)]
    return max(mp4s, key=lambda e: e.size) if mp4s else None


DATE_RE = re.compile(r"\d{8}")  # YYYYMMDD
DATED_NAME_RE = re.compile(r"\[(\d{8})\]")  # a file name's `[YYYYMMDD]` prefix


def _dated(date_folder: str, names: list[str]) -> str:
    """`date_folder` when it is a `YYYYMMDD`, else the `[YYYYMMDD]` prefix of the first of
    `names` that has one; "" when none says."""
    if DATE_RE.fullmatch(date_folder):
        return date_folder
    for name in names:
        if m := DATED_NAME_RE.match(name):
            return m.group(1)
    return ""


def file_date(path: Path) -> str:
    """The `YYYYMMDD` of one artifact file: its archive's date folder, else the `[YYYYMMDD]`
    prefix of the file's own name (never another file's); "" when neither says."""
    return _dated(path.parent.parent.name, [path.name])


class Archive:
    """One archive folder. Open it with `from_folder` or `from_video`, or through `Downloads`."""

    def __init__(self, folder: Path, stem: str | None, snapshot: _Snapshot, root: Path | None) -> None:
        self.folder = folder
        self.stem = stem  # what the artifacts are named after (the video's name less its extension); None without a video
        self.root = root  # the downloads root, when known
        self._entries = snapshot.entries
        self._folder_mtime = snapshot.folder_mtime

    @classmethod
    def from_folder(cls, folder: Path, root: Path | None = None) -> Archive:
        """The archive in `folder`; its video is the largest `.mp4` in it. Raises OSError
        when the folder cannot be read."""
        snapshot = _scan(folder)
        video = _largest_video(snapshot.entries)
        return cls(folder, Artifact.VIDEO.stem_of(video.name) if video else None, snapshot, root)

    @classmethod
    def from_video(cls, video: Path, root: Path | None = None) -> Archive:
        """The archive `video` belongs to (neither need exist yet); artifacts are named after
        it without its extension, whatever the format (`x.mkv` -> `x.transcript.json`)."""
        try:
            snapshot = _scan(video.parent)
        except FileNotFoundError:
            snapshot = _Snapshot((), 0.0)
        return cls(video.parent, video.stem, snapshot, root)

    # ---- where it is ----

    @property
    def ref(self) -> str:
        """The folder reference (`creator/date/title`, relative to the downloads root): the
        `folder` value in dashboard URLs."""
        if self.root is None:
            raise ValueError(f"no downloads root known for {self.folder}")
        return self.folder.relative_to(self.root).as_posix()

    @property
    def creator(self) -> str:
        """The streamer the archive is filed under: the top folder below the downloads root."""
        if self.root is not None:
            return self.folder.relative_to(self.root).parts[0]
        return self.folder.parent.parent.name

    @property
    def date_folder(self) -> str:
        """The name of the date folder, as on disk (`YYYYMMDD` for every archive the downloader made)."""
        return self.folder.parent.name

    @property
    def title(self) -> str:
        return self.folder.name

    @property
    def date(self) -> str:
        """The archive's `YYYYMMDD`: from its date folder, else from the `[YYYYMMDD]` prefix of
        its video's name or of any file's name (first in sorted order); "" when none says."""
        names = ([self.stem] if self.stem is not None else []) + [e.name for e in self._entries]
        return _dated(self.date_folder, names)

    @property
    def size(self) -> int:
        """Total bytes of the files in the folder."""
        return sum(e.size for e in self._entries)

    @property
    def mtime(self) -> float:
        """When a file in it last changed (the folder's own time when it is empty)."""
        return max((e.mtime for e in self._entries), default=self._folder_mtime)

    def member(self, name: str) -> Path | None:
        """The plain file `name` directly inside the archive, or None when there is none.
        Raises ValueError for a name that is not a bare file name inside the folder."""
        base = self.folder.resolve()
        path = (base / name).resolve()
        if not name or Path(name).name != name or path.parent != base:
            raise ValueError(f"bad file name: {name!r}")
        return path if path.is_file() else None

    # ---- what it has ----

    def files(self, artifact: Artifact) -> list[Path]:
        """Every file in the archive with this artifact's suffix, in sorted name order."""
        return [self.folder / e.name for e in self._entries if artifact.matches(e.name)]

    def file(self, artifact: Artifact) -> Path | None:
        """The artifact's file (see the lookup rule), or None when the archive lacks it."""
        names = {e.name for e in self._entries}
        if self.stem is not None:
            for suffix in artifact.suffixes:
                if self.stem + suffix in names:
                    return self.folder / (self.stem + suffix)
        if artifact is Artifact.VIDEO:
            video = _largest_video(self._entries)
            return self.folder / video.name if video else None
        found = self.files(artifact)
        return found[0] if found else None

    def _entry(self, artifact: Artifact) -> _Entry | None:
        file = self.file(artifact)
        return next((e for e in self._entries if e.name == file.name), None) if file else None

    @property
    def video(self) -> Path | None:
        return self.file(Artifact.VIDEO)

    @property
    def video_size(self) -> int:
        """Bytes of the video; 0 without one."""
        entry = self._entry(Artifact.VIDEO)
        return entry.size if entry else 0

    def has(self, artifact: Artifact) -> bool:
        return self.file(artifact) is not None

    def present(self) -> set[Artifact]:
        """The artifacts this archive has."""
        return {a for a in Artifact if self.has(a)}

    @property
    def downloading(self) -> bool:
        """No video yet, or a download in progress (a `.part` file) in the folder."""
        return self.video is None or any(Path(e.name).suffix == DOWNLOADING_SUFFIX for e in self._entries)

    # ---- reading ----

    def read(self, artifact: Artifact) -> Any:
        """The artifact's JSON contents, parsed. Raises ArtifactMissing when the archive
        lacks it, ArtifactDamaged when it is not readable JSON."""
        path = self.file(artifact)
        if path is None:
            raise ArtifactMissing(artifact, self.folder)
        return read_file(path, artifact)

    def read_chat(self) -> list[dict]:
        """The cleaned chat's messages (none when the file holds something other than a list)."""
        data = self.read(Artifact.CHAT)
        return data if isinstance(data, list) else []

    def read_transcript(self, lang: str = "ja") -> Any:
        """The transcript's segments: Japanese, or with `lang` "zh" the Chinese transcript."""
        return self.read(Artifact.transcript(lang))

    def read_highlights(self) -> Any:
        """The highlight table (路灯表) as stored: a dict with "rows" when well formed."""
        return self.read(Artifact.HIGHLIGHTS)

    def chat_count(self) -> int:
        """How many chat messages the archive has; 0 when it has no readable chat. Cached
        until the chat file changes."""
        entry = self._entry(Artifact.CHAT)
        if entry is None:
            return 0
        path = self.folder / entry.name
        cached = _chat_counts.get(path)
        if cached and cached[0] == entry.mtime_ns:
            return cached[1]
        try:
            n = len(self.read_chat())
        except (ArtifactMissing, ArtifactDamaged, OSError):
            n = 0
        _chat_counts[path] = (entry.mtime_ns, n)
        return n

    # ---- saving ----

    @classmethod
    def from_file(cls, path: Path, root: Path | None = None) -> Archive:
        """The archive an artifact file (e.g. a raw chat) belongs to, its artifacts named after
        that file's stem. Raises ValueError when the name carries no artifact suffix."""
        artifact = next((a for a in Artifact if a.matches(path.name)), None)
        if artifact is None:
            raise ValueError(f"not an artifact file: {path.name}")
        try:
            snapshot = _scan(path.parent)
        except FileNotFoundError:
            snapshot = _Snapshot((), 0.0)
        return cls(path.parent, artifact.stem_of(path.name), snapshot, root)

    def save(self, artifact: Artifact, content: Any) -> Path:
        """Write the artifact's content atomically (through `<name>.tmp`, then a rename) to the
        file named after the archive's stem (see `artifact_name`) and return that file. The
        archive sees the new file afterwards."""
        if artifact in (Artifact.VIDEO, Artifact.RAW_CHAT):
            raise ValueError(f"the {artifact.name.lower()} is downloaded, not saved through the archive")
        if self.stem is None:
            raise ValueError(f"no video stem to name a {artifact.name.lower()} after in {self.folder}")
        out = self.folder / artifact_name(self.stem, artifact)
        tmp = out.with_name(out.name + SAVING_SUFFIX)
        tmp.write_text(self._serialize(artifact, content), encoding="utf-8")
        tmp.replace(out)
        snapshot = _scan(self.folder)
        self._entries, self._folder_mtime = snapshot.entries, snapshot.folder_mtime
        return out

    @staticmethod
    def _serialize(artifact: Artifact, content: Any) -> str:
        """An artifact's bytes on disk: the highlight table indented by one space; the lists
        (chat, transcripts) one compact item per line."""
        if artifact is Artifact.HIGHLIGHTS:
            return json.dumps(content, ensure_ascii=False, indent=1)
        body = ",\n".join("  " + json.dumps(x, ensure_ascii=False) for x in content)
        return f"[\n{body}\n]\n" if content else "[]\n"


def read_file(path: Path, artifact: Artifact) -> Any:
    """The JSON contents of `path`, read as `artifact` (a file a person points at, which
    need not sit in an archive). Raises ArtifactMissing when it is gone, ArtifactDamaged
    when it is not readable JSON."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ArtifactMissing(artifact, path) from None
    except ValueError as e:  # bad JSON, or not UTF-8
        raise ArtifactDamaged(artifact, path, str(e)) from None


# chat file -> (mtime_ns, message count): listing every archive must not re-read every chat
_chat_counts: dict[Path, tuple[int, int]] = {}


class Downloads:
    """The downloads root: every archive under it, and client-supplied folder references."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def list(self) -> list[Archive]:
        """Every `<creator>/<date>/<title>/` archive, newest first by date, then by when a file
        in it last changed. Folders that vanish while being listed are skipped."""
        if not self.root.is_dir():
            return []
        archives = []
        for folder in self.root.glob("*/*/*"):
            if folder.is_dir():
                try:
                    archives.append(Archive.from_folder(folder, self.root))
                except OSError:
                    continue  # vanished mid-scan (e.g. yt-dlp renaming, or pruned)
        archives.sort(key=lambda a: (a.date_folder, a.mtime), reverse=True)
        return archives

    def open(self, ref: str) -> Archive:
        """The archive a client-supplied folder reference names. Raises ArchiveNotFound for
        anything outside the root, the root itself, or what is not a folder."""
        path = (self.root / ref).resolve()
        if path == self.root or not path.is_relative_to(self.root) or not path.is_dir():
            raise ArchiveNotFound(ref)
        try:
            return Archive.from_folder(path, self.root)
        except FileNotFoundError:
            raise ArchiveNotFound(ref) from None

    def ref(self, folder: Path) -> str:
        """The folder reference of an archive folder under the root: the `folder` value in
        dashboard URLs, which `open` turns back into the archive. Raises ValueError for a
        folder outside the root."""
        return folder.resolve().relative_to(self.root).as_posix()

    def prune(self, protect: Path, keep: int) -> list[Path]:
        """Delete the oldest archives of `protect`'s creator beyond `keep`, oldest first by date,
        then by the archive folder's modification time; never `protect` itself (the one just
        downloaded). A date folder left empty goes too. Does nothing unless the creator folder
        is directly under the downloads root. Returns the archive folders deleted."""
        creator = protect.parent.parent
        if creator.resolve().parent != self.root:  # never delete outside the downloads root
            log.error("not pruning %s: not directly under %s", creator, self.root)
            return []
        folders = [t for d in creator.iterdir() if d.is_dir() for t in d.iterdir() if t.is_dir()]
        folders.sort(key=lambda p: (p.parent.name, p.stat().st_mtime))
        excess = len(folders) - keep
        removed: list[Path] = []
        for folder in folders:
            if excess <= 0:
                break
            if folder == protect:
                continue
            shutil.rmtree(folder)
            removed.append(folder)
            excess -= 1
            log.info("pruned old archive: %s", folder)
            if not any(folder.parent.iterdir()):
                folder.parent.rmdir()
        return removed


# ---- naming a new archive ----

def safe_name(name: str, max_len: int = 150) -> str:
    """Make `name` safe as a Windows / Linux file or folder name."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = " ".join(name.split()).strip(" .")
    return name[:max_len].rstrip(" .") or "_"


def name_archive(creator: str, date: str, title: str) -> tuple[Path, str]:
    """Where a stream about to be downloaded goes: its folder relative to the downloads root
    (`<creator>/<YYYYMMDD>/<title>`) and the stem its files are named after
    (`[YYYYMMDD][creator] title`), both made safe for file names."""
    return Path(safe_name(creator), date, safe_name(title)), safe_name(f"[{date}][{creator}] {title}")


RAW_CHAT_SUFFIX = dict(zip(("youtube", "twitch"), Artifact.RAW_CHAT.suffixes))  # platform -> suffix


def artifact_name(stem: str, artifact: Artifact, platform: str = "") -> str:
    """The file name of `artifact` in an archive whose files are named after `stem`. Raw chat
    is named by the `platform` it came from."""
    if artifact is Artifact.RAW_CHAT:
        return stem + RAW_CHAT_SUFFIX[platform]
    return stem + artifact.suffixes[0]


class ArchiveNotFound(LookupError):
    """No archive under the downloads root by that folder reference."""


class ArtifactMissing(LookupError):
    """The archive has no such artifact."""

    def __init__(self, artifact: Artifact, path: Path) -> None:
        super().__init__(f"no {artifact.name.lower()} in {path}")
        self.artifact, self.path = artifact, path


class ArtifactDamaged(ValueError):
    """The artifact's file exists but cannot be parsed."""

    def __init__(self, artifact: Artifact, path: Path, reason: str) -> None:
        super().__init__(f"{path.name}: {reason}")
        self.artifact, self.path = artifact, path
