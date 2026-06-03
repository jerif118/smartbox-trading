# SmartBox Trading v2

Bot de trading automatizado para la estrategia de la **caja de apertura** del mercado americano (S&P 500 / NASDAQ / GER40). Combina datos de Capital.com, decisión multi-agente vía CrewAI, y ejecución de órdenes en SimpleFX — con persistencia SQLite, dashboard Streamlit, y soporte multi-provider LLM (OpenAI, Anthropic, Ollama, etc.).

---

## Tabla de contenidos

- [Cambios de v2](#cambios-de-v2)
- [Cómo funciona](#cómo-funciona)
- [Arquitectura](#arquitectura)
- [Requisitos](#requisitos)
- [Instalación](#instalación)
- [Configuración](#configuración)
- [Uso rápido](#uso-rápido)
- [CLI](#cli)
- [Dashboard Streamlit](#dashboard-streamlit)
- [Docker](#docker)
- [Tests](#tests)
- [Multi-provider LLM](#multi-provider-llm)
- [Estructura del proyecto](#estructura-del-proyecto)

---

## Cambios de v2

### Estrategia (preservada 100%)

Las **22 reglas originales** se preservan verbatim. Cada una tiene un test de regresión en `tests/domain/strategy/test_regression.py`.

### Mejoras nuevas

- **5 agentes CrewAI** (antes 3): + `mtfa` (multi-timeframe) + `position_manager` (gestiona trades abiertos: BE en +1R, trailing en +2R)
- **Persistencia SQLite** con 5 tablas: `runs`, `decisions`, `trades`, `agent_events`, `equity_snapshots`
- **Dashboard Streamlit** con 3 secciones: dashboard, histórico, timeline de agentes
- **Multi-provider LLM**: cada agente puede usar un provider/modelo distinto (OpenAI, Anthropic, Google, Mistral, DeepSeek, Groq, Ollama local, LM Studio local, OpenAI-compatible)
- **Arquitectura pipeline + capas**: `domain / application / infrastructure / pipeline / interfaces`
- **Pydantic Settings** tipado con validación
- **Pydantic contracts** en cada stage del pipeline
- **Ruff** para lint + format
- **141+ tests** (22 regresión + 119 nuevos)

---

## Cómo funciona

```
┌──────────────────────────────────────────────────────────────────────┐
│  1. POSITION MANAGER (siempre primero)                               │
│     → Gestiona trades abiertos (BE, trailing, cierre)               │
│     → Trabaja contra SQLite como source of truth                    │
│                                                                      │
│  2. INGEST (paralelo por símbolo)                                    │
│     → Descarga OHLCV desde Capital.com                              │
│     → Caché parquet para no re-descargar                            │
│                                                                      │
│  3. PREPROCESS                                                       │
│     → Box 08:00-09:55 NY (high/low/amplitud)                        │
│     → RSI 14, Volume Profile (POC/VAH/VAL)                          │
│     → Valida regla #1: amplitud > 1% → NO OPERAR                    │
│                                                                      │
│  4. SIGNAL                                                           │
│     → Monitorea 5min post-caja (ventana 2h)                          │
│     → Primer cierre fuera = breakout                                │
│                                                                      │
│  5. CONTEXT (macro)                                                  │
│     → Scrapea calendario económico                                  │
│     → Filtra HIGH impact events                                     │
│                                                                      │
│  6. ANALYZE (CrewAI)                                                 │
│     → mtfa: confirma sesgo HTF (15m/1h/4h)                          │
│     → trader: propone dirección con confluence_score                 │
│     → risk_analyst: valida R:R, drawdown, correlación               │
│     → decision_maker: decisión final estructurada                   │
│                                                                      │
│  7. EXECUTE                                                          │
│     → 2 órdenes por símbolo (primary + runner)                      │
│     → Valida R:R, coherencia, MAX_ORDERS_PER_DAY                    │
│     → Persiste en SQLite ANTES de enviar                            │
│     → Envía a SimpleFX (o simula si DRY_RUN)                        │
│                                                                      │
│  8. EQUITY SNAPSHOT                                                  │
│     → Guarda balance, equity, open positions                        │
└──────────────────────────────────────────────────────────────────────┘
```

---

## Arquitectura

### Capas

```
src/
├── domain/                  # Reglas puras, sin I/O
│   ├── strategy/            # Box, Decision, OrderSpec, Budget
│   ├── indicators/          # RSI, Volume Profile
│   ├── signals/             # Breakout
│   └── context/             # Macro
│
├── application/             # Use cases + ports (interfaces)
│   ├── use_cases/           # Orquestación
│   ├── ports/               # Protocol interfaces
│   └── agents/              # 5 agentes CrewAI
│
├── infrastructure/          # I/O (DB, HTTP, scrapers, LLM)
│   ├── persistence/sqlite/  # DB + repos
│   ├── broker/              # SimpleFX, Capital adapters
│   ├── llm/                 # Multi-provider LLM
│   ├── data_sources/        # Macro scraper, news
│   └── config/              # Settings
│
├── pipeline/                # Stages + orchestrator
│   ├── contracts/           # Pydantic Input/Output por stage
│   └── stages/              # s1_ingest → s7_manage
│
└── interfaces/              # CLI + Streamlit
    ├── cli/                 # python -m interfaces.cli.main
    └── streamlit/           # Dashboard con 3 secciones
```

### Multi-provider LLM

Cada agente puede usar un modelo distinto. Configura en `.env`:

```env
AGENT_DECISION_MAKER_MODEL=openai/gpt-4o-mini
AGENT_TRADER_MODEL=openai/gpt-4o-mini
AGENT_RISK_ANALYST_MODEL=openai/gpt-4o-mini
AGENT_MTFA_MODEL=ollama/llama3.1              # local
AGENT_POSITION_MANAGER_MODEL=anthropic/claude-3-5-sonnet
```

Providers soportados: `openai`, `anthropic`, `google`, `mistral`, `deepseek`, `groq`, `ollama`, `lm_studio`, `openai_compatible`.

---

## Requisitos

- Python 3.12 (o Docker)
- Cuenta en [Capital.com](https://capital.com/) — fuente de datos OHLC
- Cuenta en [SimpleFX](https://simplefx.com/) — broker de ejecución
- API key de al menos un provider LLM (OpenAI por defecto)

---

## Instalación

### Local

```bash
cd agents/strategy_ai
python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"
```

### Con uv (más rápido)

```bash
uv sync
```

---

## Configuración

```bash
cp .env.exmple .env
# Edita .env con tus credenciales
```

Variables mínimas requeridas:

```env
OPENAI_API_KEY=sk-...
EMAIL=...        # Capital.com
PASSWORD=...
API_KEY=...      # Capital.com
ID=...           # SimpleFX
KEY=...          # SimpleFX
SIMPLE_ACCOUNT=...
DRY_RUN=true     # SIEMPRE empieza en true
```

### Multi-provider (opcional)

```env
# Ollama local
OLLAMA_BASE_URL=http://localhost:11434
AGENT_MTFA_MODEL=ollama/llama3.1

# LM Studio local
LM_STUDIO_BASE_URL=http://localhost:1234/v1
# (lm_studio usa OpenAI-compatible)

# Anthropic
ANTHROPIC_API_KEY=sk-ant-...
AGENT_DECISION_MAKER_MODEL=anthropic/claude-3-5-sonnet-20241022

# OpenAI-compatible (vLLM, text-generation-webui, etc.)
OPENAI_COMPATIBLE_BASE_URL=http://localhost:8000/v1
OPENAI_COMPATIBLE_API_KEY=dummy
```

---

## Uso rápido

### 1. Diagnóstico

```bash
python -m interfaces.cli.main doctor
```

Verifica: API keys, DB, providers LLM locales (Ollama, LM Studio).

### 2. Correr el bot (DRY_RUN por defecto)

```bash
python -m interfaces.cli.main run
# o explícito:
python -m interfaces.cli.main run --dry-run
```

### 3. Ver el dashboard

```bash
streamlit run src/interfaces/streamlit/app.py
# Abre http://localhost:8501
```

### 4. Ver status

```bash
python -m interfaces.cli.main status
python -m interfaces.cli.main trades --limit 20
```

---

## CLI

```
python -m interfaces.cli.main <comando>

Comandos:
  run [--dry-run]     Ejecuta el pipeline completo
  doctor              Diagnóstico del sistema
  status              Muestra runs y stats
  trades [--limit N]  Lista últimos N trades
```

---

## Dashboard Streamlit

3 secciones (sidebar):

1. **🏠 Dashboard**: KPIs, equity curve (Plotly), posiciones abiertas, últimos trades, runs recientes
2. **📋 Histórico de trades**: tabla con filtros (símbolo, status, límite), exportar CSV, stats agregadas
3. **🤖 Acciones de agentes**: timeline con todos los eventos de un run (THOUGHT, TOOL_CALL, TOOL_RESULT, MESSAGE, DECISION)

---

## Docker

```bash
# Build
docker compose build

# Bot (corrida única)
docker compose up bot

# Bot + UI (background)
docker compose up -d bot ui

# Solo UI (si bot ya corrió y dejó datos en ./data)
docker compose up -d ui
# → http://localhost:8501
```

Los volúmenes `./data` (DB + parquet cache) y `./logs` se comparten entre bot y UI.

---

## Tests

```bash
# Todos los tests
pytest

# Solo tests de dominio (puros, sin I/O, muy rápidos)
pytest tests/domain

# Solo tests de regresión (las 22 reglas originales)
pytest -m regression

# Con coverage
pytest --cov=src --cov-report=term-missing
```

Estructura:
- `tests/domain/` — 80 tests (puros, sin I/O)
- `tests/infrastructure/` — 27 tests (DB, adapters)
- `tests/application/` — 21 tests (agentes, tools)
- `tests/pipeline/` — 13 tests (stages, contracts)

Total: **141 tests**.

### Lint + format

```bash
ruff check src/
ruff format src/
```

---

## Multi-provider LLM

Tradeoff recomendado:

| Agente | Provider sugerido | Por qué |
|---|---|---|
| `decision_maker` | OpenAI gpt-4o / Anthropic Claude | Necesita razonamiento de alto nivel |
| `trader` | OpenAI gpt-4o-mini / local | Pattern matching, velocidad > capacidad |
| `risk_analyst` | OpenAI / DeepSeek | Análisis cuantitativo |
| `mtfa` | **Ollama local** | Análisis técnico, no necesita nube |
| `position_manager` | OpenAI gpt-4o-mini | Lógica de gestión, baja latencia |

---

## Estructura del proyecto

```
agents/strategy_ai/
├── pyproject.toml
├── Dockerfile
├── docker-compose.yml
├── .env.exmple
├── README.md
├── src/
│   ├── domain/              # Capas puras
│   ├── application/         # Use cases + agents
│   ├── infrastructure/      # DB, broker, LLM
│   ├── pipeline/            # Orchestrator
│   ├── interfaces/          # CLI + UI
│   ├── broker_api/          # (legacy compatible)
│   ├── preprocess/          # (legacy compatible)
│   ├── strategy_ai/         # (legacy compatible)
│   ├── tools_bot/           # (legacy compatible)
│   ├── utils/               # logger, retry
│   └── data_loader/         # parquet cache
├── tests/
│   ├── domain/
│   ├── infrastructure/
│   ├── application/
│   ├── pipeline/
│   └── conftest.py
├── data/                    # SQLite + parquet (gitignored)
└── logs/                    # strategy.log (gitignored)
```

---

## Descargo de responsabilidad

> **ADVERTENCIA**
>
> - El trading de instrumentos financieros conlleva un **alto nivel de riesgo** y puede no ser adecuado para todos los inversores.
> - **Puedes perder parte o la totalidad de tu capital invertido.**
> - Los resultados pasados **no garantizan** resultados futuros.
> - El autor **no es un asesor financiero registrado**.
> - Antes de operar con dinero real, practica con cuenta demo (`SIMPLE_REALITY=DEMO`, `DRY_RUN=true`) durante al menos 1 mes.

---

## Licencia

Ver `LICENSE`.
