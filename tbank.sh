#!/usr/bin/env bash
# Короткий вызов бота: ./tbank.sh status, ./tbank.sh run, ./tbank.sh report ...
# Ищет venv рядом с собой, падает с подсказкой, если его нет.
set -euo pipefail
cd "$(dirname "$0")"
if [ -x ".venv/bin/python" ]; then
    exec .venv/bin/python cli.py "$@"
elif command -v python3 >/dev/null 2>&1; then
    exec python3 cli.py "$@"
else
    echo "Не найден .venv/bin/python — создайте: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi
