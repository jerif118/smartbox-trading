"""Tests del decorator retry (backoff con cap de delay)."""

from __future__ import annotations

import pytest

from utils import retry as retry_mod
from utils.retry import retry


def test_retry_succeeds_after_failures(monkeypatch):
    monkeypatch.setattr(retry_mod.time, "sleep", lambda s: None)
    calls = {"n": 0}

    @retry(max_retries=3, initial_delay=0.01)
    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ValueError("aún no")
        return "ok"

    assert flaky() == "ok"
    assert calls["n"] == 3


def test_retry_exhausts_and_raises(monkeypatch):
    monkeypatch.setattr(retry_mod.time, "sleep", lambda s: None)

    @retry(max_retries=2, initial_delay=0.01)
    def always_fails():
        raise ValueError("siempre")

    with pytest.raises(ValueError):
        always_fails()


def test_retry_delay_capped_at_max_delay(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(retry_mod.time, "sleep", sleeps.append)

    @retry(max_retries=5, backoff=10.0, initial_delay=1.0, max_delay=5.0)
    def always_fails():
        raise ValueError("siempre")

    with pytest.raises(ValueError):
        always_fails()

    # delays: 1, luego min(1*10,5)=5, 5, 5, 5 — nunca por encima del cap
    assert sleeps[0] == 1.0
    assert all(s <= 5.0 for s in sleeps)
    assert sleeps.count(5.0) == 4
