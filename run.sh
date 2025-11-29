#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

source .venv/bin/activate

if [ -f requirements.txt ]; then
  python -m pip install -r requirements.txt >/dev/null
fi

if [ "${1:-}" = "test" ]; then
  FAKE_OPENAI=1 python main.py --selftest
  exit $?
fi

python main.py
