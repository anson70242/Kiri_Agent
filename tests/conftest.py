"""Shared pytest setup."""

import pytest


@pytest.fixture(autouse=True)
def cheap_password_hashing(monkeypatch):
    """scrypt is slow on purpose (~50 ms a hash, far more on a busy machine); tests hash
    passwords dozens of times, so they use a tiny cost. Verifying reads the cost stored in
    each hash, so the code under test is the same."""
    monkeypatch.setattr("backend.auth.SCRYPT_N", 2**4)


@pytest.fixture(autouse=True)
def no_flash_service(monkeypatch):
    """No flash service is configured in a test, whatever the checkout's `.env` or the
    environment says: the dashboard and the pipeline would call a real one. Each setting is
    set empty (which counts as unset), and loading `.env` never overrides a variable already
    set. A test that wants a flash service passes its settings or sets them itself."""
    from backend.flash.client import ACTIONS_URL_ENV, DOWNLOADS_PATH_ENV, LOGIN_HASH_ENV, LOGIN_URL_ENV

    for name in (ACTIONS_URL_ENV, LOGIN_URL_ENV, LOGIN_HASH_ENV, DOWNLOADS_PATH_ENV):
        monkeypatch.setenv(name, "")


@pytest.fixture(autouse=True)
def restore_incident_hook():
    """Command line entry points install a process-wide incident hook; put back whatever
    was there so one test's hook never reaches the next."""
    from backend.state_file import set_incident_hook

    previous = set_incident_hook(None)
    set_incident_hook(previous)
    yield
    set_incident_hook(previous)
