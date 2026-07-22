# SmartBox Trading v2

> Bot de trading automatizado para índices americanos basado en la estrategia de la "caja de apertura". Combina reglas deterministas con agentes Trader/Risk, ejecuta órdenes en SimpleFX y muestra todo en un panel visual.

---

## Tabla de contenidos

1. [Qué hace](#qué-hace)
2. [Inicio rápido](#inicio-rápido-3-pasos)
3. [Configuración](#configuración)
4. [Uso diario](#uso-diario)
5. [Panel visual (Streamlit)](#panel-visual-streamlit)
6. [Docker](#docker)
7. [Programación automática](#programación-automática)
8. [Cómo funciona por dentro](#cómo-funciona-por-dentro)
9. [Multi-provider LLM](#multi-provider-llm)
10. [Tests y desarrollo](#tests-y-desarrollo)
11. [Solución de problemas](#solución-de-problemas)
12. [Estructura del proyecto](#estructura-del-proyecto)
13. [Advertencia de riesgo](#advertencia-de-riesgo)

---

## Qué hace

El bot corre una vez al día, antes de la apertura de la bolsa de Nueva York. Su flujo:

```
1. Carga datos de ayer y de la apertura (Capital.com)
2. Calcula la "caja" de precios: high/low/amplitud entre 08:00-09:55 NY
3. Si la amplitud > 1%  → no opera
4. Monitorea 2 horas post-caja esperando un breakout
5. Cuando hay breakout vigente, Trader y Risk evalúan el setup:
   - 📊 Trader — score direccional, RSI, VP y multi-timeframe
   - 🛡️ Risk — veta solo reglas duras; incertidumbre moderada reduce tamaño
   - 👑 Desk Manager — consolidación determinista, sin otra llamada LLM
6. Si se aprueba → envía 2 órdenes (primary + runner)
   - primary con TP fijo
   - runner con breakeven en +1R y trailing desde +2R
7. Persiste todo en SQLite para que lo veas en el panel
```

Todo queda registrado en SQLite. El panel te muestra:
- Balance, equity, P&L
- Gráfica de equity en el tiempo
- Tabla de todos los trades con razones
- Timeline de lo que hizo cada agente (sus pensamientos, herramientas, debates)

---

## Inicio rápido (3 pasos)

### 1. Requisitos

- **Python 3.12** ([descargar](https://www.python.org/downloads/)) — o usa Docker
- **Cuenta en [Capital.com](https://capital.com/)** — para datos de mercado
- **Cuenta en [SimpleFX](https://simplefx.com/)** — para ejecutar órdenes
- **API key de un LLM** (OpenAI por defecto)

### 2. Setup automático

Abre una terminal, ve a la carpeta del proyecto, y ejecuta:

```bash
cd /ruta/a/estrategiasp500/agents/strategy_ai
./setup.sh
```

Esto:
- Crea el entorno virtual (limpio, sin conflictos con conda)
- Instala todas las dependencias
- Inicializa la base de datos
- Hace los scripts ejecutables

**Si te da error de permisos:** `chmod +x setup.sh run.sh start_ui.sh`

### 3. Configura tus credenciales

Edita `.env` con tus credenciales:

```bash
nano .env       # o usa tu editor favorito
```

Campos obligatorios:

```env
OPENAI_API_KEY=sk-tu-clave-real       # https://platform.openai.com/api-keys
EMAIL=tu@email.com                     # Capital.com
PASSWORD=tu-password
API_KEY=tu-api-key-capital
ID=tu-client-id-simplefx
KEY=tu-client-secret-simplefx
SIMPLE_ACCOUNT=12345678
```

> ⚠️ **Importante:** Practica primero con `SIMPLE_REALITY=DEMO` y `DRY_RUN=true` durante al menos 1 mes.

### 4. Verifica

```bash
./run.sh doctor
```

Deberías ver:
```
✓ OPENAI_API_KEY OK
✓ 3 símbolo(s) configurado(s): ['US500', 'US100', 'DE40']
✓ Modo: DRY_RUN
✓ DB inicializada
```

---

## Uso diario

### 3 comandos para el día a día

```bash
./run.sh doctor    # verificar que todo está OK
./run.sh run       # correr el bot (envía órdenes si DRY_RUN=false)
./start_ui.sh      # abrir el panel visual en el navegador
```

### Modos de operación

| Modo | Comando | ¿Envía órdenes? |
|---|---|---|
| **Simulación** (recomendado al inicio) | `./run.sh run --dry-run` | No, simula |
| **LIVE con cuenta demo** | `SIMPLE_REALITY=DEMO ./run.sh run` | Sí, a cuenta demo |
| **LIVE con dinero real** | `SIMPLE_REALITY=LIVE ./run.sh run` | ⚠️ Sí, con dinero real |

### Comandos disponibles

```bash
./run.sh doctor                 # diagnóstico
./run.sh run [--dry-run]        # correr el pipeline
./run.sh status                 # stats de runs + trades
./run.sh trades [--limit N]     # lista últimos N trades
./start_ui.sh                   # dashboard en http://localhost:8501
```

---

## Panel visual (Streamlit)

Al correr `./start_ui.sh`, abre tu navegador en **http://localhost:8501**.

3 secciones en el sidebar:

1. **🏠 Dashboard** — Balance, equity, P&L, posiciones abiertas, runs recientes
2. **📋 Histórico de trades** — Tabla filtrable, exportable a CSV, stats agregadas
3. **🤖 Acciones de agentes** — Timeline de lo que hizo cada agente (pensamientos, herramientas, debates)

La auto-refresh es cada 10 segundos, así que puedes dejar el panel abierto y ver en vivo.

---

## Docker

Si prefieres correr todo en contenedores (sin instalar Python localmente):

### Levantar todo (bot + UI) con un comando

```bash
docker compose up -d --build
```

Esto:
1. Construye la imagen
2. Levanta el servicio `bot` (corre el pipeline una vez)
3. Levanta el servicio `ui` (Streamlit en http://localhost:8501)
4. Comparte el volumen `./data` (SQLite + parquet cache) entre ambos

### Comandos útiles

```bash
# Ver logs en vivo
docker compose logs -f

# Solo el bot (corrida única)
docker compose up bot

# Reiniciar la UI sin tocar el bot
docker compose restart ui

# Parar todo
docker compose down

# Parar y limpiar volúmenes (¡BORRA LA DB!)
docker compose down -v
```

### Verificar que todo funciona

```bash
docker compose ps          # ver estado de servicios
docker compose logs bot    # output del bot
curl http://localhost:8501 # UI respondiendo
```

### Notas sobre el Dockerfile

- Multi-stage build: builder con `build-essential`, runtime limpio sin dev tools
- Imagen final ~400MB
- Non-root user (`app`)
- Healthcheck integrado
- Mismo `.env` que se usa en local

---

## Programación automática

Para que el bot corra **todos los días a las 7:50 AM NY** (lunes a viernes):

### Mac/Linux (cron)

```bash
crontab -e
```

Agrega:

```cron
50 7 * * 1-5 cd /ruta/a/estrategiasp500/agents/strategy_ai && ./run.sh run >> logs/cron.log 2>&1
```

### Con Docker (cron del host invocando al container)

```cron
50 7 * * 1-5 cd /ruta/a/estrategiasp500/agents/strategy_ai && docker compose run --rm bot >> logs/cron.log 2>&1
```

### Windows (Task Scheduler)

Crea `run.bat`:

```bat
@echo off
cd /d C:\ruta\al\proyecto\agents\strategy_ai
call run.sh run >> logs\cron.log 2>&1
```

Programa la tarea en **Programador de tareas** de Windows a las 7:50 AM.

---

## Configuración

### Variables de entorno principales

| Variable | Default | Descripción |
|---|---|---|
| `SYMBOLS` | `US500,US100,DE40` | Símbolos a operar (CSV) |
| `PRIMARY_SYMBOL` | `US500` | Símbolo usado como confirmación del resto |
| `BOX_START` | `08:00` | Inicio de la caja (hora NY) |
| `BOX_END` | `09:55` | Fin de la caja |
| `VP_LOOKBACK_DAYS` | `3` | Días de velas a descargar (ventana móvil hasta ahora) |
| `START_VP`/`END_VP` | _(vacíos)_ | Fechas fijas solo para backtest; vacíos = ventana móvil |
| `VOLUME` | `1.0` | Volumen base (se divide 50/50 entre 2 órdenes) |
| `MAX_ORDERS_PER_DAY` | `4` | Hard cap de órdenes por día |
| `MIN_RR_RATIO` | `1.0` | R:R mínimo (los templates producen 1.0) |
| `MIN_CONFIDENCE` | `55` | Confianza mínima final para enviar órdenes |
| `STRATEGY_PROFILE` | `active` | `active` permite setups 55–69 a medio tamaño |
| `ACTIVE_MIN_CONFLUENCE` | `55` | Score mínimo del perfil activo |
| `FULL_RISK_CONFLUENCE` | `70` | Desde este score puede usar tamaño completo |
| `PRIMARY_CONFIRMATION_MODE` | `size_reduction` | Sin US500 reduce tamaño; también admite `required`/`independent` |
| `SIGNAL_MAX_AGE_MINUTES` | `15` | Descarta el primer breakout cuando ya quedó obsoleto |
| `MACRO_BLACKOUT_MINUTES` | `15` | Veto antes/después de evento HIGH inmediato |
| `MACRO_CAUTION_MINUTES` | `120` | Ventana de medio tamaño alrededor de eventos HIGH |
| `MAX_CORRELATED_SETUPS` | `1` | Evita duplicar exposición US500/US100 en el mismo run |
| `DRY_RUN` | `true` | Si `true`, NO envía órdenes |
| `SIMPLE_REALITY` | `DEMO` | `DEMO` o `LIVE` |
| `DB_PATH` | `./data/smartbox.db` | Path de la base de datos |
| `STAGE_TIMEOUT_S` | `300` | Timeout (s) por stage de datos/broker |
| `RUN_MODE` | `monitor` | `monitor` reintenta dentro de la ventana; `once` corre una sola vez |
| `OPERATE_START`/`OPERATE_END` | `10:00`/`12:00` | Ventana local del mercado para esperar ruptura post-caja |
| `MONITOR_INTERVAL_S` | `300` | Segundos entre corridas dentro de la ventana |
| `ANALYZE_TIMEOUT_S` | `1800` | Timeout (s) del crew de agentes (stage 5) |

### Persistencia

| Path | Qué guarda |
|---|---|
| `./data/smartbox.db` | SQLite (trades, decisiones, runs, eventos, métricas por stage) |
| `./data/parquet/` | Cache de velas OHLCV (no re-descarga) |
| `./logs/strategy.log` | Log rotativo (5MB x 5 archivos), con secretos redactados |

La tabla `stage_metrics` registra duración y resultado (`ok | error | timeout`)
de cada stage en cada run — útil para ver qué parte del pipeline falla o tarda.
El schema se versiona con `PRAGMA user_version`; las migraciones se aplican
solas al arrancar (no hace falta borrar la DB al actualizar).

### Calendario macro público

El bot combina tres fuentes oficiales sin API key:

- BLS ICS: CPI, PPI, empleo, JOLTS y comunicados laborales.
- BEA JSON: GDP, PCE, comercio y otras publicaciones económicas.
- Federal Reserve: día de decisión del FOMC a las 14:00 New York.

El resultado diferencia `OK`, `DEGRADED` y `UNAVAILABLE`. Una respuesta vacía
válida significa que no hay eventos; una caída de red nunca se interpreta como
riesgo bajo. `HIGH` se reserva para el blackout inmediato y `MEDIUM` reduce el
tamaño durante la ventana de cautela.

### Múltiples modelos de IA (uno por agente)

```env
AGENT_DECISION_MAKER_MODEL=openai/gpt-4o
AGENT_TRADER_MODEL=openai/gpt-4o-mini
AGENT_RISK_ANALYST_MODEL=deepseek/deepseek-chat
AGENT_MTFA_MODEL=ollama/llama3.1
AGENT_POSITION_MANAGER_MODEL=openai/gpt-4o-mini
```

Providers soportados: `openai`, `anthropic`, `google`, `mistral`, `deepseek`, `groq`, `ollama` (local), `lm_studio` (local), `openai_compatible`.

---

## Cómo funciona por dentro

### Arquitectura

```
src/
├── domain/                  # Reglas puras, sin I/O
│   ├── strategy/            # Box, Decision, OrderSpec, Budget
│   ├── indicators/          # RSI, Volume Profile
│   ├── signals/             # Breakout
│   └── context/             # Macro events
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
│   └── data_sources/        # Macro scraper, news
│
├── pipeline/                # Stages + orchestrator
│   ├── contracts/           # Pydantic Input/Output por stage
│   └── stages/              # s1_ingest → s7_manage
│
└── interfaces/              # CLI + Streamlit
    ├── cli/                 # python -m interfaces.cli.main
    └── streamlit/           # Dashboard con 3 secciones
```

### Pipeline (7 stages)

```
s7_manage (gestiona trades abiertos)
    ↓
s1_ingest → s2_preprocess → s4_signal (paralelo por símbolo)
    ↓
s3_context (macro)
    ↓
s5_analyze (crew AI)
    ↓
s6_execute (envía órdenes + persiste)
    ↓
equity_snapshot
```

### Las 22 reglas originales preservadas

Cada regla de la estrategia original está codificada en `domain/` con tests de regresión:

| # | Regla | Archivo |
|---|---|---|
| 1 | Amplitud > 1% → NO OPERAR | `domain/strategy/box.py` |
| 2 | Box high = max, low = min | `domain/strategy/box.py` |
| 3 | BoxPair prefiere SimpleFX | `domain/strategy/box.py` |
| 4 | RSI 14 períodos | `domain/indicators/rsi.py` |
| 5 | Volume Profile 70% | `domain/indicators/volume_profile.py` |
| 6 | Breakout 5min, ventana 2h | `domain/signals/breakout.py` |
| 7 | Gate on primary breakout | `pipeline/orchestrator.py` |
| 8 | Dirección consistente con breakout | `domain/strategy/decision.py` |
| 9 | R:R mínimo | `domain/strategy/order_spec.py` |
| 10 | 2 órdenes (primary + runner) | `domain/strategy/order_spec.py` |
| 11 | MAX_ORDERS_PER_DAY=4 | `domain/strategy/budget.py` |
| 12 | DRY_RUN flag | `infrastructure/config/settings.py` |
| 13-14 | Templates LONG/SHORT | `domain/strategy/order_spec.py` |
| 15 | Coherencia de niveles | `domain/strategy/order_spec.py` |
| 16 | Volume split | `domain/strategy/position_sizer.py` |
| 17 | risk_pts > 0 | `domain/strategy/order_spec.py` |
| 18 | MIN_CONFIDENCE | `domain/strategy/decision.py` |
| 19 | Multi-símbolo paralelo | `pipeline/orchestrator.py` |
| 20 | Macro HIGH impact filter | `domain/context/macro.py` |
| 21-22 | 3 agentes base, box candles limit | `application/agents/`, `preprocess/` |

---

## Multi-provider LLM

Puedes mezclar proveedores y modelos. Cada agente puede usar uno distinto.

| Agente | Provider recomendado | Por qué |
|---|---|---|
| `decision_maker` | `openai/gpt-4o` o `anthropic/claude-3-5-sonnet` | Razonamiento de alto nivel |
| `trader` | `openai/gpt-4o-mini` o local | Pattern matching, velocidad > capacidad |
| `risk_analyst` | `openai/gpt-4o-mini` o `deepseek/deepseek-chat` | Análisis cuantitativo |
| `mtfa` | `ollama/llama3.1` (local) | Análisis técnico, no necesita nube |
| `position_manager` | `openai/gpt-4o-mini` | Lógica de gestión, baja latencia |

### Ollama (modelos locales)

```bash
# Instalar Ollama (https://ollama.com)
curl -fsSL https://ollama.com/install.sh | sh

# Bajar un modelo
ollama pull llama3.1

# Configurar en .env
AGENT_MTFA_MODEL=ollama/llama3.1
```

### LM Studio (otro local)

1. Descarga LM Studio desde https://lmstudio.ai
2. Carga un modelo (Qwen, Mistral, etc.)
3. Inicia el servidor local
4. Configura: `AGENT_MTFA_MODEL=lm_studio/qwen2.5-7b-instruct`

---

## Tests y desarrollo

### Correr tests

```bash
# Todos (294+ tests)
./.venv/bin/python -m pytest tests/

# Solo dominio (puros, sin I/O, ~0.3s)
./.venv/bin/python -m pytest tests/domain/

# Solo regresión (las 22 reglas originales)
./.venv/bin/python -m pytest -m regression

# Con coverage
./.venv/bin/python -m pytest --cov=src --cov-report=term-missing
```

### Lint y format

```bash
./.venv/bin/python -m pip install ruff
./.venv/bin/ruff check src/
./.venv/bin/ruff format src/
```

### Estructura de tests

```
tests/
├── domain/          # 83 tests puros (Box, RSI, VP, Decision, OrderSpec, market_time)
├── infrastructure/  # 36 tests (SQLite, migraciones, adapters)
├── application/     # 21 tests (agentes, tools)
├── pipeline/        # 40 tests (stages, contracts, runner, orchestrator, idempotencia)
├── interfaces/      # 8 tests (CLI)
└── utils/           # 11 tests (retry, redacción de secretos)
```

### Dependencias de desarrollo

```bash
pip install -e ".[dev]"
```

Incluye: `pytest`, `pytest-asyncio`, `respx`, `freezegun`, `ruff`.

---

## Solución de problemas

### "OPENAI_API_KEY no configurada"

Tu `.env` no tiene la API key real. Consigue una en https://platform.openai.com/api-keys y edita `.env`:

```env
OPENAI_API_KEY=sk-...   # debe empezar con sk-
```

### "ModuleNotFoundError: No module named 'interfaces'"

Tu venv está corrupto (común con conda). Solución rápida: usa los scripts wrapper.

```bash
./run.sh doctor    # en lugar de "python -m interfaces.cli.main"
```

Si quieres arreglar el venv:

```bash
./setup.sh         # recrea el venv desde cero
```

### "401 Incorrect API key"

La API key de OpenAI no es válida. Verifica en https://platform.openai.com/api-keys que esté activa y cópiala de nuevo (sin espacios al inicio/final).

### "error.not-found.epic" (Capital.com)

El símbolo no existe como epic en Capital.com (p.ej. el DAX es `DE40`, no `GER40`;
el bot ya convierte `GER40` → `DE40` automáticamente). Si te pasa con otro símbolo,
búscalo con el endpoint `/markets?searchTerm=...` y usa ese epic en `SYMBOLS`.

### "No module named 'strategy_ai'" (durante build de Docker)

Probablemente `pip install` falló. Reconstruye sin cache:

```bash
docker compose build --no-cache
```

### El bot corre pero no encuentra breakouts

**Es normal.** Solo opera cuando hay una señal clara. La mayoría de los días no habrá operación. La estrategia es selectiva a propósito.

### El panel no abre en el navegador

```bash
# Verifica que el puerto 8501 está libre
lsof -i :8501

# Si hay conflicto, usa otro puerto
./start_ui.sh --server.port=8502
```

### "Connection refused" a Ollama

Ollama no está corriendo. Inícialo:

```bash
ollama serve
```

---

## Estructura del proyecto

```
agents/strategy_ai/
├── setup.sh              ← Correr UNA VEZ para instalar
├── run.sh                ← Correr el bot
├── start_ui.sh           ← Abrir el panel
├── pyproject.toml
├── Dockerfile
├── docker-compose.yml
├── .env                  ← TUS credenciales
├── README.md             ← Este archivo
│
├── src/
│   ├── domain/           # Reglas puras (+ market_time)
│   ├── application/      # Use cases + 5 agentes
│   ├── infrastructure/   # SQLite (+ migraciones), brokers, LLM
│   ├── pipeline/         # 7 stages + runner (timeouts/métricas) + errors
│   ├── interfaces/       # CLI + Streamlit
│   ├── utils/            # logger (redacción de secretos), retry
│   └── data_loader/      # parquet cache
│
├── legacy/               # Código v1, fuera del paquete (borrar tras validar v2)
│
├── tests/                # 199 tests
│   ├── domain/           # 83 tests
│   ├── infrastructure/   # 36 tests
│   ├── application/      # 21 tests
│   ├── pipeline/         # 40 tests
│   ├── interfaces/       # 8 tests
│   └── utils/            # 11 tests
│
├── docs/AUDITORIA.md     # Informe de auditoría técnica
├── data/                 # SQLite + parquet (gitignored)
└── logs/                 # strategy.log (gitignored)
```

---

## Cambios de v2 (vs. versión original)

| Área | Antes | Ahora |
|---|---|---|
| Agentes | 3 | 5 (+ multi-timeframe, + position manager) |
| Persistencia | Logs only | SQLite con 5 tablas |
| UI | No había | Streamlit con 3 secciones |
| LLM | Solo OpenAI | Multi-provider simultáneo |
| Arquitectura | Planos | Pipeline + capas (domain/app/infra) |
| Config | dict | Pydantic Settings tipado |
| Contratos | Dict libre | Pydantic en cada stage |
| Tests | 0 | 199 (22 regresión) |
| Lint | Sin config | Ruff configurado |
| Idempotencia | No (re-run duplicaba órdenes) | `client_order_id` + índice único |
| Timeouts | No (crew podía colgar el run) | Por stage, configurables |
| Observabilidad | Logs | Logs + `stage_metrics` por run |

---

## Advertencia de riesgo

> ⚠️ **El trading de instrumentos financieros conlleva un alto nivel de riesgo.**
>
> - Puedes perder parte o la totalidad de tu capital invertido.
> - Los resultados pasados no garantizan resultados futuros.
> - El autor de este software no es un asesor financiero registrado.
> - Tú eres el único responsable de tus decisiones de trading.
>
> **Antes de operar con dinero real:**
> 1. Practica con `SIMPLE_REALITY=DEMO` y `DRY_RUN=true` durante al menos 1 mes.
> 2. Empieza con volúmenes muy pequeños (0.01 - 0.1).
> 3. Entiende completamente la estrategia y sus riesgos.
> 4. Consulta con un asesor financiero profesional.
> 5. Establece límites de pérdida que puedas asumir.
>
> Al usar este software, aceptas que lo haces bajo tu propio riesgo y responsabilidad.

---

## Licencia

Ver `LICENSE`.
