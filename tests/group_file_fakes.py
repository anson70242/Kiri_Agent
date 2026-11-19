"""Fakes for the group-files module: QQ's file host (an HTTP mock transport), the drive, the
clock and the groups store, and a builder that wires them into a GroupFileKeeper. Every
group-file test imports from here. The QQ API fake is test_qq's FakeQQ, re-exported.

Addresses, group ids and names are invented."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from backend.group_files import GroupFileKeeper
from backend.notify.qq import QQGroups

from .test_qq import SECRET, FakeQQ  # noqa: F401  (re-exported for the tests of messages)

HOST = "https://grpfile.ftn.qq.com"  # under qq.com, like the addresses QQ gives
GROUP = "GROUP_OPENID_1"
FOLDER = "章鱼烧字幕组"
POSTED = "2026-11-19T05:41:00+08:00"  # a message's `timestamp` as QQ gives it


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


class FakeFileHost:
    """QQ's file host: serves each file at its address, answers what `answers` says for an
    address instead (a status, or a redirect with `Location`), and notes every request."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.answers: dict[str, httpx.Response] = {}
        self.requests: list[str] = []
        self._n = 0

    def add(self, data: bytes, url: str | None = None) -> str:
        """Serve `data`; returns its address."""
        self._n += 1
        url = f"{HOST}/ftn_handler/f{self._n:04d}" if url is None else url
        self.files[url] = data
        return url

    def redirect(self, url: str, to: str) -> None:
        self.answers[url] = httpx.Response(302, headers={"Location": to})

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if url in self.answers:
            return self.answers[url]
        if url in self.files:
            return httpx.Response(200, content=self.files[url])
        return httpx.Response(404)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))


class FakeDrive:
    """The drive's calls the module makes (`folder_names`, `put_file`, in a `turn` that comes
    at once), on folders held in memory. `error`: raised by the next calls. `put` notes
    (folder, name, bytes). `uploading`: the upload that has the drive (a test sets it), as
    Quark's."""

    def __init__(self) -> None:
        self.folders: dict[str, dict[str, bytes]] = {}
        self.put: list[tuple[str, str, bytes]] = []
        self.error: Exception | None = None
        self.uploading: Any = None

    @asynccontextmanager
    async def turn(self, kind: Any):
        yield self

    def folder_names(self, folder: str) -> list[str]:
        if self.error:
            raise self.error
        return list(self.folders.setdefault(folder, {}))

    def put_file(self, local: Path, folder: str, progress: Any = None) -> str:
        if self.error:
            raise self.error
        data = local.read_bytes()
        if progress:
            progress(len(data), len(data))
        self.folders.setdefault(folder, {})[local.name] = data
        self.put.append((folder, local.name, data))
        return f"{folder}/{local.name}"


def qq_groups(tmp_path: Path, to_drive: bool = True, name: str = FOLDER, group: str = GROUP) -> QQGroups:
    """The groups store with one named group, its files to the drive or not."""
    groups = QQGroups(tmp_path / "qq_groups.json")
    groups.register(group)
    groups.update(group, name=name, group_files_to_drive=to_drive)
    return groups


def attachment(name: str, url: str, size: int) -> dict[str, Any]:
    """A `file` attachment as QQ sends it in a group message."""
    return {"content_type": "file", "filename": name, "size": size, "url": url}


@dataclass
class Setup:
    keeper: GroupFileKeeper
    host: FakeFileHost
    drive: FakeDrive
    groups: QQGroups
    clock: Clock
    temp: Path
    jobs_file: Path

    def post(self, name: str, data: bytes = b"subtitle", *, group: str = GROUP, msg_id: str = "MSG1",
             posted: str | None = POSTED, size: int | None = None, url: str | None = None) -> str:
        """A member posts a file: the host serves it, the gateway hands it over. Returns its address."""
        url = self.host.add(data, url)
        self.keeper.posted(group, msg_id, posted, attachment(name, url, len(data) if size is None else size))
        return url

    def settle(self) -> None:
        """Run the worker until nothing more can be done now."""
        asyncio.run(settle(self.keeper))


async def settle(keeper: GroupFileKeeper) -> None:
    while await keeper.download_next() or await keeper.upload_next():
        pass


def make(tmp_path: Path, *, drive: FakeDrive | None = None, no_drive: bool = False, to_drive: bool = True,
         **kw: Any) -> Setup:
    """A keeper for one group (GROUP, folder FOLDER) with a fake host, drive and clock;
    `no_drive`: the drive is not available."""
    host, clock = FakeFileHost(), Clock()
    drive = drive or FakeDrive()
    groups = qq_groups(tmp_path, to_drive=to_drive)
    temp = tmp_path / "dl" / ".group_files"
    jobs_file = tmp_path / "data" / "group_file_jobs.json"
    keeper = GroupFileKeeper(groups, None if no_drive else drive, jobs_file=jobs_file, temp=temp,
                             client=host.client(), clock=clock, **kw)
    return Setup(keeper, host, drive, groups, clock, temp, jobs_file)
