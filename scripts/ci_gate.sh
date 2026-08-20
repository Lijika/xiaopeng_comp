#!/usr/bin/env bash
# Round21 CI gate: pytest + evaluate main + web/kb attacks + identity probes.
# Non-zero exit on any failure.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
  PYTEST="$ROOT/.venv/bin/pytest"
else
  PY="${PYTHON:-python3}"
  PYTEST="${PYTEST:-pytest}"
fi

mkdir -p out
echo "=== CI GATE (Task4) ==="
echo "python: $PY"
echo "cwd: $ROOT"

fail=0

run() {
  local title="$1"
  shift
  echo ""
  echo "-- $title --"
  if "$@"; then
    echo "OK: $title"
  else
    echo "FAIL: $title (exit $?)" >&2
    fail=1
  fi
}

run "pytest" "$PYTEST" -q
# C-DEV-REG: the fixture evaluator is development regression evidence only;
# formal acceptance wording is reserved for immutable S12 bundles.
run "evaluate suite=main (C-DEV-REG)" "$PY" -m task4_consistency evaluate \
  -c configs/rules_auto_lease.yaml \
  --suite main \
  -o out/metrics_main.json
run "attack_web_kb (W1/W2/K*)" "$PY" scripts/attack_web_kb.py
run "attack_probes (identity)" "$PY" scripts/attack_probes.py
run "smoke_web (TestClient health+check)" "$PY" scripts/smoke_web.py

# bench: refresh + optional 2x regression warn (does not fail gate unless >50ms target)
echo ""
echo "-- bench --"
if "$PY" scripts/bench.py --check-regression; then
  echo "OK: bench"
else
  rc=$?
  if [[ $rc -eq 3 ]]; then
    echo "WARN: bench regression >2x (see out/bench.json); not failing gate" >&2
  else
    echo "FAIL: bench (exit $rc)" >&2
    fail=1
  fi
fi

echo ""
if [[ $fail -ne 0 ]]; then
  echo "=== CI GATE FAIL ===" >&2
  exit 1
fi
echo "=== CI GATE PASS ==="
exit 0
