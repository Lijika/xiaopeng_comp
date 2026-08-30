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

echo "== Task4 放款审核 =="
echo "python: $PY"

# ensure web deps
"$PY" -c "import fastapi,uvicorn" 2>/dev/null || {
  echo "installing fastapi/uvicorn..."
  "$PIP" install 'fastapi>=0.110' 'uvicorn[standard]>=0.27' 'python-multipart>=0.0.9' -q
}

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8765}"

# Local demo needs a persistent S01 authority.  Keep an explicit deployment
# path when supplied; otherwise place the demo ledger under the repository's
# existing output directory.
if [[ -z "${TASK4_S01_STATE_PATH:-}" ]]; then
  export TASK4_S01_STATE_PATH="$ROOT/out/s01.sqlite3"
fi
if [[ "$TASK4_S01_STATE_PATH" != /* ]]; then
  echo "TASK4_S01_STATE_PATH must be an absolute path" >&2
  exit 2
fi
mkdir -p "$(dirname "$TASK4_S01_STATE_PATH")"

echo "Open http://${HOST}:${PORT}/"
echo "  - 第 1 步：上传 材料/task4_applications 中的 JSON → 开始核验"
echo "  - 顶部按 1–12 编号走完核验、复核、规则、放款后收尾"
echo "  - 岗位在页面右上角切换，不必改命令行"
if [[ "${TASK4_WEB_MODE:-full}" == "basic" ]]; then
  echo "mode: basic business demo"
  exec "$PY" -m uvicorn task4_consistency.web.app:app --host "$HOST" --port "$PORT" --reload
fi
if [[ -z "${TASK4_FULL_DEMO_ROOT:-}" ]]; then
  export TASK4_FULL_DEMO_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/task4-full-demo.XXXXXX")"
fi
echo "mode: full local exhibit"
echo "state: $TASK4_FULL_DEMO_ROOT"
exec "$PY" -m uvicorn task4_consistency.web.full_demo:create_app --factory --host "$HOST" --port "$PORT"
