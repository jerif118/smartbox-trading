# syntax=docker/dockerfile:1.7
# ─────────────────────────────────────────────────────────────────────────────
# SmartBox Trading — imagen producción
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

# Copia metadata + código para que hatchling encuentre los paquetes
COPY pyproject.toml ./
COPY src ./src

# Instala el paquete + dependencias en un prefijo aislado que copiaremos
# al runtime.
RUN pip install --upgrade pip \
    && pip install --prefix=/install .

# ── Etapa 2: runtime ────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=America/New_York \
    DATA_LOADER_PATH=/app/data_loader \
    VP_LOADER_PATH=/app/data_loader/vp \
    LOG_DIR=/app/logs

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

# Directorios writables para caché de parquets y logs
RUN mkdir -p /app/data_loader/vp /app/logs \
    && chown -R app:app /app

USER app

# Healthcheck: comprueba que los paquetes principales importan
HEALTHCHECK --interval=5m --timeout=10s --start-period=10s --retries=2 \
    CMD python -c "import strategy_ai, preprocess, broker_api, utils, tools_bot" || exit 1

# El comando por defecto ejecuta una corrida única.
# Para programación recurrente usa cron del host, k8s CronJob, GitHub Actions
# scheduled, o un orquestador que invoque `docker run`.
CMD ["python", "-m", "strategy_ai.main"]
