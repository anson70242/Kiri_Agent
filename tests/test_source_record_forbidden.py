"""A sheet the token's account may not edit can be refused with HTTP 403: the owner's give-up
message then says to share the sheet, while what the highlight table's upload reports for a
refused token stays as it was. Against a fake Tencent Docs endpoint and a fake clock; made-up
ids and links."""

from __future__ import annotations

import httpx
import pytest

from backend.tencent_docs import TencentDocsError, TencentSettings, upload_table

from .test_source_record_give_up import run_out
from .test_source_record_processed import processed
from .test_source_record_retries import setup

TABLE = {"rows": []}


def test_a_sheet_refused_with_http_403_is_said_to_need_sharing(tmp_path):
    s, clock, alerts = setup(tmp_path)
    s.docs.forbidden = True
    processed(s.keeper)
    run_out(s, clock)
    (message,) = alerts
    assert "共享给这个账号" in message and "HTTP 403" in message


def test_a_token_refused_with_http_401_says_nothing_about_sharing(tmp_path):
    s, clock, alerts = setup(tmp_path)
    s.docs.expired = True
    processed(s.keeper)
    run_out(s, clock)
    (message,) = alerts
    assert "共享" not in message


@pytest.mark.parametrize("status", [401, 403])
def test_the_highlight_tables_upload_reports_a_refused_token_as_before(status):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status, json={"error": "x"})))
    with pytest.raises(TencentDocsError) as e:
        upload_table(TABLE, TencentSettings(token="tok-FAKE", file_id="FAKEFILE4"), sheet_title="t", client=client)
    assert str(e.value).startswith(f"Tencent Docs rejected the token (HTTP {status}, invalid or expired): get a new one")
    assert e.value.code == status
