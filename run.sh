#!/usr/bin/env bash
# Always use the project venv (Homebrew python3 often lacks Tk).
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -x .venv/bin/python ]]; then
  echo "Creating .venv with python3.13..."
  if ! command -v python3.13 >/dev/null 2>&1; then
    echo "Install: brew install python@3.13 python-tk@3.13"
    exit 1
  fi
  python3.13 -m venv .venv
  .venv/bin/pip install -r requirements.txt
fi

if ! .venv/bin/python -c "import tkinter" 2>/dev/null; then
  echo "This venv has no Tk. Fix with:"
  echo "  brew install python-tk@3.13"
  echo "  rm -rf .venv && python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi

exec .venv/bin/python main.py "$@"
