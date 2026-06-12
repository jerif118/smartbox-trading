"""Tests del wrapper run_stage (timeout, métricas, traducción de errores)."""

from __future__ import annotations

import time

import pytest
import requests

from domain.errors import InvalidBoxError
from infrastructure.config.settings import reset_settings_cache
from infrastructure.persistence.sqlite import db, run_repo, stage_metrics_repo
from pipeline.errors import (
    StageDataError,
    StageError,
    StageNetworkError,
    StageTimeoutError,
)
from pipeline.runner import run_stage, translate_exception

RUN_ID = "run-test"


@pytest.fixture(autouse=True)
def temp_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    reset_settings_cache()
    db.reset_db(db_path)
    db.init_db(db_path)
    run_repo.start_run(RUN_ID)
    yield db_path
    db.reset_db(db_path)


def test_run_stage_ok_returns_value_and_persists_metric(temp_db):
    result = run_stage("s_demo", RUN_ID, lambda x: x * 2, 21)
    assert result == 42
    metrics = stage_metrics_repo.list_stage_metrics(RUN_ID)
    assert len(metrics) == 1
    assert metrics[0]["stage"] == "s_demo"
    assert metrics[0]["status"] == "ok"
    assert metrics[0]["duration_ms"] >= 0


def test_run_stage_timeout(temp_db):
    def slow():
        time.sleep(5)

    with pytest.raises(StageTimeoutError):
        run_stage("s_slow", RUN_ID, slow, timeout_s=0.1)
    metrics = stage_metrics_repo.list_stage_metrics(RUN_ID)
    assert metrics[0]["status"] == "timeout"
    assert metrics[0]["error_type"] == "StageTimeoutError"


def test_run_stage_translates_network_error(temp_db):
    def boom():
        raise requests.ConnectionError("broker caído")

    with pytest.raises(StageNetworkError) as exc_info:
        run_stage("s_net", RUN_ID, boom)
    assert exc_info.value.stage == "s_net"
    metrics = stage_metrics_repo.list_stage_metrics(RUN_ID)
    assert metrics[0]["status"] == "error"
    assert metrics[0]["error_type"] == "StageNetworkError"


def test_run_stage_translates_domain_error(temp_db):
    def boom():
        raise InvalidBoxError("amplitud > 1%")

    with pytest.raises(StageDataError):
        run_stage("s_data", RUN_ID, boom)
    metrics = stage_metrics_repo.list_stage_metrics(RUN_ID)
    assert metrics[0]["error_type"] == "StageDataError"


def test_run_stage_passes_through_stage_errors(temp_db):
    original = StageDataError("inner", "ya tipado")

    def boom():
        raise original

    with pytest.raises(StageDataError) as exc_info:
        run_stage("s_outer", RUN_ID, boom)
    assert exc_info.value is original


def test_run_stage_unknown_error_becomes_stage_error(temp_db):
    def boom():
        raise RuntimeError("algo raro")

    with pytest.raises(StageError) as exc_info:
        run_stage("s_unknown", RUN_ID, boom)
    assert not isinstance(exc_info.value, StageDataError | StageNetworkError)
    assert "RuntimeError" in str(exc_info.value)


def test_translate_exception_mapping():
    assert isinstance(
        translate_exception("s", requests.Timeout("t")), StageNetworkError
    )
    assert isinstance(translate_exception("s", ValueError("v")), StageDataError)
    assert isinstance(translate_exception("s", KeyError("k")), StageDataError)
    generic = translate_exception("s", RuntimeError("r"))
    assert type(generic) is StageError


def test_metric_failure_does_not_break_stage(temp_db, monkeypatch):
    def broken_metric(*args, **kwargs):
        raise RuntimeError("DB de métricas rota")

    monkeypatch.setattr(
        "pipeline.runner.stage_metrics_repo.insert_stage_metric", broken_metric
    )
    assert run_stage("s_ok", RUN_ID, lambda: "fine") == "fine"
