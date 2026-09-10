#!/usr/bin/env bash
# One-click launcher for the codelet web GUI (macOS / Linux).
#   ./run-web.sh [--port 9000] [--no-open]
#   PYTHON=/path/to/python ./run-web.sh      # force an interpreter
# Starts the server and opens your browser automatically.
set -euo pipefail

# Run from the repo so .env (LLM config) and .codelet/ (settings, skills) load.
cd "$(dirname "$0")"

# Probe for uvicorn/fastapi, NOT just `import codelet`: the repo root is on
# sys.path when cwd is the project, so ANY python "imports codelet" here -- a
# false green light that then dies on the missing web extras.
has_web() { "$1" -c "import uvicorn, fastapi, codelet" >/dev/null 2>&1; }

candidates=()
[ -n "${PYTHON:-}" ] && candidates+=("$PYTHON")
[ -n "${CONDA_PREFIX:-}" ] && candidates+=("$CONDA_PREFIX/bin/python")
[ -n "${HOME:-}" ] && candidates+=("$HOME/.conda/envs/codelet/bin/python")
candidates+=(python3 python)

PY=""
tried=()
for c in "${candidates[@]}"; do
  command -v "$c" >/dev/null 2>&1 || [ -x "$c" ] || continue
  tried+=("$c")
  if has_web "$c"; then PY="$c"; break; fi
done

if [ -z "$PY" ]; then
  echo "No Python with the codelet web extras was found." >&2
  [ ${#tried[@]} -gt 0 ] && printf 'Tried:\n%s\n' "$(printf '    %s\n' "${tried[@]}")" >&2
  echo >&2
  echo "Fix it with either:" >&2
  echo "    conda activate codelet && pip install -e \".[web]\"" >&2
  echo "    PYTHON=/path/to/env/bin/python ./run-web.sh" >&2
  exit 1
fi

echo "codelet: using $PY"
exec "$PY" -m codelet.web "$@"
