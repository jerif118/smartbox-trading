# syntax=docker/dockerfile:1.7
# ─────────────────────────────────────────────────────────────────────────────
# SmartBox Trading v2 — imagen producción
#   build:  docker build -t smartbox-trading .
#   run :   docker compose up
# ─────────────────────────────────────────────────────────────────────────────

# ── Etapa 1: builder ────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copia metadata + código
COPY pyproject.toml ./
COPY src ./src

# Instala el paquete (no editable, modo producción)
RUN pip install --upgrade pip \
    && pip install --prefix=/install .

# ── Etapa 2: runtime ────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=America/New_York \
    DATA_LOADER_PATH=/app/data/parquet \
    VP_LOADER_PATH=/app/data/parquet/vp \
    LOG_DIR=/app/logs \
    DB_PATH=/app/data/smartbox.db

RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata \
        ca-certificates \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# Usuario no-root
RUN useradd --create-home --shell /bin/bash app
WORKDIR /app

# Copia paquetes instalados desde builder
COPY --from=builder /install /usr/local

# Copia el código fuente (necesario para streamlit run src/...)
COPY --chown=app:app src ./src

# Directorios writables para DB, parquet cache, logs
RUN mkdir -p /app/data/parquet/vp /app/logs \
    && chown -R app:app /app

USER app

# Healthcheck — verifica que el módulo principal importa y doctor funciona
HEALTHCHECK --interval=5m --timeout=30s --start-period=30s --retries=3 \
    CMD python -c "from interfaces.cli.main import main; main(['doctor'])" || exit 1

# Comando por defecto: doctor (para que el container no falle si se arranca solo)
CMD ["python", "-m", "interfaces.cli.main", "doctor"]
