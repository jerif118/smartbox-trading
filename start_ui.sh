#!/bin/bash
# SmartBox Trading v2 — Streamlit dashboard launcher
set -e
cd "$(dirname "$0")"
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
exec .venv/bin/streamlit run src/interfaces/streamlit/app.py \
    --server.address=0.0.0.0 \
    --server.port=8501 \
    --server.headless=true \
    --browser.gatherUsageStats=false \
    "$@"
