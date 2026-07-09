"""
Configuración centralizada con Pydantic Settings.

Carga todas las variables de entorno en un objeto tipado `Settings` con
validación automática. Soporta multi-provider LLM (OpenAI, Anthropic,
Google, Mistral, DeepSeek, Groq, Ollama, LM Studio, OpenAI-compatible).
"""

from __future__ import annotations

import os
from typing import Literal

from dotenv import load_dotenv
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv()

Provider = Literal[
    "openai",
    "anthropic",
    "google",
    "mistral",
    "deepseek",
    "groq",
    "ollama",
    "lm_studio",
    "openai_compatible",
]


SUPPORTED_PROVIDERS: tuple[str, ...] = (
    "openai",
    "anthropic",
    "google",
    "mistral",
    "deepseek",
    "groq",
    "ollama",
    "lm_studio",
    "openai_compatible",
)


class LLMSettings(BaseSettings):
    """Configuración de los 5 agentes con modelos multi-provider."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    decision_maker: str = Field(
        default="openai/gpt-4o-mini", alias="AGENT_DECISION_MAKER_MODEL"
    )
    trader: str = Field(default="openai/gpt-4o-mini", alias="AGENT_TRADER_MODEL")
    risk_analyst: str = Field(default="openai/gpt-4o-mini", alias="AGENT_RISK_ANALYST_MODEL")
    mtfa: str = Field(default="openai/gpt-4o-mini", alias="AGENT_MTFA_MODEL")
    position_manager: str = Field(
        default="openai/gpt-4o-mini", alias="AGENT_POSITION_MANAGER_MODEL"
    )

    ollama_base_url: str = Field(
        default="http://localhost:11434", alias="OLLAMA_BASE_URL"
    )
    lm_studio_base_url: str = Field(
        default="http://localhost:1234/v1", alias="LM_STUDIO_BASE_URL"
    )
    openai_compatible_base_url: str = Field(
        default="", alias="OPENAI_COMPATIBLE_BASE_URL"
    )
    # Sin default "dummy": si se configura base_url sin key, validate_credentials
    # lo reporta en vez de mandar una key falsa a un servidor desconocido.
    openai_compatible_api_key: str = Field(
        default="", alias="OPENAI_COMPATIBLE_API_KEY"
    )

    @field_validator(
        "decision_maker",
        "trader",
        "risk_analyst",
        "mtfa",
        "position_manager",
    )
    @classmethod
    def validate_model_format(cls, v: str) -> str:
        if "/" not in v:
            raise ValueError(
                f"Model must be in 'provider/model' format, got: {v!r}. "
                f"Supported providers: {SUPPORTED_PROVIDERS}"
            )
        provider, _model = v.split("/", 1)
        if provider not in SUPPORTED_PROVIDERS:
            raise ValueError(
                f"Unknown provider {provider!r} in {v!r}. "
                f"Supported: {SUPPORTED_PROVIDERS}"
            )
        return v

    def for_agent(self, agent_name: str) -> str:
        return getattr(self, agent_name)


class Settings(BaseSettings):
    """Configuración global del sistema."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── IA / LLM ──────────────────────────────────────────────────────────
    llm: LLMSettings = Field(default_factory=LLMSettings)

    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    google_api_key: str = Field(default="", alias="GOOGLE_API_KEY")
    mistral_api_key: str = Field(default="", alias="MISTRAL_API_KEY")
    deepseek_api_key: str = Field(default="", alias="DEEPSEEK_API_KEY")
    groq_api_key: str = Field(default="", alias="GROQ_API_KEY")

    # ── Mercado ───────────────────────────────────────────────────────────
    symbols: str = Field(default="US500", alias="SYMBOLS")
    primary_symbol: str = Field(default="US500", alias="PRIMARY_SYMBOL")
    timeframe: str = Field(default="MINUTE_5", alias="TIMEFRAME")
    market: str = Field(default="S&P 500 / Indices", alias="MARKET")
    timezone: str = Field(default="America/New_York", alias="TIMEZONE")
    market_tz: str = Field(default="America/New_York", alias="MARKET_TZ")

    # ── Caja ──────────────────────────────────────────────────────────────
    box_date: str = Field(default="", alias="BOX_DATE")
    box_start: str = Field(default="13:00", alias="BOX_START")
    box_end: str = Field(default="14:55", alias="BOX_END")

    # ── Volume Profile / ventana de datos ─────────────────────────────────
    # START_VP/END_VP fijos solo para backtests; vacíos → ventana dinámica
    # de los últimos VP_LOOKBACK_DAYS días hasta ahora.
    start_vp: str = Field(default="", alias="START_VP")
    end_vp: str = Field(default="", alias="END_VP")
    vp_lookback_days: int = Field(default=3, alias="VP_LOOKBACK_DAYS", ge=1, le=30)

    # ── Capital.com ───────────────────────────────────────────────────────
    email: str = Field(default="", alias="EMAIL")
    password: str = Field(default="", alias="PASSWORD")
    api_key: str = Field(default="", alias="API_KEY")

    # ── SimpleFX ──────────────────────────────────────────────────────────
    sf_id: str = Field(default="", alias="ID")
    sf_key: str = Field(default="", alias="KEY")
    simple_account: str = Field(default="", alias="SIMPLE_ACCOUNT")
    simple_reality: str = Field(default="DEMO", alias="SIMPLE_REALITY")

    # ── Trading ───────────────────────────────────────────────────────────
    volume: float = Field(default=1.0, alias="VOLUME")
    max_orders_per_day: int = Field(default=4, alias="MAX_ORDERS_PER_DAY")
    min_rr_ratio: float = Field(default=1.0, alias="MIN_RR_RATIO")
    min_confidence: int = Field(default=0, alias="MIN_CONFIDENCE", ge=0, le=100)
    max_daily_loss: float = Field(default=500.0, alias="MAX_DAILY_LOSS")

    # ── Seguridad ─────────────────────────────────────────────────────────
    dry_run: bool = Field(default=True, alias="DRY_RUN")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # ── Timeouts del pipeline (segundos) ──────────────────────────────────
    # stage_timeout_s aplica a stages de datos/broker; analyze_timeout_s al
    # crew de agentes (stage 5), que legítimamente tarda mucho más.
    stage_timeout_s: int = Field(default=300, alias="STAGE_TIMEOUT_S", ge=10)
    analyze_timeout_s: int = Field(default=1800, alias="ANALYZE_TIMEOUT_S", ge=60)

    # ── Modo de ejecución y ventana operativa ─────────────────────────────
    # once    → corre el pipeline una vez y termina (one-shot, cron externo).
    # monitor → se mantiene vivo y monitorea dentro de la ventana operativa.
    run_mode: Literal["once", "monitor"] = Field(default="once", alias="RUN_MODE")
    # Ventana operativa (hora local de market_tz). Por defecto coincide con la
    # ventana de breakout (2h posteriores al cierre de la caja 08:00–09:55).
    operate_start: str = Field(default="10:00", alias="OPERATE_START")
    operate_end: str = Field(default="12:00", alias="OPERATE_END")
    # Cada cuánto vuelve a correr el pipeline dentro de la ventana (segundos).
    # El monitor se alinea al cierre de vela: corre en el próximo múltiplo de
    # este intervalo + monitor_grace_s (no cada N segundos desde el arranque).
    monitor_interval_s: int = Field(default=300, alias="MONITOR_INTERVAL_S", ge=10)
    # Margen tras el cierre de vela antes de correr (deja llegar la vela al REST).
    monitor_grace_s: float = Field(default=5.0, alias="MONITOR_GRACE_S", ge=0)
    # WebSocket de Capital.com como gatillo del monitor: corre el pipeline en
    # cuanto cierra una vela. Si el socket cae, el monitor sigue por reloj.
    stream_enabled: bool = Field(default=True, alias="STREAM_ENABLED")

    # ── Persistencia ──────────────────────────────────────────────────────
    db_path: str = Field(default="./data/smartbox.db", alias="DB_PATH")
    data_loader_path: str = Field(default="./data/parquet", alias="DATA_LOADER_PATH")
    vp_loader_path: str = Field(default="./data/parquet/vp", alias="VP_LOADER_PATH")
    log_dir: str = Field(default="./logs", alias="LOG_DIR")

    # ── API key opcional para news ────────────────────────────────────────
    news_api_key: str = Field(default="", alias="NEWS_API_KEY")

    @field_validator("dry_run", mode="before")
    @classmethod
    def parse_dry_run(cls, v: object) -> bool:
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in {"1", "true", "yes", "y"}

    @field_validator("min_confidence")
    @classmethod
    def check_confidence_range(cls, v: int) -> int:
        if not 0 <= v <= 100:
            raise ValueError(f"MIN_CONFIDENCE must be 0-100, got {v}")
        return v

    @field_validator("volume")
    @classmethod
    def check_volume_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"VOLUME must be > 0, got {v}")
        return v

    @field_validator("min_rr_ratio")
    @classmethod
    def check_rr_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"MIN_RR_RATIO must be > 0, got {v}")
        return v

    @property
    def symbol_list(self) -> list[str]:
        """Símbolos activos, filtrados contra el whitelist (US500/US100).

        Cualquier símbolo extra (DE40, etc.) en SYMBOLS se ignora y loguea.
        Si tras filtrar no queda ninguno, cae al whitelist completo para no
        dejar el bot sin nada que analizar.
        """
        from domain.symbols import ALLOWED_SYMBOLS, filter_symbols

        raw = [s.strip() for s in self.symbols.split(",") if s.strip()]
        filtered = filter_symbols(raw, context="settings.SYMBOLS")
        return filtered or list(ALLOWED_SYMBOLS)

    @property
    def primary_symbol_resolved(self) -> str:
        """Símbolo primario garantizado dentro de los símbolos activos."""
        from domain.symbols import resolve_primary

        return resolve_primary(self.primary_symbol, self.symbol_list)

    def vp_window(self) -> tuple[str, str]:
        """Ventana de velas (UTC, ISO sin tz) para ingest/VP.

        Si START_VP y END_VP están definidos se respetan (modo backtest);
        si no, ventana móvil que siempre incluye el día actual.
        """
        if self.start_vp and self.end_vp:
            return self.start_vp, self.end_vp
        from datetime import UTC, datetime, timedelta

        end = datetime.now(UTC)
        start = end - timedelta(days=self.vp_lookback_days)
        fmt = "%Y-%m-%dT%H:%M:%S"
        return start.strftime(fmt), end.strftime(fmt)

    @property
    def is_demo(self) -> bool:
        return self.simple_reality.upper() == "DEMO"

    def validate_credentials(self) -> list[str]:
        """Chequeos de credenciales que Pydantic no puede expresar por campo.

        Retorna lista de problemas (vacía = OK). Lo invoca env_validator
        antes de correr el bot.
        """
        problems: list[str] = []

        agent_models = (
            self.llm.decision_maker,
            self.llm.trader,
            self.llm.risk_analyst,
            self.llm.mtfa,
            self.llm.position_manager,
        )
        providers = {m.split("/", 1)[0] for m in agent_models}
        if "openai_compatible" in providers:
            if not self.llm.openai_compatible_base_url:
                problems.append(
                    "OPENAI_COMPATIBLE_BASE_URL vacío pero hay agentes con provider openai_compatible"
                )
            if not self.llm.openai_compatible_api_key:
                problems.append(
                    "OPENAI_COMPATIBLE_API_KEY vacío pero hay agentes con provider openai_compatible"
                )

        if self.simple_reality.upper() not in ("DEMO", "LIVE"):
            problems.append(f"SIMPLE_REALITY inválido: {self.simple_reality!r} (DEMO o LIVE)")

        if not self.dry_run:
            missing = [
                name
                for name, val in (
                    ("ID", self.sf_id),
                    ("KEY", self.sf_key),
                    ("SIMPLE_ACCOUNT", self.simple_account),
                )
                if not val.strip()
            ]
            if missing:
                problems.append(
                    f"modo LIVE (DRY_RUN=false) requiere credenciales SimpleFX: faltan {missing}"
                )

        return problems


_settings_singleton: Settings | None = None


def get_settings() -> Settings:
    """Singleton lazy de Settings (testeable con monkeypatch de env)."""
    global _settings_singleton
    if _settings_singleton is None:
        _settings_singleton = Settings()
    return _settings_singleton


def reset_settings_cache() -> None:
    """Para tests: resetea el singleton."""
    global _settings_singleton
    _settings_singleton = None


REQUIRED_FOR_LIVE: tuple[str, ...] = (
    "OPENAI_API_KEY",
    "EMAIL",
    "PASSWORD",
    "API_KEY",
    "ID",
    "KEY",
    "SIMPLE_ACCOUNT",
)


def has_openai_key() -> bool:
    """Detecta si hay API key real (no placeholder)."""
    placeholder_values = {
        "",
        "123456",
        "your-key-here",
        "changeme",
        "xxx",
        "placeholder",
        "sk-tu-clave-openai-aqui",
    }
    val = os.getenv("OPENAI_API_KEY", "").strip()
    if not val or val.lower() in placeholder_values:
        return False
    return val.startswith("sk-")
