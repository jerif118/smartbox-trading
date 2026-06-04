#!/bin/bash
# SmartBox Trading v2 — CLI runner
# Funciona incluso si el editable install está roto (conda + venv híbrido).
set -e
cd "$(dirname "$0")"
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
exec .venv/bin/python -m interfaces.cli.main "$@"
