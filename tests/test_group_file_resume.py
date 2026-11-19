"""A download of a group file that broke off continues where it stopped, with a range
request, when its part is still on disk; it starts over when the host does not answer with
the range. The file's SHA-256 is that of the whole file either way."""

from __future__ import annotations

import asyncio
import hashlib

import httpx

from backend.group_files import GroupFileKeeper
from backend.group_files.jobs import GroupFileJobs

from .group_file_fakes import FOLDER, FakeFileHost, make
from .test_group_file_retries import INTERVAL, Told

DATA = bytes(range(256)) * 400  # 102 400 bytes


class Broken(httpx.AsyncByteStream):
    """A body that breaks off after `upto` bytes."""

    def __init__(self, data: bytes, upto: int) -> None:
        self.data, self.upto = data, upto

    async def __aiter__(self):
        yield self.data[:self.upto]
        raise httpx.ReadError("connection reset")


class RangeHost(FakeFileHost):
    """QQ's file host answering range requests (when `ranges`), whose next answer for an
    address can break off after some bytes (`break_after`). Notes each Range header."""

    def __init__(self, ranges: bool = True) -> None:
        super().__init__()
        self.ranges = ranges
        self.break_after: dict[str, int] = {}
        self.range_headers: list[str | None] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        self.range_headers.append(request.headers.get("range"))
        data = self.files.get(url)
        if data is None:
            return httpx.Response(404)
        start, status, headers = 0, 200, {}
        if self.ranges and (asked := request.headers.get("range")):
            start = int(asked.removeprefix("bytes=").split("-")[0])
            status, headers = 206, {"Content-Range": f"bytes {start}-{len(data) - 1}/{len(data)}"}
        body = data[start:]
        if url in self.break_after:
            return httpx.Response(status, headers=headers, stream=Broken(body, self.break_after.pop(url)))
        return httpx.Response(status, headers=headers, content=body)


def setup(tmp_path, ranges=True):
    s = make(tmp_path, retry_interval=INTERVAL, report=Told())
    s.host = RangeHost(ranges)
    s.keeper.client = s.host.client()
    return s


def broken_off(s, at: int) -> str:
    url = s.post("【熟肉】第3话.mp4", DATA)
    s.host.break_after[url] = at
    asyncio.run(s.keeper.download_next())
    (part,) = [p for p in s.temp.rglob("*.part")]
    assert part.read_bytes() == DATA[:at]  # kept for the next try
    return url


def test_a_download_that_broke_off_continues_with_a_range_request(tmp_path):
    s = setup(tmp_path)
    broken_off(s, 30_000)

    s.clock.t += INTERVAL
    s.settle()
    assert s.host.range_headers == [None, "bytes=30000-"]
    assert s.drive.put == [(f"{FOLDER}/熟肉", "【熟肉】第3话.mp4", DATA)]


def test_a_continued_download_has_the_sha256_of_the_whole_file(tmp_path):
    s = setup(tmp_path)
    broken_off(s, 30_000)
    s.clock.t += INTERVAL
    asyncio.run(s.keeper.download_next())
    (job,) = GroupFileJobs(s.jobs_file).load()
    assert job.downloaded and job.sha256 == hashlib.sha256(DATA).hexdigest()


def test_a_download_starts_over_when_the_host_does_not_answer_with_the_range(tmp_path):
    s = setup(tmp_path, ranges=False)
    broken_off(s, 30_000)

    s.clock.t += INTERVAL
    s.settle()
    assert s.host.range_headers == [None, "bytes=30000-"]
    assert s.drive.put == [(f"{FOLDER}/熟肉", "【熟肉】第3话.mp4", DATA)]


def test_a_part_left_by_a_restart_is_continued(tmp_path):
    s = setup(tmp_path)
    url = s.post("【熟肉】第3话.mp4", DATA)
    (job,) = GroupFileJobs(s.jobs_file).load()
    (s.temp / job.id).mkdir(parents=True)
    (s.temp / job.id / "【熟肉】第3话.mp4.part").write_bytes(DATA[:50_000])  # the service was killed

    keeper = GroupFileKeeper(s.groups, s.drive, jobs_file=s.jobs_file, temp=s.temp,
                             client=s.host.client(), clock=s.clock)

    async def scenario() -> None:
        task = asyncio.create_task(keeper.run())
        for _ in range(300):
            await asyncio.sleep(0.01)
            if s.drive.put:
                break
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert s.host.requests == [url] and s.host.range_headers == ["bytes=50000-"]
    assert [p[2] for p in s.drive.put] == [DATA]


def test_a_part_longer_than_the_file_is_not_continued(tmp_path):
    s = setup(tmp_path)
    s.post("【熟肉】第3话.mp4", DATA)
    (job,) = GroupFileJobs(s.jobs_file).load()
    (s.temp / job.id).mkdir(parents=True)
    (s.temp / job.id / "【熟肉】第3话.mp4.part").write_bytes(DATA + b"more")
    s.settle()
    assert s.host.range_headers == [None] and [p[2] for p in s.drive.put] == [DATA]
