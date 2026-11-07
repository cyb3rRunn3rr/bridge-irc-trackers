#!/bin/sh
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
    python3 -m venv .venv || { echo 'Falha ao criar ambiente Python' >&2; exit 1; }
    .venv/bin/python -m pip install -r requirements.txt || { echo 'Falha ao instalar dependências' >&2; exit 1; }
fi
exec .venv/bin/python irc_bridge.py "$@"
