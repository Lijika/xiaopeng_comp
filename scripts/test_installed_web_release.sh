#!/usr/bin/env bash
# T10 installed-release qualification harness (Issue #44, R1 fix).
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
# 10. Playwright collection gate: `npm run test:e2e -- --list` must parse to
#     exactly "66 tests in 12 files"; any other or unparsable value fails
# 11. full Playwright matrix from the same installed-import environment (specs
#     spawn their own uvicorn children)
# 12. unconditional (EXIT trap): preserve Playwright artifacts on success
#     and failure.  Freshness boundary: step 10 clears the fixed output
#     directory before collection, so preserved artifacts belong to this
#     run; a failure before the boundary records "playwright never started".
#     Then stop the owned uvicorn; remove the exact temporary root
# 13. unconditional (EXIT trap): rebuild the sorted source-input manifest and
#     compare complete before/after (detects changed/removed/added files
#     under configs/ fixtures/ data/), then print the PASS/FAIL verdict and
#     the numeric HARNESS_EXIT marker
#
# The EXIT trap preserves the original command status: any failure records
# the failing step (HARNESS_FAILED_STEP) and exact exit status (HARNESS_EXIT)
# in $LOG, still runs steps 12-13, and exits with the preserved status.
#
# The release root is always created directly under /tmp (ticket contract);
# deletion targets only the exact generated directory (safety prefix
# /tmp/t10-installed-release.*).
#
# All evidence goes to $LOG_DIR (default /tmp/codex/ticket-44-kimi-evidence)
# as lane-c-harness-*.log; Playwright artifacts are copied alongside.
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
# Release root directly under /tmp, regardless of TMPDIR (ticket contract).
TMP="$(mktemp -d /tmp/t10-installed-release.XXXXXX)"
SERVER_PID=""
PLAYWRIGHT_OUT="/tmp/xiaopeng-task4-s01-playwright-artifacts"
PLAYWRIGHT_STARTED=0
CURRENT_STEP="startup"
FAILED_STEP=""
STATUS=0
SOURCE_INPUT_CHANGED=0

step() { CURRENT_STEP="$1"; printf '\n== %s ==\n' "$1" | tee -a "$LOG"; }

record_failure() {
  # Runs only from the ERR trap; bash suppresses errexit while a trap is
  # executing, so this body cannot recurse and must NOT touch set -e.
  local status=$?
  if [[ -z "$FAILED_STEP" ]]; then FAILED_STEP="$CURRENT_STEP"; fi
  echo "ERROR: step '${FAILED_STEP}' failed with exit status ${status}" | tee -a "$LOG"
}
trap record_failure ERR

finalize() {
  # Preserve the original command status first; this trap must never
  # recurse or change the exit code it is about to report.
  local status=$?
  set +e
  trap - EXIT ERR INT TERM HUP
  STATUS="$status"

  # 12/13. Preserve this run's browser artifacts.  Freshness is established
  # at the Playwright boundary: step 10 clears the fixed output directory
  # before collection, so anything found later belongs to this run.  When
  # Playwright never started (failure before the boundary), that is
  # recorded explicitly and no stale directory is copied.  Runs on success
  # and failure when artifacts exist.
  step "12/13 preserve Playwright artifacts (unconditional)"
  if [[ "$PLAYWRIGHT_STARTED" -eq 1 && -d "$PLAYWRIGHT_OUT" && -n "$(ls -A "$PLAYWRIGHT_OUT" 2>/dev/null)" ]]; then
    ARTIFACT_DIR="$LOG_DIR/lane-c-harness-playwright-artifacts-$(date +%Y%m%d-%H%M%S)"
    cp -a "$PLAYWRIGHT_OUT" "$ARTIFACT_DIR"
    echo "playwright artifacts copied (from this run's boundary): $ARTIFACT_DIR" | tee -a "$LOG"
  elif [[ "$PLAYWRIGHT_STARTED" -eq 1 ]]; then
    echo "playwright started but produced no artifacts" | tee -a "$LOG"
  else
    echo "playwright never started; no artifacts to preserve" | tee -a "$LOG"
  fi

  # 13/13. Unconditional source conservation: rebuild the sorted
  # before/after manifests and compare completely (catches changed,
  # removed, and newly added files under configs/ fixtures/ data/).
  step "13/13 source-input conservation (unconditional)"
  if [[ -f "$TMP/source-input.sha256" ]]; then
    FINAL_MANIFEST="$TMP/source-input.final.sha256"
    (cd "$ROOT" && find configs fixtures data -type f -print0 \
      | sort -z | xargs -0 sha256sum) > "$FINAL_MANIFEST" 2>/dev/null
    if diff -u "$TMP/source-input.sha256" "$FINAL_MANIFEST" >/dev/null 2>&1; then
      echo "source-input manifests identical ($(wc -l < "$FINAL_MANIFEST") files)" | tee -a "$LOG"
    else
      echo "ERROR: source inputs changed/removed/added under configs fixtures data" | tee -a "$LOG"
      diff -u "$TMP/source-input.sha256" "$FINAL_MANIFEST" | tee -a "$LOG"
      SOURCE_INPUT_CHANGED=1
      [[ -z "$FAILED_STEP" ]] && FAILED_STEP="source-input conservation"
      [[ "$STATUS" -eq 0 ]] && STATUS=1
    fi
  else
    echo "no initial manifest; conservation skipped (before step 1)" | tee -a "$LOG"
  fi

  # Stop the owned uvicorn process.
  if [[ -n "$SERVER_PID" ]]; then
    kill "$SERVER_PID" 2>/dev/null
    wait "$SERVER_PID" 2>/dev/null
    echo "stopped owned uvicorn pid=$SERVER_PID" | tee -a "$LOG"
  fi

  # Remove exactly the harness-created temporary root (safety prefix).
  if [[ -n "$TMP" && "$TMP" == /tmp/t10-installed-release.* ]]; then
    rm -rf "$TMP"
    if [[ ! -e "$TMP" ]]; then
      echo "removed temporary root: $TMP" | tee -a "$LOG"
    else
      echo "ERROR: failed to remove temporary root: $TMP" | tee -a "$LOG"
      STATUS=1
    fi
  fi

  if [[ "$STATUS" -ne 0 ]]; then
    FAILED_STEP="${FAILED_STEP:-$CURRENT_STEP}"
  else
    FAILED_STEP="none"
  fi
  if [[ "$STATUS" -eq 0 && "$SOURCE_INPUT_CHANGED" -eq 0 ]]; then
    echo "INSTALLED RELEASE QUALIFICATION PASS (log: $LOG)" | tee -a "$LOG"
  else
    echo "INSTALLED RELEASE QUALIFICATION FAIL" | tee -a "$LOG"
  fi
  echo "HARNESS_FAILED_STEP=${FAILED_STEP}" | tee -a "$LOG"
  echo "HARNESS_EXIT=$STATUS" | tee -a "$LOG"
  exit "$STATUS"
}
trap finalize EXIT

# Termination signals (INT/TERM/HUP) must preserve the signal status
# (128+signum) through the same unconditional finalize path instead of
# dying mid-cleanup.  While the script waits on a foreground child the
# trap is deferred until the child exits; the child receives the same
# signal from the process group, so both die together and the preserved
# status stays non-zero.
handle_signal() {
  local name="$1" num="$2"
  echo "ERROR: harness terminated by signal $name ($num)" | tee -a "$LOG"
  [[ -z "$FAILED_STEP" ]] && FAILED_STEP="terminated by signal $name"
  exit "$((128 + num))"
}
trap 'handle_signal INT 2' INT
trap 'handle_signal TERM 15' TERM
trap 'handle_signal HUP 1' HUP

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
  find "$TMP/dist" -maxdepth 1 -mindepth 1 -printf '%f\n' | sort | tee -a "$LOG"
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

# 10. Playwright collection gate: the parsed `--list` output must equal the
#     frozen matrix exactly; any other or unparsable value fails here so a
#     removed spec cannot silently shrink coverage.
step "10/13 playwright collection gate (must equal '66 tests in 12 files')"
# Freshness boundary: clear the fixed output directory before collection so
# that any artifacts preserved in step 12 provably belong to this run.
PLAYWRIGHT_STARTED=1
rm -rf "$PLAYWRIGHT_OUT"
mkdir -p "$PLAYWRIGHT_OUT"
list_output="$(npm run test:e2e -- --list 2>&1 | tee -a "$LOG")"
collected="$(printf '%s\n' "$list_output" | grep -oE '[0-9]+ tests? in [0-9]+ files?' | tail -1 || true)"
echo "collected: ${collected:-unparsable}" | tee -a "$LOG"
if [[ "${collected:-}" != "66 tests in 12 files" ]]; then
  echo "ERROR: expected '66 tests in 12 files', observed '${collected:-unparsable}'" | tee -a "$LOG"
  exit 1
fi

# 11. Full matrix from the installed-import environment. Each spec spawns
#     its own uvicorn child, which inherits PYTHONSAFEPATH/PYTHONPATH and
#     therefore imports task4_consistency from <site>.
step "11/13 full installed Playwright matrix"
env PYTHONSAFEPATH=1 PYTHONPATH="$TMP/site:$ROOT" TASK4_T10_INSTALLED_ROOT="$TMP/site" \
  npm run test:e2e 2>&1 | tee -a "$LOG"

# Steps 12/13 run unconditionally from the EXIT trap (artifacts,
# conservation, uvicorn stop, temporary-root removal, verdict).
