# SmartBox Trading

Agente automatizado para la estrategia de la **caja de apertura** del mercado americano (S&P 500 / NASDAQ). Combina datos de Capital.com, decisión multi-agente vía CrewAI y ejecución de órdenes en SimpleFX.

---

## Tabla de contenidos

- [Cómo funciona](#cómo-funciona)
- [Requisitos](#requisitos)
- [Instalación local](#instalación-local)
- [Configuración](#configuración)
- [Ejecución](#ejecución)
- [Docker](#docker)
- [Ejecución programada](#ejecución-programada)
- [Estructura del proyecto](#estructura-del-proyecto)
- [Variables de entorno](#variables-de-entorno)
- [Descargo de responsabilidad](#descargo-de-responsabilidad)
- [Licencia](#licencia)

---

## Cómo funciona

```
┌─────────────────────────────────────────────────────────────┐
│  1. CAJA (ventana configurable, p.ej. 08:00 - 09:55 NY)     │
│     → Calcula high / low / amplitud                         │
│     → Si amplitud > 1% → NO OPERAR                          │
│                                                             │
│  2. MONITOREO (máx 2 horas post-caja)                       │
│     → Velas de 5 min via Capital.com                        │
│     → Detecta primer cierre fuera de la caja                │
│       • Arriba → evaluar LONG                               │
│       • Abajo  → evaluar SHORT                              │
│                                                             │
│  3. IA (CrewAI jerárquico, 3 agentes)                       │
│     → decision_maker, trader, risk_analyst                  │
│     → Evalúa RSI, Volume Profile, contexto macro            │
│     → Decide: LONG / SHORT / NO_OPERAR                      │
│     → Define riesgo: COMPLETO / MEDIO                       │
│                                                             │
│  4. EJECUCIÓN (SimpleFX)                                    │
│     → Validación cruzada: dirección IA == dirección breakout│
│     → R:R >= MIN_RR_RATIO (default 1.5)                     │
│     → Orden 1: 50% volumen con SL + TP                      │
│     → Orden 2: 50% volumen con SL sin TP (runner)           │
│     → DRY_RUN=true loguea sin enviar                        │
└─────────────────────────────────────────────────────────────┘
```

---

## Requisitos

- **Python** 3.12 (o **Docker** + Docker Compose)
- Cuenta en [Capital.com](https://capital.com/) — fuente de datos OHLC
- Cuenta en [SimpleFX](https://simplefx.com/) — broker de ejecución
- API key de [OpenAI](https://platform.openai.com/) — modelo LLM

---

## Instalación local

### 1. Clonar el repositorio

```bash
git clone https://github.com/tu-usuario/smartbox-trading.git
cd smartbox-trading/agents/strategy_ai
```

### 2. Crear entorno virtual

**Mac / Linux:**
```bash
python3.12 -m venv .venv
source .venv/bin/activate
```

**Windows (PowerShell):**
```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
```

### 3. Instalar el paquete en modo editable

> **Importante:** el proyecto usa un layout `src/` con varios paquetes top-level (`broker_api`, `preprocess`, `strategy_ai`, `tools_bot`, `utils`). Instalarlo con `pip install -e .` registra los módulos en el `PYTHONPATH` del intérprete — **sin esto los imports fallan** y `python -m strategy_ai.main` no encuentra `utils.logger`.

```bash
pip install --upgrade pip
pip install -e .
```

### 4. Verificar instalación

```bash
python -c "import strategy_ai, preprocess, broker_api, utils, tools_bot; print('OK')"
```

---

## Configuración

Copia la plantilla y edítala con tus credenciales:

```bash
cp .env.exmple .env   # nota: el archivo plantilla se llama .env.exmple en este repo
```

### Mínimo necesario

```env
# ── IA ──────────────────────────────────────────────
MODEL=gpt-4o-mini
OPENAI_API_KEY=sk-...

# ── Símbolos y temporalidad ─────────────────────────
SYMBOLS=US500,US100
PRIMARY_SYMBOL=US500
TIMEFRAME=MINUTE_5

# ── Caja (UTC) ──────────────────────────────────────
# Consulta la hora real de apertura del mercado destino.
BOX_DATE=
BOX_START=13:00
BOX_END=14:55

# ── Volume Profile ──────────────────────────────────
# El rango DEBE contener la ventana de la caja.
START_VP=2026-05-25T00:00:00
END_VP=2026-06-02T14:55:00

# ── Capital.com (datos) ─────────────────────────────
EMAIL=...
PASSWORD=...
API_KEY=...

# ── SimpleFX (ejecución) ────────────────────────────
ID=...
KEY=...
SIMPLE_ACCOUNT=...
SIMPLE_REALITY=DEMO       # DEMO | LIVE

# ── Trading ─────────────────────────────────────────
VOLUME=1.0
MAX_ORDERS_PER_DAY=4
MIN_RR_RATIO=1.5
MIN_CONFIDENCE=0

# ── Seguridad ───────────────────────────────────────
DRY_RUN=true              # true = simula sin enviar al broker
LOG_LEVEL=INFO
```

> El bot **respeta `DRY_RUN=true`** y calcula todo sin enviar órdenes. Úsalo siempre la primera vez.

---

## Ejecución

Con el venv activo:

```bash
# Forma estándar (módulo)
python -m strategy_ai.main

# Equivalente vía script instalado por pyproject
strategy_ai
```

Otras entradas registradas en `pyproject.toml`:

| Comando | Descripción |
|---|---|
| `strategy_ai` / `run_crew` | Corrida única (= `main:run`) |
| `train <N> <file>` | Entrena el crew N iteraciones |
| `replay <task_id>` | Re-ejecuta una tarea concreta |
| `test <N> <eval_llm>` | Test del crew |
| `run_with_trigger '<json>'` | Ejecuta con payload externo |

---

## Docker

### Build y corrida única

```bash
docker build -t smartbox-trading .
docker run --rm --env-file .env \
  -v "$(pwd)/src/data_loader:/app/data_loader" \
  -v "$(pwd)/logs:/app/logs" \
  smartbox-trading
```

### Con Docker Compose (recomendado)

```bash
docker compose up --build
```

El `docker-compose.yml` monta los volúmenes de caché de parquets (`data_loader/`) y `logs/` para que la información persista entre corridas y para no descargar de cero los datos de Volume Profile cada día.

### Variables clave en el contenedor

El `Dockerfile` ya fija:

```
TZ=America/New_York
DATA_LOADER_PATH=/app/data_loader
VP_LOADER_PATH=/app/data_loader/vp
LOG_DIR=/app/logs
```

Si necesitas otra TZ, pásala con `docker run -e TZ=...` o sobreescríbela en `docker-compose.yml`.

---

## Ejecución programada

El contenedor (o el script local) hace **una sola corrida** y termina; la recurrencia se delega al sistema.

### Linux / Mac — cron del host invocando Docker

```bash
crontab -e
```

```cron
# Lunes a viernes, 7:50 AM hora NY → Docker Compose
50 7 * * 1-5 cd /ruta/al/proyecto/agents/strategy_ai && /usr/bin/docker compose run --rm bot >> /tmp/smartbox.log 2>&1
```

### Linux / Mac — cron sin Docker

```cron
50 7 * * 1-5 cd /ruta/al/proyecto/agents/strategy_ai && /ruta/al/.venv/bin/python -m strategy_ai.main >> /tmp/smartbox.log 2>&1
```

### Windows — Task Scheduler

Crear `run_strategy.bat`:

```bat
@echo off
cd /d "C:\ruta\al\proyecto\agents\strategy_ai"
call .venv\Scripts\activate
python -m strategy_ai.main >> logs\strategy.log 2>&1
```

Luego en **Programador de tareas** crear una tarea diaria a las 7:50 AM apuntando al `.bat`.

> El bot detecta fines de semana internamente (`interval_fecha.is_trading_day`) y aborta limpio.

---

## Estructura del proyecto

```
agents/strategy_ai/
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml              # configuración de paquete + scripts
├── requirements.txt            # mirror de dependencias (instalación pip puro)
├── .env.exmple                 # plantilla — copiar a .env
├── src/
│   ├── broker_api/             # Login y órdenes (Capital + SimpleFX)
│   │   ├── login.py
│   │   ├── api_requests.py
│   │   └── make_order.py
│   ├── preprocess/             # Pipeline de datos
│   │   ├── process_pipeline.py # Caja + RSI + VP + caché parquet
│   │   └── breakout_monitor.py # Monitor post-caja (live/histórico)
│   ├── tools_bot/              # Cálculos puros
│   │   ├── box.py
│   │   ├── utils_trading_rsi.py
│   │   ├── utils_trading_vp.py
│   │   ├── time_now.py
│   │   ├── interval_fecha.py
│   │   └── standar_data.py
│   ├── strategy_ai/            # CrewAI
│   │   ├── main.py             # orquestador
│   │   ├── crew.py             # 3 agentes + after_kickoff (órdenes)
│   │   ├── config/
│   │   │   ├── agents.yaml
│   │   │   └── tasks.yaml
│   │   └── tools/              # tools del crew (scraping, summarize, analyze)
│   ├── utils/
│   │   ├── logger.py
│   │   ├── safety/env_validator.py
│   │   └── retry.py
│   └── data_loader/            # caché de parquets (montable como volumen)
│       └── vp/
├── tests/
└── logs/
```

---

## Variables de entorno

### Requeridas (validadas al arranque)

| Variable | Descripción |
|---|---|
| `EMAIL`, `PASSWORD`, `API_KEY` | Credenciales Capital.com |
| `ID`, `KEY`, `SIMPLE_ACCOUNT` | Credenciales SimpleFX |
| `OPENAI_API_KEY` | API key OpenAI |
| `SYMBOLS` | Lista CSV de instrumentos (`US500,US100`) |
| `TIMEFRAME` | Resolución Capital (`MINUTE_5`, `MINUTE_15`, …) |
| `START_VP`, `END_VP` | Rango ISO del Volume Profile (debe contener la caja) |

### Opcionales con default

| Variable | Default | Descripción |
|---|---|---|
| `PRIMARY_SYMBOL` | `US500` | Símbolo de referencia; el bot espera su breakout antes del veredicto final |
| `BOX_START`, `BOX_END` | `08:00`, `09:55` | Ventana de la caja (UTC, según .env) |
| `BOX_DATE` | hoy | Fecha de la caja `YYYY-MM-DD` |
| `MARKET_TZ` | `America/New_York` | TZ del mercado |
| `VOLUME` | `1.0` | Volumen base; se divide 50/50 entre las dos órdenes |
| `MAX_ORDERS_PER_DAY` | `4` | Hard cap de órdenes enviadas en una corrida |
| `MIN_RR_RATIO` | `1.5` | R:R mínimo para no descartar la operación |
| `MIN_CONFIDENCE` | `0` | Confianza mínima del crew (0–100) |
| `SIMPLE_REALITY` | `Demo` | `DEMO` o `LIVE` |
| `DRY_RUN` | `false` | Si `true`, **no** se envían órdenes al broker |
| `LOG_LEVEL` | `INFO` | Nivel de logging |
| `LOG_DIR` | `./logs` (cwd) | Dónde escribir `strategy.log` rotativo. Si no es escribible, se usa solo stdout. |
| `DATA_LOADER_PATH` | `src/data_loader` | Dónde escribir parquets (útil para volúmenes Docker) |
| `VP_LOADER_PATH` | `${DATA_LOADER_PATH}/vp` | Parquets de 1-min para Volume Profile |

---

## Descargo de responsabilidad

> **ADVERTENCIA**

- El trading de instrumentos financieros conlleva un **alto nivel de riesgo** y puede no ser adecuado para todos los inversores.
- **Puedes perder parte o la totalidad de tu capital invertido.**
- Los resultados pasados **no garantizan** resultados futuros.
- El autor **no es un asesor financiero registrado** y no proporciona asesoramiento de inversión.
- **Tú eres el único responsable** de tus decisiones de trading.
- Antes de operar con dinero real:
  - Practica con una **cuenta demo** durante al menos 1 mes (`SIMPLE_REALITY=DEMO`, `DRY_RUN=true`).
  - Comprende completamente la estrategia y sus riesgos.
  - Consulta con un **asesor financiero profesional**.
  - Establece límites de pérdida que puedas asumir.

**Al usar este software, aceptas que lo haces bajo tu propio riesgo y responsabilidad.**

---

## Licencia

Ver [`LICENSE`](LICENSE).
