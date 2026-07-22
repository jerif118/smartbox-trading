"""Tests del contexto de logs por run/símbolo (ContextFilter)."""

from __future__ import annotations

import logging

from utils.logger import _LOG_FORMAT, ContextFilter, log_context


def _format_line(logger_name: str = "test") -> tuple[logging.Handler, list[str]]:
    """Handler que captura líneas formateadas con el ContextFilter instalado."""
    lines: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(self.format(record))

    h = _Capture()
    h.setFormatter(logging.Formatter(_LOG_FORMAT))
    h.addFilter(ContextFilter())
    return h, lines


def test_no_context_has_no_prefix():
    h, lines = _format_line()
    log = logging.getLogger("test.noctx")
    log.addHandler(h)
    log.setLevel(logging.INFO)
    log.propagate = False
    log.info("hola")
    log.removeHandler(h)
    assert lines[-1].endswith("| hola")


def test_context_adds_run_and_symbol_prefix():
    h, lines = _format_line()
    log = logging.getLogger("test.ctx")
    log.addHandler(h)
    log.setLevel(logging.INFO)
    log.propagate = False
    with log_context(run_id="abcdef1234", symbol="US500"):
        log.info("con contexto")
    log.removeHandler(h)
    assert "[abcdef12][US500] con contexto" in lines[-1]


def test_context_resets_after_block():
    h, lines = _format_line()
    log = logging.getLogger("test.reset")
    log.addHandler(h)
    log.setLevel(logging.INFO)
    log.propagate = False
    with log_context(run_id="run123456", symbol="US100"):
        log.info("dentro")
    log.info("fuera")
    log.removeHandler(h)
    assert "[run12345][US100] dentro" in lines[0]
    assert lines[1].endswith("| fuera")  # sin prefijo tras salir del bloque
