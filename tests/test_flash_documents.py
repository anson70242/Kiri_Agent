"""`.env.example` names every setting the flash service client reads, commented out: a reader
finds how to switch flash transfers on, and a fresh copy of the file leaves them off."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.flash.client import ACTIONS_URL_ENV, DOWNLOADS_PATH_ENV, LOGIN_HASH_ENV, LOGIN_URL_ENV

ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"


@pytest.mark.parametrize("name", [ACTIONS_URL_ENV, LOGIN_URL_ENV, LOGIN_HASH_ENV, DOWNLOADS_PATH_ENV])
def test_env_example_names_each_flash_setting_commented_out(name: str) -> None:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert re.search(rf"^# {name}=\S", text, re.MULTILINE), f"{name} is not in .env.example as '# {name}=...'"
    assert not re.search(rf"^{name}=", text, re.MULTILINE), f"{name} is switched on in .env.example"
