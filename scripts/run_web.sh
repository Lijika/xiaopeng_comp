#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
  PIP="$ROOT/.venv/bin/pip"
else
  PY="${PYTHON:-python3}"
  PIP="${PIP:-pip}"
fi

echo "== Task4 Web Demo =="
echo "python: $PY"

# ensure web deps
"$PY" -c "import fastapi,uvicorn" 2>/dev/null || {
  echo "installing fastapi/uvicorn..."
  "$PIP" install 'fastapi>=0.110' 'uvicorn[standard]>=0.27' 'python-multipart>=0.0.9' -q
}

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8765}"
echo "Open http://${HOST}:${PORT}/"
echo "  - 校验演示 / 规则维护 / 实体知识库"
exec "$PY" -m uvicorn task4_consistency.web.app:app --host "$HOST" --port "$PORT" --reload
