# syntax=docker/dockerfile:1.7
# ─────────────────────────────────────────────────────────────────────────────
# SmartBox Trading v2 — imagen producción
#   build:  docker build -t smartbox-trading .
#   run :   docker run --rm --env-file .env smartbox-trading
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
COPY tests ./tests

# Instala paquete + dependencias en prefijo aislado
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

# Copia paquetes ya instalados desde builder
COPY --from=builder /install /usr/local

# Directorios writables
RUN mkdir -p /app/data/parquet/vp /app/logs \
    && chown -R app:app /app

USER app

# Healthcheck
HEALTHCHECK --interval=5m --timeout=10s --start-period=10s --retries=2 \
    CMD python -c "from interfaces.cli.main import main; main(['doctor'])" || exit 1

# Comando por defecto: doctor (para verificar entorno)
CMD ["python", "-m", "interfaces.cli.main", "doctor"]
