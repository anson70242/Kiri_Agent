"""The gateway hands every `file` attachment of a group message, mentioned or not, to the
group-files module, with the group, the message's id and time and the attachment, and
nothing else; the help lists the filing rules for a group whose files are kept. Fake QQ
API, events dispatched straight into the gateway."""

from __future__ import annotations

from .group_file_fakes import POSTED, attachment
from .test_qq import FakeQQ
from .test_qq_gateway import dispatch, gateway
from .test_qq_mention_commands import BOT, ordinary, text

FILE = attachment("【待校】第3话.ass", "https://grpfile.ftn.qq.com/ftn_handler/f1", 1234)
PICTURE = {"content_type": "image/jpeg", "filename": "IMG_0001.jpg", "size": 999, "url": "https://multimedia.nt.qq.com/x",
           "width": 100, "height": 100}
VOICE = {"content_type": "voice", "filename": "v.amr", "size": 9, "url": "https://multimedia.nt.qq.com/v"}
VIDEO = {"content_type": "video/mp4", "filename": "v.mp4", "size": 9, "url": "https://multimedia.nt.qq.com/m"}


def with_files(d, *attachments, timestamp=POSTED):
    return {**d, "attachments": list(attachments), "timestamp": timestamp}


def handing(tmp_path, fake):
    taken: list[tuple] = []
    gw, groups, _ = gateway(tmp_path, fake, take_group_file=lambda *a: taken.append(a))
    return gw, groups, taken


def test_a_file_in_an_ordinary_message_is_handed_over_and_nothing_is_answered_or_registered(tmp_path):
    fake = FakeQQ()
    gw, groups, taken = handing(tmp_path, fake)

    dispatch(gw, "GROUP_MESSAGE_CREATE", with_files(ordinary("G1", "MSG1", ""), FILE))

    assert taken == [("G1", "MSG1", POSTED, FILE)]
    assert fake.messages == [] and groups.get("G1") is None


def test_a_file_in_a_mention_is_handed_over_and_the_mention_answered(tmp_path):
    fake = FakeQQ()
    gw, groups, taken = handing(tmp_path, fake)
    groups.register("G1")

    dispatch(gw, "GROUP_MESSAGE_CREATE", with_files(ordinary("G1", "M1", "<@BOTID> 帮助", mentions=[BOT]), FILE))
    dispatch(gw, "GROUP_AT_MESSAGE_CREATE", with_files({"group_openid": "G1", "id": "M2", "content": " 帮助",
                                                         "author": {"member_role": "member"}}, FILE))

    assert [t[:2] for t in taken] == [("G1", "M1"), ("G1", "M2")]
    assert [b["msg_id"] for _, b in fake.messages] == ["M1", "M2"]


def test_pictures_voice_and_video_are_not_handed_over_and_each_file_is(tmp_path):
    fake = FakeQQ()
    gw, _, taken = handing(tmp_path, fake)
    other = attachment("【已校】第3话.ass", "https://grpfile.ftn.qq.com/ftn_handler/f2", 5)

    dispatch(gw, "GROUP_MESSAGE_CREATE", with_files(ordinary("G1", "M1", "看图"), PICTURE, VOICE, VIDEO))
    dispatch(gw, "GROUP_MESSAGE_CREATE", ordinary("G1", "M2", "只是聊天"))
    assert taken == []

    dispatch(gw, "GROUP_MESSAGE_CREATE", with_files(ordinary("G1", "M3", ""), PICTURE, FILE, other))
    assert [t[3] for t in taken] == [FILE, other]


def test_nothing_else_of_the_message_leaves_the_gateway(tmp_path):
    fake = FakeQQ()
    gw, _, taken = handing(tmp_path, fake)

    dispatch(gw, "GROUP_MESSAGE_CREATE", with_files(ordinary("G1", "M1", "秘密的聊天内容"), FILE))

    assert "秘密" not in repr(taken) and "AUTHOR" not in repr(taken)


def test_a_failure_of_the_module_does_not_stop_the_gateway_and_logs_no_text(tmp_path, caplog):
    fake = FakeQQ()

    def broken(*_):
        raise RuntimeError("module broke")

    gw, groups, _ = gateway(tmp_path, fake, take_group_file=broken)
    groups.register("G1")
    caplog.set_level("DEBUG")

    dispatch(gw, "GROUP_MESSAGE_CREATE", with_files(ordinary("G1", "M1", "<@BOTID> 帮助 秘密", mentions=[BOT]), FILE))

    assert [b["msg_id"] for _, b in fake.messages] == ["M1"]  # the mention is still answered
    assert "秘密" not in caplog.text


def test_without_the_module_files_are_ignored(tmp_path):
    fake = FakeQQ()
    gw, _, _ = gateway(tmp_path, fake)
    dispatch(gw, "GROUP_MESSAGE_CREATE", with_files(ordinary("G1", "M1", ""), FILE))
    assert fake.messages == []


def test_the_help_lists_the_filing_rules_only_for_a_group_whose_files_are_kept(tmp_path):
    fake = FakeQQ()
    gw, groups, _ = gateway(tmp_path, fake)
    groups.register("G1")
    groups.update("G1", name="章鱼烧字幕组")

    dispatch(gw, "GROUP_MESSAGE_CREATE", ordinary("G1", "M1", "<@BOTID> 帮助", mentions=[BOT]))
    assert "【待校】" not in text(fake)

    groups.update("G1", group_files_to_drive=True)
    dispatch(gw, "GROUP_MESSAGE_CREATE", ordinary("G1", "M2", "<@BOTID> 帮助", mentions=[BOT]))
    lines = text(fake).splitlines()
    assert any("- 订阅：" in line for line in lines)  # the commands come first
    rules = [line for line in lines if "→" in line]
    assert len(rules) == 5
    assert any("【待轴】" in line and ".mp4" in line and "章鱼烧字幕组/剪辑/" in line for line in rules)
    assert any("【已校】" in line and ".ass" in line and "章鱼烧字幕组/已校/" in line for line in rules)
