"""A fake of Quark's CLI for the Quark module's tests: it replaces the one function that runs
the CLI (`backend.quark.run_cli`) and answers each command with the lines the real CLI
printed in ticket 01 (ids, codes and addresses invented)."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

FOLDER_ID = "0123456789abcdef0123456789abcdef"
FILE_ID = "fedcba9876543210fedcba9876543210"
RECORD_ID = "11111111-2222-3333-4444-555555555555"

NOISE = ["cat: /sys/class/dmi/id/product_name: No such file or directory",
         "[ERROR] something the CLI complains about", "{not json", '{"no": "type"}', "[1, 2]"]


def line(**fields) -> str:
    return json.dumps(fields, ensure_ascii=False)


def result(action: str, code: int = 0, msg: str = "成功", data: dict | None = None) -> str:
    return line(code=code, msg=msg, data=data if data is not None else {}, action=action, type="result")


def lapsed_11001(action: str) -> str:
    return result(action, 11001, "长效Access Token过期，需触发刷新")


LAPSED_103 = result("login", -103, "未登录，请先执行 login 命令完成登录授权")

USER_INFO = result("get-user-info", data={
    "vipInfo": {"vipType": "SVIP", "expiresIn": 1800000000000, "used": 250000000000, "capacity": 6597069766656},
    "userInfo": {"nickname": "某个昵称"}})


def create_folder(fid: str = FOLDER_ID) -> str:
    return result("create-folder", data={"fid": fid, "full_path": "夸克网盘/录播"})


def progress(current: int, total: int) -> str:
    return line(msg="进行中", data={"current": current, "total": total, "percent": current * 100 // total},
                action="upload", type="progress")


def uploaded(name: str, size: int) -> list[str]:
    return [progress(size // 2, size), progress(size, size),
            line(code=0, msg="上传成功", data={"recordId": RECORD_ID, "fileId": FILE_ID, "fileName": name,
                                              "fileSize": size, "instantUpload": False}, action="upload", type="list"),
            result("upload", data={"fileNames": [name], "fileCount": 1, "totalSize": size, "fids": [FILE_ID],
                                   "successCount": 1, "instantUpload": False, "instantUploadCount": 0,
                                   "fullPath": "夸克网盘/录播/柊優花"})]


def upload_failed(code: int, msg: str, record: bool = True) -> list[str]:
    lines = [progress(1, 10)]
    if record:
        lines.append(line(code=code, msg=msg, data={"recordId": RECORD_ID}, action="upload", type="list"))
    return lines + [result("upload", code, msg, {"fids": [], "successCount": 0, "failedCount": 1})]


def browse(files: list[tuple[str, int]]) -> Callable[[list[str], Path], list[str]]:
    """`browse --all`: the JSONL file under `browse/` in the working directory, then the
    preview and the artifact line naming the file."""

    def answer(argv: list[str], cwd: Path) -> list[str]:
        folder = cwd / "browse"
        folder.mkdir(exist_ok=True)
        jsonl = folder / "browse-20261118-142901-aa3718.jsonl"
        entries = [{"fid": f"~1AAAA{i}|BBBB", "parent_fid": "~1CCCC|DDDD", "category": 7, "filename": name,
                    "size": size, "file_type": "1", "full_path": [{"missing": "录播"}],
                    "format_type": "application/zip", "source_display": "agent_upload",
                    "created_at": 1795012139891, "updated_at": 1795012139891} for i, (name, size) in enumerate(files)]
        jsonl.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries), encoding="utf-8")
        return [result("browse", data={"total": len(entries), "hasMore": False, "file_list": entries[:5]}),
                line(code=0, msg="成功", data={"file_path": str(jsonl), "count": len(entries), "format": "jsonl",
                                             "description": "…"}, action="browse", type="artifact")]

    return answer


UPLOAD_DELETED = result("upload-delete", data={"recordId": RECORD_ID})


class FakeProcess:
    """One run of the fake CLI: its lines, then (when `hang`) no more output until killed."""

    def __init__(self, lines: list[str], hang: bool = False):
        self._lines, self._hang = lines, hang
        self.killed = threading.Event()

    def __iter__(self) -> Iterator[str]:
        yield from self._lines
        if self._hang:
            self.killed.wait(30)

    def kill(self) -> None:
        self.killed.set()


class FakeCli:
    """Answers by command (`upload delete` and `upload list` by their two words): a list of
    lines, a callable (argv, cwd) -> lines, or a FakeProcess. A command answered with a
    list of answers takes them in turn. Records every call as (argv, cwd, env)."""

    def __init__(self, answers: dict):
        self.answers = dict(answers)
        self.calls: list[tuple[list[str], Path, dict[str, str]]] = []
        self.processes: list[FakeProcess] = []

    def __call__(self, argv: list[str], cwd: Path, env: dict[str, str]) -> FakeProcess:
        self.calls.append((list(argv), Path(cwd), dict(env)))
        args = argv[2:]  # node, the script, then the CLI's own arguments
        key = " ".join(args[:2]) if args[:2] in (["upload", "delete"], ["upload", "list"]) else args[0]
        answer = self.answers.get(key)
        if isinstance(answer, tuple):  # one answer per call, in turn
            answer, rest = answer[0], answer[1:]
            self.answers[key] = rest if rest else answer
        if answer is None:
            answer = [result(key.replace(" ", "-"), -999, f"unexpected command: {args}")]
        if callable(answer) and not isinstance(answer, FakeProcess):
            answer = answer(argv, Path(cwd))
        process = answer if isinstance(answer, FakeProcess) else FakeProcess(answer)
        self.processes.append(process)
        return process

    def commands(self) -> list[list[str]]:
        """The CLI's own arguments of each call."""
        return [argv[2:] for argv, _, _ in self.calls]


SHARE_ID = "0a1b2c3d4e5f"
SHARE_URL = f"https://pan.quark.cn/s/{SHARE_ID}"
PASSCODE = "Ab3d"
SHARED = result("share", data={"pwd_id": SHARE_ID, "share_url": SHARE_URL, "passcode": PASSCODE,
                               "url_type": 2, "expired_type": 1})
