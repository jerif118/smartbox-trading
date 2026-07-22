"""
Contratos Pydantic para cada stage del pipeline.

Cada stage tiene un Input y Output. La validación en frontera asegura
que un stage solo recibe lo que el stage anterior garantiza.
"""

from __future__ import annotations

from typing import Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator

from domain.strategy.box import Box
from domain.strategy.decision import Action, RiskMode


# ── Stage 1: Ingest ───────────────────────────────────────────────────
class IngestInput(BaseModel):
    symbol: str
    start_iso: str
    end_iso: str
    timeframe: str = "MINUTE_5"


class IngestOutput(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    symbol: str
    df_candles: pd.DataFrame  # no serializable; se pasa como objeto
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
    box: Box  # caja Capital.com — referencia (ruptura, gate de amplitud)
    # Caja SimpleFX — broker de ejecución. None si el feed no devolvió velas;
    # en ese caso la ejecución cae de vuelta a la caja Capital (BoxPair).
    box_simple: Box | None = None
    rsi_last: float | None
    volume_profile: dict | None
    box_candles: list[dict]

    @field_validator("box")
    @classmethod
    def validate_box(cls, v: Box) -> Box:
        if v.high is None or v.low is None:
            raise ValueError(f"Box sin niveles (high={v.high}, low={v.low})")
        if v.high <= v.low:
            raise ValueError(f"Box inválida: high ({v.high}) <= low ({v.low})")
        if v.amplitude_pct is None:
            raise ValueError("Box sin amplitud calculada")
        if v.n_candles <= 0:
            raise ValueError(f"Box con {v.n_candles} velas")
        return v


# ── Stage 3: Context (macro) ──────────────────────────────────────────
class ContextInput(BaseModel):
    date_str: str
    market_tz: str = "America/New_York"
    reference_time: str | None = None


class ContextOutput(BaseModel):
    macro_risk: Literal["LOW", "MEDIUM", "HIGH"]
    high_impact_events: list[dict]
    provider_status: Literal["OK", "DEGRADED", "UNAVAILABLE"] = "OK"
    providers_ok: list[str] = Field(default_factory=list)
    providers_failed: list[str] = Field(default_factory=list)
    nearest_event_minutes: float | None = None


# ── Stage 4: Signal (breakout) ────────────────────────────────────────
class SignalInput(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    symbol: str
    df_candles: pd.DataFrame
    box: Box
    primary: bool = False
    max_age_minutes: int = Field(default=15, ge=1)


class SignalOutput(BaseModel):
    symbol: str
    has_breakout: bool
    breakout_state: Literal["ABOVE", "BELOW", "INSIDE", "NONE"] | None
    candle_close: float | None
    signal_time: str | None
    signal_age_minutes: float | None = None


# ── Stage 5: Analyze (crew) ───────────────────────────────────────────
class BreakoutSignalData(BaseModel):
    state: Literal["ABOVE", "BELOW", "INSIDE", "NONE"]
    close: float | None = None
    time: str | None = None


class BoxLevelsData(BaseModel):
    high: float
    low: float
    mid: float
    amp_pct: float


class SymbolCrewData(BaseModel):
    """Datos de un símbolo con breakout, listos para el crew."""

    symbol: str
    is_primary: bool = False
    primary_confirmed: bool = True
    market_tz: str = "America/New_York"
    breakout_signal: BreakoutSignalData
    caja: BoxLevelsData
    vp: dict = Field(default_factory=dict)
    rsi: dict = Field(default_factory=dict)
    macro: dict = Field(default_factory=dict)


class AnalyzeInput(BaseModel):
    symbols: list[SymbolCrewData] = Field(min_length=1)
    market: str = "S&P 500"


# ── Outputs estructurados del crew (por símbolo) ──────────────────────
ProposedDirection = Literal["LONG", "SHORT", "NO_OPERAR"]

# Estados de riesgo explícitos. Se elimina "APPROVE" genérico/ambiguo.
RiskDecision = Literal[
    "APPROVE_TRADE",  # operar en la dirección propuesta
    "APPROVE_NO_TRADE",  # aprobado NO operar (el trader propuso NO_OPERAR)
    "MODIFY",  # operar pero con ajustes (niveles/tamaño)
    "NEED_DATA",  # faltan datos críticos → NO_OPERAR
    "VETO",  # regla dura violada → NO_OPERAR
]


class TraderAssessment(BaseModel):
    """Salida estructurada del Trader de un símbolo. Conserva TODO el contexto."""

    symbol: str
    proposed_direction: ProposedDirection
    # El score autoritativo se recalcula/inyecta determinista en s5_analyze; el
    # LLM no necesita devolverlo (default 0 y se sobrescribe).
    confluence_score: int = Field(default=0, ge=0, le=100)
    confidence: int = Field(default=0, ge=0, le=100)
    rsi: float | None = None
    vah: float | None = None
    val: float | None = None
    poc: float | None = None
    breakout_state: str | None = None
    macro_risk: str | None = None
    macro_provider_status: str | None = None
    mtf_alignment: str | None = None
    reasons: list[str] = Field(default_factory=list)


class RiskAssessment(BaseModel):
    """Salida estructurada del Risk de un símbolo."""

    symbol: str
    risk_decision: RiskDecision
    rr_ratio: float | None = None
    reasons: list[str] = Field(default_factory=list)
    # Niveles ajustados cuando risk_decision == MODIFY (opcional).
    suggested_stop_loss: float | None = None
    suggested_take_profit: float | None = None


class SymbolResult(BaseModel):
    """Resultado completo de una rama por símbolo: trader + risk."""

    symbol: str
    trader: TraderAssessment
    risk: RiskAssessment


class DecisionContract(BaseModel):
    symbol: str
    action: Action
    risk: RiskMode
    confidence: int = Field(ge=0, le=100)
    reasons: list[str] = Field(min_length=1)
    # key_levels/signal quedan como dict deliberadamente: son output del LLM
    # (con fallback) y su estructura interna no es contractual.
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
class OpenTradeContract(BaseModel):
    """Fila de trade abierto desde SQLite. extra='allow' tolera columnas nuevas."""

    model_config = ConfigDict(extra="allow")

    id: int
    symbol: str
    side: Literal["BUY", "SELL"]
    entry_price: float
    stop_loss: float | None = None
    initial_stop_loss: float | None = None
    take_profit: float | None = None
    is_runner: bool = False
    broker_order_id: str | None = None
    max_favorable_price: float | None = None


class ManageInput(BaseModel):
    open_trades: list[OpenTradeContract]
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
    # partial = terminó pero con errores no fatales (algún símbolo/orden falló)
    status: Literal["running", "success", "partial", "failed", "skipped"]
    decisions_count: int = 0
    orders_sent: int = 0
    errors: list[str] = []
