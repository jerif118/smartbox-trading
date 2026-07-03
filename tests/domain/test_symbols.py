"""Tests del whitelist de símbolos (solo US500 y US100)."""

from __future__ import annotations

import pytest

from domain.symbols import (
    ALLOWED_SYMBOLS,
    filter_symbols,
    is_allowed,
    resolve_primary,
)


def test_allowed_symbols_are_exactly_two() -> None:
    assert ALLOWED_SYMBOLS == ("US500", "US100")


@pytest.mark.parametrize("sym", ["US500", "us500", " US100 ", "us100"])
def test_is_allowed_true(sym: str) -> None:
    assert is_allowed(sym) is True


@pytest.mark.parametrize("sym", ["DE40", "US30", "NAS100", "", "  "])
def test_is_allowed_false(sym: str) -> None:
    assert is_allowed(sym) is False


def test_filter_drops_de40_and_others() -> None:
    result = filter_symbols(["US500", "DE40", "US100", "US30"])
    assert result == ["US500", "US100"]


def test_filter_normalizes_and_dedups() -> None:
    result = filter_symbols([" us500 ", "US500", "us100"])
    assert result == ["US500", "US100"]


def test_filter_logs_ignored(caplog) -> None:
    import logging

    with caplog.at_level(logging.WARNING):
        filter_symbols(["US500", "DE40"], context="test")
    assert "DE40" in caplog.text
    assert "ignorados" in caplog.text.lower()


def test_filter_empty_returns_empty() -> None:
    assert filter_symbols([]) == []
    assert filter_symbols(["DE40", "FOO"]) == []


def test_resolve_primary_keeps_valid() -> None:
    assert resolve_primary("US100", ["US500", "US100"]) == "US100"


def test_resolve_primary_falls_back() -> None:
    # DE40 no está activo → cae al primero de la lista.
    assert resolve_primary("DE40", ["US500", "US100"]) == "US500"


def test_resolve_primary_empty_allowed_uses_whitelist() -> None:
    assert resolve_primary("DE40", []) == "US500"
