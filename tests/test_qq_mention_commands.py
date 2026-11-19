"""Mentioning the bot: a mention arrives as its own event or, in a group that sends the bot
everything, as an ordinary message whose `mentions` marks the bot; its first word is a
command. Fake QQ API, events dispatched straight into the gateway."""

from __future__ import annotations

from backend.notify.qq_gateway import COMMANDS, Command

from .test_qq import FakeQQ
from .test_qq_gateway import dispatch, gateway

BOT = {"id": "BOTID", "member_openid": "BOTOPENID", "is_you": True, "bot": True,
       "member_role": "member", "username": "测试机器人"}
MEMBER = {"id": "MEMID", "member_openid": "MEMOPENID", "is_you": False, "bot": False,
          "member_role": "member", "username": "群友"}


def ordinary(group, msg_id, content, *, mentions=(), role="member"):
    """A GROUP_MESSAGE_CREATE as a group that sends the bot everything delivers it."""
    d = {"group_openid": group, "id": msg_id, "content": content,
         "author": {"member_openid": "AUTHOR", "member_role": role, "bot": False}}
    if mentions:
        d["mentions"] = list(mentions)
    return d


def text(fake, i=-1):
    return fake.messages[i][1]["markdown"]["content"]


def test_an_ordinary_message_that_mentions_the_bot_is_answered_as_a_mention(tmp_path):
    fake = FakeQQ()
    gw, groups, _ = gateway(tmp_path, fake)

    dispatch(gw, "GROUP_MESSAGE_CREATE", ordinary("G1", "MSG1", "<@BOTID> 你好", mentions=[BOT]))

    assert groups.get("G1") is not None
    (group, body), = fake.messages
    assert group == "G1" and body["msg_id"] == "MSG1"  # a reply to the mention
    assert body["markdown"]["content"].startswith("已登记本群 ✅")


def test_a_message_that_mentions_a_member_or_nobody_is_not_answered_and_registers_nothing(tmp_path):
    fake = FakeQQ()
    gw, groups, _ = gateway(tmp_path, fake)

    dispatch(gw, "GROUP_MESSAGE_CREATE", ordinary("G1", "MSG1", "大家好"))
    dispatch(gw, "GROUP_MESSAGE_CREATE", ordinary("G1", "MSG2", "<@MEMID> 订阅", mentions=[MEMBER]))

    assert groups.get("G1") is None and fake.messages == []

    groups.register("G1")
    dispatch(gw, "GROUP_MESSAGE_CREATE", ordinary("G1", "MSG3", "<@MEMID> 帮助", mentions=[MEMBER], role="owner"))
    assert fake.messages == []


def test_subscribe_answers_the_status_and_help_anything_else_or_nothing_the_help(tmp_path):
    fake = FakeQQ()
    gw, groups, _ = gateway(tmp_path, fake)
    groups.register("G1")
    groups.update("G1", streamers=["youtube:@a"], enabled=True)

    dispatch(gw, "GROUP_MESSAGE_CREATE", ordinary("G1", "M1", "<@BOTID> 订阅", mentions=[BOT], role="owner"))
    assert text(fake).startswith("已登记本群 ✅") and "- A (YouTube)" in text(fake)
    assert fake.messages[-1][1]["msg_id"] == "M1"

    for i, content in enumerate(["<@BOTID> 帮助", "<@BOTID> 订阅吧", "<@BOTID> 你好 订阅", "<@BOTID>", "<@BOTID>  "]):
        dispatch(gw, "GROUP_MESSAGE_CREATE", ordinary("G1", f"H{i}", content, mentions=[BOT], role="owner"))
        help_text = text(fake)
        assert fake.messages[-1][1]["msg_id"] == f"H{i}"
        assert "已登记本群" not in help_text, content
        # both commands, one a line, with what each does
        assert any(line.startswith("- 订阅：") and len(line) > len("- 订阅：") for line in help_text.splitlines())
        assert any(line.startswith("- 帮助：") and len(line) > len("- 帮助：") for line in help_text.splitlines())


def test_a_mention_event_reads_its_command_the_same_way(tmp_path):
    fake = FakeQQ()
    gw, groups, _ = gateway(tmp_path, fake)
    groups.register("G1")
    owner = {"member_role": "owner"}

    dispatch(gw, "GROUP_AT_MESSAGE_CREATE", {"group_openid": "G1", "id": "M1", "content": " 订阅 一下", "author": owner})
    assert text(fake).startswith("已登记本群 ✅")
    dispatch(gw, "GROUP_AT_MESSAGE_CREATE", {"group_openid": "G1", "id": "M2", "content": " 帮助", "author": owner})
    assert "- 订阅：" in text(fake) and fake.messages[-1][1]["msg_id"] == "M2"
    # a mention event's text carries no <@…>, but if one is there it is no command either
    dispatch(gw, "GROUP_AT_MESSAGE_CREATE", {"group_openid": "G1", "id": "M3", "content": "<@BOTID>订阅",
                                             "author": owner})
    assert text(fake).startswith("已登记本群 ✅")


def test_a_new_groups_first_mention_registers_it_and_gets_the_status_whatever_it_says(tmp_path):
    fake = FakeQQ()
    gw, groups, _ = gateway(tmp_path, fake)

    dispatch(gw, "GROUP_MESSAGE_CREATE", ordinary("G1", "M1", "<@BOTID> 帮助", mentions=[BOT]))
    dispatch(gw, "GROUP_AT_MESSAGE_CREATE", {"group_openid": "G2", "id": "M2", "content": " 随便",
                                             "author": {"member_role": "member"}})

    assert groups.get("G1") is not None and groups.get("G2") is not None
    assert [(g, b["msg_id"]) for g, b in fake.messages] == [("G1", "M1"), ("G2", "M2")]
    assert all(b["markdown"]["content"].startswith("已登记本群 ✅") for _, b in fake.messages)


def test_a_plain_member_gets_the_help_but_not_the_status(tmp_path):
    fake = FakeQQ()
    gw, groups, _ = gateway(tmp_path, fake)
    groups.register("G1")

    dispatch(gw, "GROUP_MESSAGE_CREATE", ordinary("G1", "M1", "<@BOTID> 订阅", mentions=[BOT]))
    dispatch(gw, "GROUP_AT_MESSAGE_CREATE", {"group_openid": "G1", "id": "M2", "content": " 订阅",
                                             "author": {"member_role": "member"}})
    assert fake.messages == []

    dispatch(gw, "GROUP_MESSAGE_CREATE", ordinary("G1", "M3", "<@BOTID> 帮助", mentions=[BOT]))
    dispatch(gw, "GROUP_AT_MESSAGE_CREATE", {"group_openid": "G1", "id": "M4", "content": " 什么",
                                             "author": {"member_role": "member"}})
    assert [b["msg_id"] for _, b in fake.messages] == ["M3", "M4"]
    assert all("- 订阅：" in b["markdown"]["content"] for _, b in fake.messages)

    dispatch(gw, "GROUP_MESSAGE_CREATE", ordinary("G1", "M5", "<@BOTID> 订阅", mentions=[BOT], role="admin"))
    assert text(fake).startswith("已登记本群 ✅")


def test_being_added_is_still_answered_with_the_status(tmp_path):
    fake = FakeQQ()
    gw, groups, _ = gateway(tmp_path, fake)
    groups.register("G1")

    dispatch(gw, "GROUP_ADD_ROBOT", {"group_openid": "G1", "op_member_openid": "M1"}, event_id="EV7")

    (_, body), = fake.messages
    assert body["event_id"] == "EV7" and body["markdown"]["content"].startswith("已登记本群 ✅")


def test_nothing_of_a_messages_text_is_logged(tmp_path, caplog):
    fake = FakeQQ()
    gw, groups, _ = gateway(tmp_path, fake)
    caplog.set_level("DEBUG")
    secret = "秘密的聊天内容"

    dispatch(gw, "GROUP_MESSAGE_CREATE", ordinary("G1", "M1", f"<@BOTID> {secret}", mentions=[BOT]))
    dispatch(gw, "GROUP_MESSAGE_CREATE", ordinary("G1", "M2", f"<@BOTID> {secret}", mentions=[BOT]))
    dispatch(gw, "GROUP_MESSAGE_CREATE", ordinary("G1", "M3", f"<@BOTID> 订阅 {secret}", mentions=[BOT]))
    dispatch(gw, "GROUP_MESSAGE_CREATE", ordinary("G1", "M4", secret))
    dispatch(gw, "GROUP_AT_MESSAGE_CREATE", {"group_openid": "G1", "id": "M5", "content": f" {secret}",
                                             "author": {"member_role": "owner"}})

    assert len(fake.messages) == 3  # the first, the help, the help
    assert secret not in caplog.text and "秘密" not in caplog.text


def test_a_third_command_is_one_entry_in_the_table(tmp_path, monkeypatch):
    monkeypatch.setitem(COMMANDS, "网盘", Command("查看网盘", lambda gw, g: "网盘的回答"))
    fake = FakeQQ()
    gw, groups, _ = gateway(tmp_path, fake)
    groups.register("G1")

    dispatch(gw, "GROUP_MESSAGE_CREATE", ordinary("G1", "M1", "<@BOTID> 网盘", mentions=[BOT]))
    dispatch(gw, "GROUP_MESSAGE_CREATE", ordinary("G1", "M2", "<@BOTID> 帮助", mentions=[BOT]))

    assert text(fake, 0) == "网盘的回答"
    assert "- 网盘：查看网盘" in text(fake, 1).splitlines()
