"""No test calls a real flash service: whatever a checkout's `.env` names, every test starts
with no flash service configured (the guard in tests/conftest.py)."""

from __future__ import annotations

import json

from dotenv import load_dotenv

import backend.flash.client
from backend.flash.client import flash_configured, load_flash


def test_a_checkout_whose_env_file_names_a_flash_service_gives_a_test_none(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("FLASH_ACTIONS_URL=ws://flash.example:3001\n"
                                   "FLASH_LOGIN_URL=http://flash.example:6099\n"
                                   "FLASH_LOGIN_HASH=made-up-hash\n", encoding="utf-8")
    monkeypatch.setattr(backend.flash.client, "REPO_ROOT", tmp_path)  # the checkout
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"download": {"output_dir": str(tmp_path / "downloads")}}), encoding="utf-8")

    load_dotenv(tmp_path / ".env")  # as any other module of the service loads it

    assert load_flash(config) is None
    assert not flash_configured()


def test_a_test_that_wants_a_flash_service_switches_it_on_itself(monkeypatch):
    monkeypatch.setenv("FLASH_ACTIONS_URL", "ws://flash.example:3001")

    assert flash_configured()
