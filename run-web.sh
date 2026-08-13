#!/usr/bin/env bash
# One-click launcher for the codelet web GUI (macOS / Linux).
#   ./run-web.sh  [--port 9000]
# Starts the server and opens your browser automatically.
set -euo pipefail
PY="${PYTHON:-python3}"
if ! "$PY" -c "import codelet" 2>/dev/null; then
  echo "codelet isn't importable by '$PY'."
  echo "Activate your env first, or install it:  pip install -e \".[web]\""
  exit 1
fi
exec "$PY" -m codelet.web "$@"
