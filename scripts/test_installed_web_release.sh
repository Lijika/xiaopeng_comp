#!/usr/bin/env bash
# T10 installed-release qualification harness (Issue #44).
#
# Single temporary-root release gate for the installed FastAPI Web release:
#  1. SHA256 manifest over source inputs (configs/ fixtures/ data/)
#  2. npm run build (production React build -> task4_consistency/web/static/react/)
#  3. PEP 517 sdist + wheel via `python -m build` (default isolated flow:
#     build dependencies come from pyproject.toml [build-system].requires)
#  4. assert exactly one .tar.gz and one .whl
#  5. pip install --no-deps --target <tmp>/site
#  6. copy configs/ fixtures/ data/ into the installed root (the app derives
#     ROOT from __file__, so the installed copy is the runtime authority)
#  7. provenance probe: task4_consistency.__file__ lives under <tmp>/site and
#     the installed React shell / hashed assets / legacy static / templates
#     exist with no sourcemaps
#  8. focused release pytest (tests/test_t10_release.py + three T01 shell/cache
#     contracts) with PYTHONSAFEPATH=1 and PYTHONPATH=<site>:<repo>
#  9. real Python-only uvicorn from the installed package, PID/argv logged,
#     /api/health probed
# 10. `npm run test:e2e -- --list` collection count (recorded; expected
#     "66 tests in 12 files")
# 11. full Playwright matrix from the same installed-import environment (specs
#     spawn their own uvicorn children)
# 12. preserve the run's Playwright artifacts under $LOG_DIR
# 13. re-verify the source-input manifest, then the EXIT trap removes the
#     temporary root and stops the server
#
# All evidence goes to $LOG_DIR (default /tmp/codex/ticket-44-kimi-evidence)
# as lane-c-harness-*.log; Playwright artifacts are copied alongside.
# Any failure exits non-zero with the failing step visible in the log.
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

LOG_DIR="${LOG_DIR:-/tmp/codex/ticket-44-kimi-evidence}"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/lane-c-harness-$(date +%Y%m%d-%H%M%S).log"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/t10-installed-release.XXXXXX")"
SERVER_PID=""

step() { printf '\n== %s ==\n' "$1" | tee -a "$LOG"; }

cleanup() {
  if [[ -n "$SERVER_PID" ]]; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  if [[ -n "$TMP" && "$TMP" == /tmp/* ]]; then
    rm -rf "$TMP"
  fi
}
trap cleanup EXIT

echo "== T10 installed Web release qualification ==" | tee -a "$LOG"
echo "root: $ROOT" | tee -a "$LOG"
echo "tmp:  $TMP" | tee -a "$LOG"
echo "log:  $LOG" | tee -a "$LOG"
"$PY" --version 2>&1 | tee -a "$LOG"
node --version 2>&1 | tee -a "$LOG"
npm --version 2>&1 | tee -a "$LOG"

# 1. Source-input manifest: deterministic (sorted) SHA256 over configs/,
#    fixtures/ and data/. Relative paths, verified later from $ROOT.
step "1/13 source-input SHA256 manifest (configs fixtures data)"
find configs fixtures data -type f -print0 | sort -z | xargs -0 sha256sum > "$TMP/source-input.sha256"
wc -l "$TMP/source-input.sha256" | tee -a "$LOG"

# 2. Production React build. Runs typecheck + check:generated + vite build and
#    writes into task4_consistency/web/static/react/ (the only repo mutation
#    this harness performs; byte-identical when frontend sources are unchanged).
#    PYTHONPATH must NOT be exported here: check:generated spawns
#    .venv/bin/python and would fail while <site> does not exist yet.
step "2/13 npm run build (production React build)"
npm run build 2>&1 | tee -a "$LOG"

# 3. PEP 517 sdist + wheel. The default isolated flow (`python -m build`
#    without --no-isolation) reads pyproject.toml [build-system].requires
#    (setuptools>=68, wheel) and provisions them in a temporary build
#    environment, so the runtime venv needs no build-dependency mutation.
step "3/13 python -m build (isolated PEP 517 sdist + wheel)"
mkdir -p "$TMP/dist"
"$PY" -m build --outdir "$TMP/dist" 2>&1 | tee -a "$LOG"

# 4. Exactly one sdist and one wheel; list the real files otherwise.
step "4/13 assert exactly one .tar.gz and one .whl"
mapfile -t SDISTS < <(ls "$TMP"/dist/*.tar.gz 2>/dev/null || true)
mapfile -t WHEELS < <(ls "$TMP"/dist/*.whl 2>/dev/null || true)
if [[ "${#SDISTS[@]}" -ne 1 || "${#WHEELS[@]}" -ne 1 ]]; then
  echo "ERROR: expected exactly one .tar.gz and one .whl in $TMP/dist" | tee -a "$LOG"
  ls -la "$TMP/dist" | tee -a "$LOG"
  exit 1
fi
SDIST="${SDISTS[0]}"
WHEEL="${WHEELS[0]}"
echo "sdist: $SDIST" | tee -a "$LOG"
echo "wheel: $WHEEL" | tee -a "$LOG"

# 5. Install the wheel into the temporary site root without dependencies.
step "5/13 pip install --no-deps --target $TMP/site"
mkdir -p "$TMP/site"
"$PIP" install --no-deps --target "$TMP/site" "$WHEEL" 2>&1 | tee -a "$LOG"

# 6. Runtime input copies: the installed app derives ROOT from __file__, so
#    configs/ fixtures/ data/ must live under the installed root. SQLite
#    authority directory for the uvicorn evidence server.
step "6/13 copy operator inputs into installed root"
cp -a configs fixtures data "$TMP/site/"
mkdir -p "$TMP/site/var"

# 7. Provenance probe: installed import + React shell/hashed assets/legacy
#    static/templates present, no sourcemaps.
step "7/13 provenance probe against installed root"
env PYTHONSAFEPATH=1 PYTHONPATH="$TMP/site:$ROOT" TASK4_T10_INSTALLED_ROOT="$TMP/site" \
  "$PY" -P - <<'PY' 2>&1 | tee -a "$LOG"
import os
import re
from pathlib import Path

import task4_consistency

site = Path(os.environ["TASK4_T10_INSTALLED_ROOT"]).resolve()
pkg_file = Path(task4_consistency.__file__).resolve()
assert pkg_file.is_relative_to(site), (
    f"task4_consistency not imported from installed root: {pkg_file}"
)
print(f"TASK4_T10_INSTALLED_ROOT={site}")
print(f"task4_consistency.__file__={pkg_file}")

web = site / "task4_consistency" / "web"
index = web / "static" / "react" / "index.html"
assert index.is_file(), f"installed React index missing: {index}"
html = index.read_text(encoding="utf-8")
refs = sorted(set(re.findall(r"/static/react/assets/[A-Za-z0-9._/-]+\.(?:js|css)", html)))
assert refs, f"no hashed asset references in {index}"
for ref in refs:
    asset = web / "static" / "react" / ref.removeprefix("/static/react/")
    assert asset.is_file(), f"installed hashed asset missing: {asset}"
print(f"installed React shell references {len(refs)} hashed assets")

legacy = web / "static" / "app.js"
assert legacy.is_file(), f"legacy static app.js missing: {legacy}"
for name in ("index.html", "s01.html", "s02.html"):
    assert (web / "templates" / name).is_file(), f"installed template missing: {web / 'templates' / name}"
maps = sorted((web / "static" / "react").rglob("*.map"))
assert not maps, f"production build must not ship sourcemaps: {maps}"
print("provenance probe OK")
PY

# 8. Focused release pytest from the installed package.
#
#    `-o pythonpath=` is required: pyproject.toml's [tool.pytest.ini_options]
#    pythonpath = ["."] would insert the repo root at sys.path[0] and shadow
#    the installed package, defeating the provenance assertions. The repo
#    root stays reachable through PYTHONPATH (<site>:<repo>) so the T01
#    factories (tests.test_t01_http:create_t01_test_app) and their spawned
#    uvicorn children import the installed task4_consistency.
step "8/13 focused release pytest (installed package + T01 shell/cache contracts)"
env PYTHONSAFEPATH=1 PYTHONPATH="$TMP/site:$ROOT" TASK4_T10_INSTALLED_ROOT="$TMP/site" \
  "$PY" -P -m pytest -q -o pythonpath= \
  tests/test_t10_release.py \
  tests/test_t01_http.py::test_react_shell_missing_build_fails_explicitly_and_legacy_route_stays \
  tests/test_t01_http.py::test_react_shell_rejects_partial_builds_and_legacy_route_stays \
  tests/test_t01_http.py::test_react_shell_serves_committed_build_with_no_store_shell_and_immutable_assets \
  2>&1 | tee -a "$LOG"

# 9. Real Python-only uvicorn from the installed package: random port, PID and
#    argv logged, /api/health readiness probe (server stdout follows $LOG).
step "9/13 real Python-only uvicorn + /api/health"
PORT="$("$PY" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
echo "port=$PORT" | tee -a "$LOG"
env PYTHONSAFEPATH=1 PYTHONPATH="$TMP/site:$ROOT" \
  TASK4_S01_STATE_PATH="$TMP/site/var/s01.sqlite3" \
  "$PY" -P -m uvicorn task4_consistency.web.app:app \
  --host 127.0.0.1 --port "$PORT" --log-level warning >>"$LOG" 2>&1 &
SERVER_PID=$!
echo "uvicorn_pid=$SERVER_PID" | tee -a "$LOG"
ps -o args= -p "$SERVER_PID" | tee -a "$LOG"
health_ok=0
for _ in $(seq 1 60); do
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "ERROR: uvicorn exited before readiness" | tee -a "$LOG"
    break
  fi
  if env NO_PROXY="127.0.0.1,localhost" no_proxy="127.0.0.1,localhost" \
    "$PY" -c "import json,urllib.request; d=json.load(urllib.request.urlopen('http://127.0.0.1:$PORT/api/health', timeout=1)); print(json.dumps(d, ensure_ascii=False))" >>"$LOG" 2>&1; then
    health_ok=1
    break
  fi
  sleep 0.5
done
if [[ "$health_ok" -ne 1 ]]; then
  echo "ERROR: /api/health never became ready on port $PORT" | tee -a "$LOG"
  exit 1
fi
echo "health OK" | tee -a "$LOG"

# 10. Playwright collection count (recorded, not a gate: the current specs
#     collect "66 tests in 12 files"; a different count is reported verbatim
#     so the integrator can reconcile it) and the full matrix from the
#     installed-import environment. Each spec spawns its own uvicorn child,
#     which inherits PYTHONSAFEPATH/PYTHONPATH and therefore imports
#     task4_consistency from <site>.
step "10/13 playwright collection (expected 66 tests in 12 files)"
npm run test:e2e -- --list 2>&1 | tee -a "$LOG"
collected="$(grep -oE '[0-9]+ tests? in [0-9]+ files?' "$LOG" | tail -1 || true)"
echo "collected: ${collected:-unknown}" | tee -a "$LOG"

step "11/13 full installed Playwright matrix"
env PYTHONSAFEPATH=1 PYTHONPATH="$TMP/site:$ROOT" TASK4_T10_INSTALLED_ROOT="$TMP/site" \
  npm run test:e2e 2>&1 | tee -a "$LOG"

# 12. Preserve this run's browser artifacts (Playwright clears its fixed
#     outputDir at the start of each run, so anything left belongs to it).
PLAYWRIGHT_OUT="/tmp/xiaopeng-task4-s01-playwright-artifacts"
if [[ -d "$PLAYWRIGHT_OUT" && -n "$(ls -A "$PLAYWRIGHT_OUT" 2>/dev/null)" ]]; then
  cp -a "$PLAYWRIGHT_OUT" "$LOG_DIR/lane-c-harness-playwright-artifacts-$(date +%Y%m%d-%H%M%S)"
  echo "playwright artifacts copied to $LOG_DIR" | tee -a "$LOG"
fi

# 13. Final source-input immutability check, then the EXIT trap removes the
#     temporary root and stops the server.
step "12/13 final source-input manifest re-verification"
(cd "$ROOT" && sha256sum -c "$TMP/source-input.sha256") 2>&1 | tee -a "$LOG"

step "13/13 done"
echo "INSTALLED RELEASE QUALIFICATION PASS (log: $LOG)" | tee -a "$LOG"
