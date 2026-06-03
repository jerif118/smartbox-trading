"""
Dataclasses para repos. Modelan las filas de la DB con tipos Python.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class TradeStatus(str, Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    CLOSED_TP = "CLOSED_TP"
    CLOSED_SL = "CLOSED_SL"
    CLOSED_MANUAL = "CLOSED_MANUAL"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


class RunStatus(str, Enum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class AgentEventType(str, Enum):
    THOUGHT = "THOUGHT"
    TOOL_CALL = "TOOL_CALL"
    TOOL_RESULT = "TOOL_RESULT"
    MESSAGE = "MESSAGE"
    DECISION = "DECISION"
    SYSTEM = "SYSTEM"


@dataclass
class Trade:
    id: int | None = None
    run_id: str = ""
    decision_id: int | None = None
    ts_open: str = ""
    symbol: str = ""
    side: str = "BUY"
    volume: float = 0.0
    entry_price: float = 0.0
    stop_loss: float | None = None
    take_profit: float | None = None
    is_runner: bool = False
    broker_order_id: str | None = None
    status: str = TradeStatus.PENDING.value
    ts_close: str | None = None
    exit_price: float | None = None
    pnl: float | None = None
    r_multiple: float | None = None
    reason: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None or k in {"is_runner", "status"}}


@dataclass
class Decision:
    id: int | None = None
    run_id: str = ""
    ts: str = ""
    symbol: str = ""
    action: str = "NO_OPERAR"
    risk: str | None = None
    confidence: int | None = None
    reasons: str = "[]"
    team_consensus: str | None = None
    crew_raw_output: str | None = None
    key_levels: str | None = None
    signal: str | None = None
    created_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class AgentEvent:
    id: int | None = None
    run_id: str = ""
    ts: str = ""
    agent: str = ""
    event_type: str = AgentEventType.SYSTEM.value
    payload: str | None = None
    duration_ms: int | None = None
    created_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class EquitySnapshot:
    id: int | None = None
    ts: str = ""
    balance: float = 0.0
    equity: float = 0.0
    daily_pnl: float | None = None
    total_pnl: float | None = None
    open_positions: int = 0
    source: str | None = None
    run_id: str | None = None
    created_at: str | None = None


@dataclass
class Run:
    id: str
    started_at: str
    finished_at: str | None = None
    status: str = RunStatus.RUNNING.value
    error: str | None = None
    config_snapshot: str | None = None
    created_at: str | None = None
