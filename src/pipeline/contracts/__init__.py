"""
Contratos Pydantic para cada stage del pipeline.

Cada stage tiene un Input y Output. La validación en frontera asegura
que un stage solo recibe lo que el stage anterior garantiza.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from domain.strategy.box import Box
from domain.strategy.decision import Action, RiskMode


# ── Stage 1: Ingest ───────────────────────────────────────────────────
class IngestInput(BaseModel):
    symbol: str
    start_iso: str
    end_iso: str
    timeframe: str = "MINUTE_5"


class IngestOutput(BaseModel):
    symbol: str
    df_candles: Any  # pd.DataFrame (no se puede serializar, lo pasamos como objeto)
    n_candles: int


# ── Stage 2: Preprocess ───────────────────────────────────────────────
class PreprocessInput(BaseModel):
    symbol: str
    start_iso: str
    end_iso: str
    box_date: str
    box_start: str = "08:00"
    box_end: str = "09:55"
    market_tz: str = "America/New_York"


class PreprocessOutput(BaseModel):
    symbol: str
    box: Box
    rsi_last: float | None
    volume_profile: dict | None
    box_candles: list[dict]

    @field_validator("box")
    @classmethod
    def validate_box(cls, v: Box) -> Box:
        v.validate()  # regla #1
        return v


# ── Stage 3: Context (macro) ──────────────────────────────────────────
class ContextInput(BaseModel):
    date_str: str
    market_tz: str = "America/New_York"


class ContextOutput(BaseModel):
    macro_risk: Literal["LOW", "MEDIUM", "HIGH"]
    high_impact_events: list[dict]


# ── Stage 4: Signal (breakout) ────────────────────────────────────────
class SignalInput(BaseModel):
    symbol: str
    df_candles: Any  # pd.DataFrame
    box: Box
    primary: bool = False


class SignalOutput(BaseModel):
    symbol: str
    has_breakout: bool
    breakout_state: Literal["ABOVE", "BELOW", "INSIDE", "NONE"] | None
    candle_close: float | None
    signal_time: str | None


# ── Stage 5: Analyze (crew) ───────────────────────────────────────────
class AnalyzeInput(BaseModel):
    symbols: list[dict]  # cada uno con box, signal, contexto
    market: str = "S&P 500"


class DecisionContract(BaseModel):
    symbol: str
    action: Action
    risk: RiskMode
    confidence: int = Field(ge=0, le=100)
    reasons: list[str] = Field(min_length=1)
    key_levels: dict
    signal: dict
    team_consensus: str


class AnalyzeOutput(BaseModel):
    decisions: list[DecisionContract] = Field(min_length=1)


# ── Stage 6: Execute ──────────────────────────────────────────────────
def _today_utc() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).date().isoformat()


class ExecuteInput(BaseModel):
    decision: DecisionContract
    symbol: str
    box: Box
    base_volume: float
    min_rr: float = 1.0
    # Fecha de trading (UTC) usada en el client_order_id idempotente.
    trade_date: str = Field(default_factory=_today_utc)


class OrderContract(BaseModel):
    symbol: str
    side: Literal["BUY", "SELL"]
    volume: float = Field(gt=0)
    entry_price: float
    stop_loss: float
    take_profit: float | None
    is_runner: bool
    decision_id: int | None = None
    broker_order_id: str | None = None


class ExecuteOutput(BaseModel):
    decision_id: int
    orders: list[OrderContract]
    errors: list[str] = []
    # client_order_ids saltados por idempotencia (orden activa ya existente)
    skipped: list[str] = []


# ── Stage 7: Manage (position manager) ────────────────────────────────
class ManageInput(BaseModel):
    open_trades: list[dict]  # de SQLite
    current_prices: dict[str, float]  # symbol -> price


class ManageAction(BaseModel):
    trade_id: int
    action: Literal["MODIFY_SL", "MODIFY_TP", "CLOSE", "HOLD"]
    new_sl: float | None = None
    new_tp: float | None = None
    reason: str


class ManageOutput(BaseModel):
    actions: list[ManageAction]


# ── Run result ─────────────────────────────────────────────────────────
class RunResult(BaseModel):
    run_id: str
    started_at: str
    finished_at: str | None = None
    status: Literal["running", "success", "failed", "skipped"]
    decisions_count: int = 0
    orders_sent: int = 0
    errors: list[str] = []
