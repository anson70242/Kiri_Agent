"""The Quark module's first slice: whether a drive is available, authorizing it, reading its
status; run through a fake CLI (no Node, no Quark)."""

from __future__ import annotations

import json
import os
import sys
import time
import logging

import pytest

from backend import quark

from .quark_fakes import LAPSED_103, NOISE, USER_INFO, FakeCli, lapsed_11001, result


@pytest.fixture
def config(tmp_path):
    """A config naming a CLI script that exists."""
    script = tmp_path / "data" / "quark" / "quarkclouddrive-1.0.25" / "scripts" / "quark-drive.cjs"
    script.parent.mkdir(parents=True)
    script.write_text("// the CLI", encoding="utf-8")
    return write_config(tmp_path, {"quark_cli": str(script)})


def write_config(tmp_path, drive: dict | None):
    cfg = tmp_path / "config.json"
    body = {"download": {"output_dir": str(tmp_path / "dl")}}
    if drive is not None:
        body["drive"] = drive
    cfg.write_text(json.dumps(body), encoding="utf-8")
    return cfg


@pytest.fixture
def cli(monkeypatch):
    fake = FakeCli({})
    monkeypatch.setattr(quark, "run_cli", fake)
    return fake


# ---- available or not ----

@pytest.mark.parametrize("drive", [{"quark_cli": "nowhere/quark-drive.cjs"}, {"quark_cli": ""}],
                         ids=["CLI file missing", "feature off"])
def test_without_the_cli_file_the_drive_is_not_available_with_one_warning(tmp_path, caplog, cli, drive):
    with caplog.at_level(logging.WARNING):
        assert quark.load_quark(write_config(tmp_path, drive)) is None
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
    assert cli.calls == []


def test_the_cli_named_in_the_config_is_the_drive(config, cli):
    assert quark.load_quark(config) is not None


def test_the_cli_path_defaults_to_the_data_volume(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        assert quark.load_quark(write_config(tmp_path, None)) is None  # not copied there in a checkout
    assert "data/quark/quarkclouddrive-1.0.25/scripts/quark-drive.cjs" in caplog.text.replace("\\", "/")


# ---- status ----

def status(config, capsys):
    code = quark.main(["status", "--config", str(config)])
    return code, capsys.readouterr().out


def test_status_prints_the_membership_its_end_and_the_space_never_the_nickname(config, cli, capsys):
    cli.answers["get-user-info"] = NOISE + [USER_INFO]
    code, out = status(config, capsys)

    assert code == 0
    assert "已授权" in out and "SVIP" in out
    assert "2027-01-15" in out  # expiresIn 1800000000000 ms
    assert "232.8 GB" in out and "6.0 TB" in out  # used 250 000 000 000 of 6 597 069 766 656 bytes
    assert "某个昵称" not in out
    assert cli.commands() == [["get-user-info"]]


def test_the_cli_runs_with_node_and_the_marker_and_nothing_else_gets_the_marker(config, cli, capsys, monkeypatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    cli.answers["get-user-info"] = [USER_INFO]
    status(config, capsys)

    (argv, cwd, env), = cli.calls
    assert argv[:2] == ["node", str(config.parent / "data/quark/quarkclouddrive-1.0.25/scripts/quark-drive.cjs")]
    assert env["CLAUDECODE"] == "1"
    assert "CLAUDECODE" not in os.environ
    assert not cwd.exists()  # a scratch working directory, cleaned up


@pytest.mark.parametrize("answer", [LAPSED_103, lapsed_11001("get-user-info"),
                                    result("get-user-info", -106, "浏览器打开失败"),
                                    result("not_authenticated", -1408, "未完成授权认证")],
                         ids=["-103", "11001", "-106", "-1408"])
def test_status_of_a_lapsed_authorization_says_to_authorize_again(config, cli, capsys, answer):
    cli.answers["get-user-info"] = [answer]
    code, out = status(config, capsys)

    assert code == 1
    assert "授权已失效" in out and "python -m backend.quark login" in out


def test_status_that_fails_otherwise_gives_the_cli_message(config, cli, capsys):
    cli.answers["get-user-info"] = [result("get-user-info", -2301, "网络错误")]
    code, out = status(config, capsys)

    assert code == 1
    assert "网络错误" in out and "授权已失效" not in out


def test_a_run_without_a_result_line_fails(config, cli, capsys):
    cli.answers["get-user-info"] = NOISE
    code, out = status(config, capsys)

    assert code == 1
    assert "没有给出结果" in out


# ---- login ----

AUTH_URL = "https://auth.example/oauth/authorize?client_id=c0ffee&device_id=d00d&agent_id=claudecode"
NEEDS_CODE = result("login", -106, "你好像还没完成授权？如果已经扫码确认了，发任意消息，或是复制授权码给我，"
                    f"我来检测一下；还没完成的话，点下方链接重新操作就好。🔗 [前往授权]{AUTH_URL}\n\n"
                    "绑定夸克网盘后，对话文件随时归档。")


def login(config, capsys, *args):
    code = quark.main(["login", *args, "--config", str(config)])
    return code, capsys.readouterr().out


def test_login_prints_the_authorization_link_and_how_to_finish(config, cli, capsys):
    cli.answers["login"] = NOISE + [NEEDS_CODE]
    code, out = login(config, capsys)

    assert code == 0
    assert AUTH_URL in out.split()
    assert "login --code" in out
    assert cli.commands() == [["login"]]


def test_login_when_already_authorized_says_so_without_the_nickname(config, cli, capsys):
    cli.answers["login"] = [result("login", -118, "当前账号已授权，若想切换账号，请先解除授权（某个昵称）")]
    code, out = login(config, capsys)

    assert code == 0
    assert "已授权" in out and "某个昵称" not in out


def test_login_with_the_code_completes_the_authorization(config, cli, capsys):
    cli.answers["login"] = [result("login", data={"userId": "u-123", "status": "confirmed",
                                                  "msg": "授权登录成功，继续未完成的任务"})]
    code, out = login(config, capsys, "--code", "c0de-from-the-page")

    assert code == 0
    assert "授权成功" in out and "u-123" not in out
    assert cli.commands() == [["login", "--token", "c0de-from-the-page"]]


def test_login_with_a_refused_code_says_why(config, cli, capsys):
    cli.answers["login"] = [result("login", -102, "授权码无效或已过期")]
    code, out = login(config, capsys, "--code", "stale")

    assert code == 1
    assert "授权码无效或已过期" in out


def test_no_command_passes_the_session_arguments(config, cli, capsys):
    cli.answers.update({"login": [NEEDS_CODE], "get-user-info": [USER_INFO]})
    login(config, capsys)
    login(config, capsys, "--code", "c")
    status(config, capsys)

    assert not [a for args in cli.commands() for a in args if a.startswith("--session")]


# ---- the real runner, on a stand-in for Node ----

CHILD = """
import os, sys, time
print(os.environ.get("CLAUDECODE"), os.getcwd(), flush=True)
print('{"type": "progress", "msg": "进行中"}', flush=True)
time.sleep(30)
print("never", flush=True)
"""


def test_the_runner_streams_lines_as_they_come_and_can_be_killed(tmp_path):
    process = quark.run_cli([sys.executable, "-c", CHILD], tmp_path, {**os.environ, "CLAUDECODE": "1", "PYTHONIOENCODING": "utf-8"})  # Node writes UTF-8
    lines = iter(process)
    started = time.monotonic()
    assert next(lines).split() == ["1", str(tmp_path)]
    assert next(lines) == '{"type": "progress", "msg": "进行中"}'
    process.kill()
    assert list(lines) == []
    assert time.monotonic() - started < 10  # not after the child's 30 seconds
