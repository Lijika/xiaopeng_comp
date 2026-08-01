#!/usr/bin/env bash
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

echo "== Task4 demo (Round6) =="
echo "python: $PY"
mkdir -p out/demo

echo "-- [1/4] consistent app → JSON + MD + HTML --"
"$PY" -m task4_consistency.cli check \
  fixtures/applications/app_consistent_01.json \
  -c configs/rules_auto_lease.yaml \
  -o out/demo/report_ok.json \
  --markdown out/demo/report_ok.md \
  --html out/demo/report_ok.html

echo "-- [2/4] inconsistent VIN hard-negative → JSON + MD + HTML --"
"$PY" -m task4_consistency.cli check \
  fixtures/applications/app_inconsistent_vin.json \
  -c configs/rules_auto_lease.yaml \
  -o out/demo/report_bad_vin.json \
  --markdown out/demo/report_bad_vin.md \
  --html out/demo/report_bad_vin.html

echo "-- [3/4] ADV brand JV → JSON + HTML --"
"$PY" -m task4_consistency.cli check \
  fixtures/applications/app_atk_brand_jv.json \
  -c configs/rules_auto_lease.yaml \
  -o out/demo/report_atk_brand.json \
  --html out/demo/report_atk_brand.html

echo "-- [4/4] evaluate fixtures --"
"$PY" -m task4_consistency.cli evaluate \
  fixtures/applications \
  -c configs/rules_auto_lease.yaml \
  -o out/metrics.json

echo "-- attack probes --"
"$PY" scripts/attack_probes.py

echo "-- pytest --"
"$PYTEST" -q

echo ""
echo "DONE. Artifacts:"
ls -la out/demo/ out/metrics.json 2>/dev/null | sed 's/^/  /'
echo "  metrics: out/metrics.json"
