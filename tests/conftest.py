"""Shared pytest setup."""

import pytest


@pytest.fixture(autouse=True)
def cheap_password_hashing(monkeypatch):
    """scrypt is slow on purpose (~50 ms a hash, far more on a busy machine); tests hash
    passwords dozens of times, so they use a tiny cost. Verifying reads the cost stored in
    each hash, so the code under test is the same."""
    monkeypatch.setattr("backend.auth.SCRYPT_N", 2**4)
