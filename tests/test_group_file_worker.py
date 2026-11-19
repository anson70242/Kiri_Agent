"""The worker as the service runs it: woken by a posted file, it downloads and uploads on
its own, and stops when cancelled."""

from __future__ import annotations

import asyncio

from .group_file_fakes import FOLDER, make


def test_the_running_worker_files_a_posted_file_on_its_own_and_stops_when_cancelled(tmp_path):
    s = make(tmp_path)

    async def scenario() -> None:
        task = asyncio.create_task(s.keeper.run())
        await asyncio.sleep(0)
        s.post("【待校】第3话.ass", b"subtitle")
        for _ in range(200):
            if s.drive.put:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()

    asyncio.run(scenario())
    assert s.drive.put == [(f"{FOLDER}/待校", "【待校】第3话.ass", b"subtitle")]
