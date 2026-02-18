# 📦 SmartBox Trading

Agente de estrategia de la caja que funciona con la apertura del mercado americano automatizado parcialmente con IA para la toma de decisiones de entrada a operativas en long o short

---

## 📋 Tabla de contenidos

- [Cómo funciona](#-cómo-funciona)
- [Requisitos](#-requisitos)
- [Instalación](#-instalación)
- [Configuración](#-configuración)
- [Ejecución](#-ejecución)
- [Ejecución programada](#-ejecución-programada)
- [Estructura del proyecto](#-estructura-del-proyecto)
- [Descargo de responsabilidad](#-descargo-de-responsabilidad)
- [Licencia](#-licencia)

---

## 🧠 Cómo funciona

```
┌─────────────────────────────────────────────────────────────┐
│  1. CAJA (08:00 - 09:55 hora NY)                         │
│     → Calcula high / low / amplitud                         │
│     → Si amplitud > 1% → NO OPERAR                         │
│                                                             │
│  2. MONITOREO (máx 2 horas post-caja)                      │
│     → Velas de 5 min via Capital.com                        │
│     → Detecta primer cierre fuera de la caja                │
│       • Arriba → evaluar LONG                               │
│       • Abajo  → evaluar SHORT                              │
│                                                             │
│  3. IA (CrewAI + GPT)                                       │
│     → Evalúa RSI, Volume Profile, contexto macro            │
│     → Decide: LONG / SHORT / NO_OPERAR                      │
│     → Define riesgo: COMPLETO / MEDIO                       │
│                                                             │
│  4. EJECUCIÓN (SimpleFX)                                    │
│     → Orden 1: 50% volumen con SL + TP                      │
│     → Orden 2: 50% volumen con SL sin TP (runner)           │
└─────────────────────────────────────────────────────────────┘
```

---

## 📌 Requisitos

- **Python** >= 3.10, < 3.14
- Cuenta en [Capital.com](https://capital.com) (API de datos)
- Cuenta en [SimpleFX](https://simplefx.com) (ejecución de órdenes)
- API key de [OpenAI](https://platform.openai.com)

---

## 🛠 Instalación

### 1. Clonar el repositorio

```bash
git clone https://github.com/tu-usuario/smartbox-trading.git
cd smartbox-trading/agents/strategy_ai
```

### 2. Crear entorno virtual

**Mac / Linux:**
```bash
python3 -m venv venv
source venv/bin/activate
```

**Windows:**
```bash
python -m venv venv
venv\Scripts\activate
```

### 3. Instalar dependencias

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Verificar instalación

```bash
python -c "import crewai; import pandas; print('Todo instalado correctamente')"
```

---

## ⚙ Configuración


Edita `.env` con tus datos reales:

```env
# ── IA ────────────────────────────────────────────────
MODEL=gpt-4o-mini
OPENAI_API_KEY=sk-tu-clave-aqui

# ── Símbolos y temporalidad ───────────────────────────
SYMBOLS=US500,US100
TIMEFRAME=MINUTE_5

# ── Caja ──────────────────────────────────────────────
BOX_DATE=
BOX_START=08:00
BOX_END=09:55

# ── Capital.com (datos de mercado) ────────────────────
EMAIL=tu-email@ejemplo.com
PASSWORD=tu-password
API_KEY=tu-api-key-capital

# ── SimpleFX (ejecución de órdenes) ──────────────────
ID=tu-client-id
KEY=tu-client-secret
SIMPLE_ACCOUNT=tu-numero-cuenta
SIMPLE_REALITY=Demo

# ── Trading ───────────────────────────────────────────
VOLUME=1.0
MAX_ORDERS_PER_DAY=4
MAX_DAILY_LOSS=500.0

# ── Seguridad ─────────────────────────────────────────
DRY_RUN=true
LOG_LEVEL=INFO
```


---

## 🚀 Ejecución

### Ejecución manual

```bash
# Activar entorno virtual
source venv/bin/activate        # Mac/Linux
# venv\Scripts\activate         # Windows

# Ejecutar
python -m strategy_ai.main
```
Nota: Ajuste `VOLUME` que es su lotaje según tu capital disponible y el riesgo que desee asumir 

---

## ⏰ Ejecución programada

### Linux / Mac (cron)

Ejecutar de lunes a viernes a las 7:50 AM (hora NY):

```bash
# Abrir crontab
crontab -e

# Agregar esta línea
50 7 * * 1-5 cd /ruta/a/smartbox-trading/agents/strategy_ai && /ruta/a/venv/bin/python -m strategy_ai.main >> /tmp/smartbox.log 2>&1
```

Verificar que se guardó:
```bash
crontab -l
```

### Windows (Task Scheduler)

1. Crear archivo `run_strategy.bat`:

```bat
@echo off
cd /d "C:\ruta\a\smartbox-trading\agents\strategy_ai"
call venv\Scripts\activate
python -m strategy_ai.main >> strategy.log 2>&1
```

2. Abrir **Programador de tareas** (`Win + R` → `taskschd.msc`)
3. **Crear tarea básica:**
   - Nombre: `SmartBox Trading`
   - Desencadenador: Diariamente, 7:50 AM
   - Acción: Iniciar programa → seleccionar `run_strategy.bat`
4. En **Condiciones**: desmarcar "Iniciar solo con AC"
5. En **Configuración**: marcar "Ejecutar tarea lo antes posible si se perdió"

> **Nota:** El bot valida internamente fines de semana y feriados. Si se ejecuta un sábado, se detiene automáticamente.

---

## 📁 Estructura del proyecto

```
smartbox-trading/
└── agents/strategy_ai/
    ├── src/
    │   ├── broker_api/          # Login y órdenes (Capital.com + SimpleFX)
    │   │   ├── login.py
    │   │   ├── api_requests.py
    │   │   └── make_order.py
    │   │
    │   ├── preprocess/          # Pipeline de datos
    │   │   ├── process_pipeline.py   # Caja + RSI + VP
    │   │   └── breakout_monitor.py   # Monitor de breakout post-caja
    │   │
    │   ├── tools_bot/           # Herramientas de análisis
    │   │   ├── box.py               # Estrategia de la caja
    │   │   ├── utils_trading_rsi.py # RSI + divergencias
    │   │   ├── utils_trading_vp.py  # Volume Profile
    │   │   ├── time_now.py          # Conversiones de tiempo
    │   │   └── interval_fecha.py    # Rangos de fechas
    │   │
    │   ├── strategy_ai/         # CrewAI (agentes + tareas)
    │   │   ├── crew.py              # Definición del crew
    │   │   ├── main.py              # Orquestador principal
    │   │   └── config/
    │   │       ├── agents.yaml
    │   │       └── tasks.yaml
    │   │
    │   ├── utils/               # Utilidades
    │   │   ├── logger.py
    │   │   ├── safety.py            # Validaciones de producción
    │   │   └── env_validator.py
    │   │
    │   └── data_loader/         # Caché de datos (parquets)
    │       └── vp/              # Parquets de 1 min para VP
    │
    ├── .env                     # Configuración (no subir a git)
    ├── .env.example             # Plantilla de configuración
    ├── requirements.txt
    ├── pyproject.toml
    └── README.md
```

---

## ⚠ Descargo de responsabilidad

> **ADVERTENCIA**

- El trading de instrumentos financieros conlleva un **alto nivel de riesgo** y puede no ser adecuado para todos los inversores.
- **Puedes perder parte o la totalidad de tu capital invertido.** No inviertas dinero que no puedas permitirte perder.
- Los resultados pasados **no garantizan** resultados futuros.
- El autor de este software **no es un asesor financiero registrado** y no proporciona asesoramiento financiero, de inversión ni de trading.
- **Tú eres el único responsable** de tus decisiones de trading y de cualquier ganancia o pérdida resultante.
- Se recomienda encarecidamente si usted no conoce o no sabe nada acerca sobre el trading y el mercado de valores e indices, no use ni descargue este proyecto.
- Antes de operar con dinero real:
  - Practica con una **cuenta demo** durante al menos 1 mes
  - Comprende completamente la estrategia y sus riesgos
  - Consulta con un **asesor financiero profesional**
  - Establece límites de pérdida que puedas asumir

**Al usar este software, aceptas que lo haces bajo tu propio riesgo y responsabilidad.**

---