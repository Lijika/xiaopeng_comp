#!/usr/bin/env bash
# T10/T54 installed-release qualification harness (Issues #44/#54).
#
# Single temporary-root release gate for the installed FastAPI Web release:
#  1. SHA256 manifest over source inputs (configs/ fixtures/ data/)
#  2. npm run build (production React build -> task4_consistency/web/static/react/)
#  3. PEP 517 sdist + wheel via `python -m build` (default isolated flow:
#     build dependencies come from pyproject.toml [build-system].requires)
#  4. assert exactly one .tar.gz and one .whl (current artifact)
#  5. pip install --no-deps --target <tmp>/site
#  6. copy configs/ fixtures/ data/ into the installed root (the app derives
#     ROOT from __file__, so the installed copy is the runtime authority)
#  7. provenance probe: task4_consistency.__file__ lives under <tmp>/site and
#     the installed React shell / hashed assets exist with no sourcemaps while
#     the removed legacy web/templates and static app.js/style.css surface
#     stays absent
#  8. focused release pytest (tests/test_t10_release.py + the canonical
#     T01 shell/cache contracts) with PYTHONSAFEPATH=1 and
#     PYTHONPATH=<site>:<repo>
#  9. controlled runtime window (Issue #54) in a loopback-only user+network
#     namespace: precheck server -> prechecks (catalog scan, telemetry
#     continuity, health, canonical shells) -> window start -> release
#     server -> frozen Playwright collection gate -> full matrix as the
#     operator-simulated cohort -> playwright-probe population -> accepted
#     fact snapshot -> prior-wheel rollback probe -> current-artifact
#     restoration -> window end -> sealed observation bundle -> public
#     verifier (valid + zero-caller acceptance)
# 10. unconditional (EXIT trap): preserve Playwright artifacts on success
#     and failure.  Then stop owned uvicorn; remove the exact temporary
#     root; rebuild the sorted source-input manifest and compare complete
#     before/after; print the PASS/FAIL verdict and the numeric
#     HARNESS_EXIT marker
#
# The EXIT trap preserves the original command status: any failure records
# the failing step (HARNESS_FAILED_STEP) and exact exit status (HARNESS_EXIT)
# in $LOG, still runs the preservation steps, and exits with the preserved
# status.
#
# The release root is always created directly under /tmp (ticket contract);
# deletion targets only the exact generated directory (safety prefix
# /tmp/t10-installed-release.*).
#
# The prior wheel is built from a `git archive` of the fixed base commit
# (Issue #54 fixed base 2627d4886..., which already serves the qualified
# React shell on /, /controlled/s01 and /controlled/s02) and its bytes are
# never altered; it is installed beside the current artifact for the
# deployment-only rollback rehearsal.
#
# All evidence goes to $LOG_DIR (default /tmp/codex/ticket-44-kimi-evidence)
# as lane-c-harness-*.log; Playwright artifacts and the sealed observation
# bundle are copied alongside.
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
# Run-specific Playwright output directory inside the unique harness root;
# the fixed global directory previously caused cross-run interference.
PLAYWRIGHT_OUT="$TMP/playwright-output"
PLAYWRIGHT_STARTED=0
CURRENT_STEP="startup"
FAILED_STEP=""
STATUS=0
SOURCE_INPUT_CHANGED=0

# Issue #54 fixed base (prior qualified artifact; serves the qualified React
# shell on the canonical routes, so the prior root probe expects '200 react').
T54_FIXED_BASE="2627d4886c89523edce6f13ac3d434789b4ba0c6"

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
  # Capture the entry step before the preservation steps update CURRENT_STEP.
  ENTRY_STEP="$CURRENT_STEP"

  # Preserve this run's browser artifacts (run-owned, as in Issue #44).
  # The marker file is written by the runtime window when the matrix starts.
  step "10b/10 preserve Playwright artifacts (unconditional)"
  if [[ -f "$TMP/t54-playwright-started" && -d "$PLAYWRIGHT_OUT" && -n "$(ls -A "$PLAYWRIGHT_OUT" 2>/dev/null)" ]]; then
    ARTIFACT_DIR="$LOG_DIR/lane-c-harness-playwright-artifacts-$(date +%Y%m%d-%H%M%S)"
    if cp -a "$PLAYWRIGHT_OUT" "$ARTIFACT_DIR"; then
      echo "playwright artifacts copied (run-owned): $ARTIFACT_DIR" | tee -a "$LOG"
    else
      echo "ERROR: failed to preserve playwright artifacts from $PLAYWRIGHT_OUT" | tee -a "$LOG"
      if [[ "$STATUS" -eq 0 ]]; then
        STATUS=1
        FAILED_STEP="10/10 preserve Playwright artifacts (unconditional)"
      else
        echo "secondary evidence: artifact preservation failed after primary step '${FAILED_STEP:-unknown}'" | tee -a "$LOG"
      fi
    fi
  elif [[ -f "$TMP/t54-playwright-started" ]]; then
    echo "playwright started but produced no artifacts" | tee -a "$LOG"
  else
    echo "playwright never started; no artifacts to preserve" | tee -a "$LOG"
  fi

  # Unconditional source conservation: rebuild the sorted before/after
  # manifests and compare completely.
  step "10c/10 source-input conservation (unconditional)"
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
    FAILED_STEP="${FAILED_STEP:-$ENTRY_STEP}"
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
# (128+signum) through the same unconditional finalize path.
handle_signal() {
  local name="$1" num="$2"
  echo "ERROR: harness terminated by signal $name ($num)" | tee -a "$LOG"
  [[ -z "$FAILED_STEP" ]] && FAILED_STEP="terminated by signal $name"
  exit "$((128 + num))"
}
trap 'handle_signal INT 2' INT
trap 'handle_signal TERM 15' TERM
trap 'handle_signal HUP 1' HUP

echo "== T10/T54 installed Web release qualification ==" | tee -a "$LOG"
echo "root: $ROOT" | tee -a "$LOG"
echo "tmp:  $TMP" | tee -a "$LOG"
echo "log:  $LOG" | tee -a "$LOG"
echo "fixed_base: $T54_FIXED_BASE" | tee -a "$LOG"
"$PY" --version 2>&1 | tee -a "$LOG"
node --version 2>&1 | tee -a "$LOG"
npm --version 2>&1 | tee -a "$LOG"

REVIEWED_COMMIT="$(git rev-parse HEAD)"
if [[ -n "$(git status --short --untracked-files=no)" ]]; then
  echo "ERROR: tracked tree must be clean before the commit-bound build" | tee -a "$LOG"
  exit 1
fi
if [[ -n "${T54_REVIEWED_COMMIT:-}" && "$T54_REVIEWED_COMMIT" != "$REVIEWED_COMMIT" ]]; then
  echo "ERROR: reviewed commit mismatch expected=$T54_REVIEWED_COMMIT actual=$REVIEWED_COMMIT" | tee -a "$LOG"
  exit 1
fi
NODE_VERSION="$(node --version)"
NPM_VERSION="$(npm --version)"
echo "reviewed_commit=$REVIEWED_COMMIT tracked_tree_clean=true" | tee -a "$LOG"

# 1. Source-input manifest: deterministic (sorted) SHA256 over configs/,
#    fixtures/ and data/. Relative paths, verified later from $ROOT.
step "1/10 source-input SHA256 manifest (configs fixtures data)"
find configs fixtures data -type f -print0 | sort -z | xargs -0 sha256sum > "$TMP/source-input.sha256"
wc -l "$TMP/source-input.sha256" | tee -a "$LOG"

# 2. Production React build. Writes into task4_consistency/web/static/react/
#    (the only repo mutation this harness performs; byte-identical when
#    frontend sources are unchanged).
step "2/10 npm run build (production React build)"
npm run build 2>&1 | tee -a "$LOG"
if [[ -n "$(git status --short --untracked-files=no)" ]]; then
  echo "ERROR: production build changed tracked bytes from reviewed commit" | tee -a "$LOG"
  exit 1
fi

# 3. PEP 517 sdist + wheel (current artifact).
step "3/10 python -m build (isolated PEP 517 sdist + wheel, current)"
mkdir -p "$TMP/dist"
"$PY" -m build --outdir "$TMP/dist" 2>&1 | tee -a "$LOG"

# 4. Exactly one sdist and one wheel; list the real files otherwise.
step "4/10 assert exactly one .tar.gz and one .whl (current)"
mapfile -t SDISTS < <(ls "$TMP"/dist/*.tar.gz 2>/dev/null || true)
mapfile -t WHEELS < <(ls "$TMP"/dist/*.whl 2>/dev/null || true)
if [[ "${#SDISTS[@]}" -ne 1 || "${#WHEELS[@]}" -ne 1 ]]; then
  echo "ERROR: expected exactly one .tar.gz and one .whl in $TMP/dist" | tee -a "$LOG"
  find "$TMP/dist" -maxdepth 1 -mindepth 1 -printf '%f\n' | sort | tee -a "$LOG"
  exit 1
fi
SDIST="${SDISTS[0]}"
WHEEL="${WHEELS[0]}"
CURRENT_SHA="$(sha256sum "$WHEEL" | awk '{print $1}')"
echo "sdist: $SDIST" | tee -a "$LOG"
echo "wheel: $WHEEL" | tee -a "$LOG"
echo "current_wheel_sha256=$CURRENT_SHA" | tee -a "$LOG"

# 5. Install the current wheel into the temporary site root.
step "5/10 pip install --no-deps --target $TMP/site (current)"
mkdir -p "$TMP/site"
"$PIP" install --no-deps --target "$TMP/site" "$WHEEL" 2>&1 | tee -a "$LOG"

# 6. Runtime input copies for the installed root; SQLite authority dir.
step "6/10 copy operator inputs into installed root (current)"
cp -a configs fixtures data "$TMP/site/"
mkdir -p "$TMP/site/var"

# 6b. Prior qualified artifact: build the wheel from a `git archive` of the
#     fixed base (bytes never altered) and install it beside the current.
step "6b/10 prior wheel from fixed-base git archive + install"
PRIOR_ARCHIVE="$TMP/prior-archive"
mkdir -p "$PRIOR_ARCHIVE"
git archive "$T54_FIXED_BASE" | tar -x -C "$PRIOR_ARCHIVE"
(cd "$PRIOR_ARCHIVE" && "$PY" -m build --outdir "$TMP/dist-prior" 2>&1) | tee -a "$LOG"
PRIOR_WHEEL="$(ls "$TMP"/dist-prior/*.whl 2>/dev/null | head -1)"
if [[ -z "$PRIOR_WHEEL" ]]; then
  echo "ERROR: no prior wheel produced from fixed-base archive" | tee -a "$LOG"
  exit 1
fi
PRIOR_SHA="$(sha256sum "$PRIOR_WHEEL" | awk '{print $1}')"
mkdir -p "$TMP/prior-site"
"$PIP" install --no-deps --target "$TMP/prior-site" "$PRIOR_WHEEL" 2>&1 | tee -a "$LOG"
cp -a configs fixtures data "$TMP/prior-site/"
echo "prior_wheel: $PRIOR_WHEEL" | tee -a "$LOG"
echo "prior_wheel_sha256=$PRIOR_SHA" | tee -a "$LOG"
PACKAGE_IDENTITY="$(env PYTHONSAFEPATH=1 PYTHONPATH="$TMP/site" "$PY" -P -c \
  'import importlib.metadata; print("task4-consistency==" + importlib.metadata.version("task4-consistency"))')"
echo "package_identity=$PACKAGE_IDENTITY" | tee -a "$LOG"

# 6c. S02 registry for the rollback/restoration stages (same inputs).
step "6c/10 S02 source registry (rollback stages)"
S02_OBJECT_ROOT="$TMP/s02-objects"
mkdir -p "$S02_OBJECT_ROOT"
printf '{"ok": true}' > "$S02_OBJECT_ROOT/result.json"
cat > "$TMP/s02-registry.json" <<'S02JSON'
{
  "schema_version": "s02-runtime-registry/1",
  "sources": [
    {
      "tenant_id": "tenant-t54",
      "source_system_id": "t54-registered-source",
      "workload_identity_id": "t54-workload",
      "adapter_id": "t54-adapter",
      "adapter_version": "1",
      "source_shape": "ocr-detection/unversioned",
      "producer_family": "t54-ocr"
    }
  ],
  "objects": [
    {
      "tenant_id": "tenant-t54",
      "source_system_id": "t54-registered-source",
      "object_ref": "t54-result",
      "media_type": "application/json",
      "file": "result.json"
    }
  ]
}
S02JSON
echo "s02 registry ready" | tee -a "$LOG"

# 6d. Prior-artifact observer wrapper: the prior wheel stays byte-identical;
#     the wrapper loads the CURRENT observation module + catalog from the
#     current installed site by absolute path and registers them around the
#     PRIOR app, so rollback probes produce rollback-probe observations.
cat > "$TMP/prior_wrapper_app.py" <<'WRAPPER'
"""Prior-artifact observer wrapper (Issue #54)."""
import importlib.util
import sys
from pathlib import Path

PRIOR_SITE = Path("__PRIOR_SITE__").resolve()
CURRENT_SITE = Path("__CURRENT_SITE__").resolve()


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# The current catalog + observation module come from the current installed
# site (immutable data; the prior wheel does not contain them).  Registering
# the catalog under its canonical name lets the observation module import it
# even though the prior package occupies `task4_consistency.web`.
_load(
    "task4_consistency.web.legacy_catalog",
    CURRENT_SITE / "task4_consistency" / "web" / "legacy_catalog.py",
)
current_observation = _load(
    "t54_current_observation",
    CURRENT_SITE / "task4_consistency" / "web" / "observation.py",
)

# The prior artifact is imported from the prior installed site.
sys.path.insert(0, str(PRIOR_SITE))
import task4_consistency.web.app as prior_web  # noqa: E402

prior_app = prior_web.app
# The prior artifact (fixed base 2627d488) self-registers its own
# observation middleware at import (Issue #54 final seam).  If left
# enabled, it records every rollback request AGAIN alongside the current
# observer below, duplicating correlation ids in the shared JSONL and
# invalidating the sealed window.  Neutralize it at runtime without ever
# altering the prior wheel's bytes: the current observation module remains
# the sole recorder for the prior-artifact rollback stage.
_inner_recorder = getattr(prior_web, "_OBSERVATION_RECORDER", None)
if _inner_recorder is not None:
    _inner_recorder.enabled = False
recorder = current_observation.recorder_from_env(
    family_table=current_observation.app_family_table(prior_app)
)
app = current_observation.ObservationMiddleware(prior_app, recorder)
WRAPPER
sed -i "s|__PRIOR_SITE__|$TMP/prior-site|; s|__CURRENT_SITE__|$TMP/site|" "$TMP/prior_wrapper_app.py"
"$PY" -c "import ast; ast.parse(open('$TMP/prior_wrapper_app.py').read())" && echo "prior wrapper syntax OK" | tee -a "$LOG"

# 7. Provenance probe: installed import + React shell/hashed assets present,
#    the removed legacy static/templates surface absent, no sourcemaps.
step "7/10 provenance probe against installed root (current)"
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

# The five legacy web files are contracted away (Issue #45): the installed
# artifact must not ship them, while the React index + hashed assets above
# remain the shell authority.
for name in ("app.js", "style.css"):
    assert not (web / "static" / name).exists(), f"retired legacy static present: {name}"
for name in ("index.html", "s01.html", "s02.html"):
    assert not (web / "templates" / name).exists(), f"retired legacy template present: {name}"
maps = sorted((web / "static" / "react").rglob("*.map"))
assert not maps, f"production build must not ship sourcemaps: {maps}"
print("provenance probe OK")
PY

# 8. Focused release pytest from the installed package (canonical contracts).
#
#    `-o pythonpath=` is required: pyproject.toml's [tool.pytest.ini_options]
#    pythonpath = ["."] would insert the repo root at sys.path[0] and shadow
#    the installed package, defeating the provenance assertions.
step "8/10 focused release pytest (installed package + canonical T01 contracts)"
env PYTHONSAFEPATH=1 PYTHONPATH="$TMP/site:$ROOT" TASK4_T10_INSTALLED_ROOT="$TMP/site" \
  "$PY" -P -m pytest -q -o pythonpath= \
  tests/test_t10_release.py \
  tests/test_t01_http.py::test_react_shell_missing_build_fails_explicitly_on_canonical_and_alias_routes \
  tests/test_t01_http.py::test_react_shell_rejects_partial_builds_and_canonical_routes_stay_closed \
  tests/test_t01_http.py::test_react_shell_serves_committed_build_with_no_store_shell_and_immutable_assets \
  tests/test_t01_http.py::test_canonical_routes_serve_the_qualified_react_shell \
  2>&1 | tee -a "$LOG"

# 9. Controlled runtime window (Issue #54) in a loopback-only namespace.
#    Build/install finished above; only the runtime executes in isolation.
#    The window body writes its exit status to $TMP/t54-window-status.
step "9/10 loopback-only controlled runtime window"
WINDOW_ID="t54-installed-window-$(date +%Y%m%d-%H%M%S)"
OBS_DIR="$TMP/t54-obs"
BUNDLE_DIR="$LOG_DIR/window-bundle"
STATE_PATH="$TMP/authority.sqlite3"
echo "window_id=$WINDOW_ID" | tee -a "$LOG"
echo "obs_dir=$OBS_DIR" | tee -a "$LOG"
echo "bundle_dir=$BUNDLE_DIR" | tee -a "$LOG"
echo "state_path=$STATE_PATH" | tee -a "$LOG"
rm -f "$TMP/t54-window-status"

cat > "$TMP/t54-window-body.sh" <<'BODY'
set -euo pipefail

step() { printf '\n== %s ==\n' "$1" >>"$LOG"; }

: > "$TMP/t54-window-pids"
cleanup_window() {
  # Best-effort teardown of every server started in this window, so no
  # orphan keeps the temporary root or the namespace alive on failure.
  if [[ -f "$TMP/t54-window-pids" ]]; then
    while read -r window_pid; do
      [[ -n "$window_pid" ]] || continue
      kill "$window_pid" 2>/dev/null || true
    done < "$TMP/t54-window-pids"
  fi
}
trap cleanup_window EXIT

# Loopback-only: raise lo and record the namespace route table.
ip link set lo up
{
  ip -o addr show dev lo
  ip route 2>/dev/null || true
} > "$TMP/network-routes.txt"
cat "$TMP/network-routes.txt" >> "$LOG"
echo "namespace_hostname=$(hostname)" >>"$LOG"
echo "namespace_tz=$(date +%Z)" >>"$LOG"

HEALTH_PY() { "$PY" -c "import json,urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:$1/api/health', timeout=2)))"; }

start_server() {
  # $1 = site root, $2 = extra env (obs vars or empty), $3 = state path,
  # $4 = uvicorn app target (default task4_consistency.web.app:app),
  # $5 = extra PYTHONPATH prefix (default empty)
  local site="$1" obs_env="$2" state="$3" app_target="${4:-task4_consistency.web.app:app}"
  local extra_path="${5:-}"
  local pythonpath="$site:$ROOT"
  if [[ -n "$extra_path" ]]; then pythonpath="$extra_path:$pythonpath"; fi
  local port
  port="$("$PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
  local pid
  env PYTHONSAFEPATH=1 PYTHONPATH="$pythonpath" \
    TASK4_S01_STATE_PATH="$state" \
    TASK4_S01_DEMO_CREDENTIAL="s01-registered-demo-test-credential" \
    TASK4_S01_DEMO_SUBJECT="c-demo-test-user" \
    TASK4_S01_OPERATOR_CREDENTIAL="s01-registered-operator-test-credential" \
    TASK4_S01_OPERATOR_SUBJECT="c-demo-test-operator" \
    TASK4_S01_AUDITOR_CREDENTIAL="s01-registered-auditor-test-credential" \
    TASK4_S01_AUDITOR_SUBJECT="c-demo-test-auditor" \
    TASK4_S02_REGISTRY_PATH="$TMP/s02-registry.json" \
    TASK4_S02_OBJECT_ROOT="$S02_OBJECT_ROOT" \
    TASK4_S02_CREDENTIAL="t54-s02-credential" \
    TASK4_S02_SUBJECT="t54-integrator" \
    TASK4_S02_TENANT_ID="tenant-t54" \
    TASK4_S02_SOURCE_SYSTEM_ID="t54-registered-source" \
    $obs_env \
    "$PY" -P -m uvicorn "$app_target" \
    --host 127.0.0.1 --port "$port" --log-level warning >>"$LOG" 2>&1 &
  pid=$!
  local ok=0
  for _ in $(seq 1 60); do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "ERROR: uvicorn exited before readiness (site=$site)" >>"$LOG"
      return 1
    fi
    if NO_PROXY="127.0.0.1,localhost" no_proxy="127.0.0.1,localhost" "$PY" -c "
import json,urllib.request
d=json.load(urllib.request.urlopen('http://127.0.0.1:$port/api/health', timeout=1))
print(json.dumps(d, ensure_ascii=False))" >>"$LOG" 2>&1; then
      ok=1
      break
    fi
    sleep 0.5
  done
  if [[ "$ok" -ne 1 ]]; then
    echo "ERROR: /api/health never became ready on port $port (site=$site)" >>"$LOG"
    return 1
  fi
  echo "server_ready site=$site port=$port pid=$pid" >>"$LOG"
  echo "$pid" >> "$TMP/t54-window-pids"
  echo "$pid $port"
}

stop_server() {
  local pid="$1"
  kill "$pid" 2>/dev/null || true
  # The server was started inside a command-substitution subshell, so it is
  # not this shell's child; poll for real exit instead of waiting.  The
  # observation "end" lifecycle record is written before uvicorn exits, so
  # process exit implies the record is durable.
  for _ in $(seq 1 100); do
    if ! kill -0 "$pid" 2>/dev/null; then break; fi
    sleep 0.2
  done
  echo "stopped server pid=$pid" >>"$LOG"
}

get_shell() {
  # $1=port $2=path $3=auth header value or empty
  local port="$1" path="$2" auth="${3:-}"
  if [[ -n "$auth" ]]; then
    "$PY" -c "
import urllib.request
req = urllib.request.Request('http://127.0.0.1:$port$path', headers={'Authorization': '$auth'})
try:
    r = urllib.request.urlopen(req, timeout=5)
    print(r.status, r.headers.get('Cache-Control', ''))
except urllib.error.HTTPError as e:
    print(e.code, e.headers.get('Cache-Control', ''))"
  else
    "$PY" -c "
import urllib.request
req = urllib.request.Request('http://127.0.0.1:$port$path')
try:
    r = urllib.request.urlopen(req, timeout=5)
    body = r.read().decode('utf-8', 'replace')
    print(r.status, 'react' if '/static/react/assets/' in body else 'other')
except urllib.error.HTTPError as e:
    print(e.code, '')
    import sys; sys.stderr.write(e.read().decode('utf-8', 'replace')[:200])"
  fi
}

capture_facts() {
  # $1 = port; $2 = optional application_id; $3 = optional session-cookie
  # file.  With no application_id, open an S01 session, submit the frozen
  # scenario and poll the queue; with one, only poll the queue for that
  # application.  The session token is persisted to the cookie file (when
  # given) and reused by later stages: S01 projections are scoped to the
  # submitting session (C-DEMO/session/<hash>), so accepted-fact equality
  # must be read with the SAME session across current -> prior -> current.
  # Re-submission is avoided (cross-process idempotent replay is not an S01
  # guarantee).  Prints the queue item as canonical JSON.
  local port="$1" want_app="${2:-}" cookie_file="${3:-}"
  "$PY" - "$port" "${want_app}" "${cookie_file}" <<'FACTS'
import json, sys, time, urllib.request, urllib.error
from http.cookies import SimpleCookie
from pathlib import Path

port = sys.argv[1]
want_app = sys.argv[2] if len(sys.argv) > 2 else ""
cookie_file = sys.argv[3] if len(sys.argv) > 3 else ""
base = f"http://127.0.0.1:{port}"
DEMO = "s01-registered-demo-test-credential"

def req(method, path, body=None, headers=None, cookie=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    r = urllib.request.Request(base + path, data=data, method=method,
                               headers={"Content-Type": "application/json", **(headers or {})})
    if cookie:
        r.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), resp.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), e.headers

cookie = ""
if cookie_file and Path(cookie_file).is_file():
    cookie = f"s01_session={Path(cookie_file).read_text(encoding='utf-8').strip()}"
else:
    status, body, resp_headers = req("POST", "/controlled/s01/api/session", {},
                                     {"Authorization": f"Bearer {DEMO}"})
    if status != 204:
        raise SystemExit(f"session failed: {status} {body}")
    cookies = SimpleCookie()
    cookies.load(resp_headers.get("Set-Cookie", ""))
    token = cookies["s01_session"].value
    cookie = f"s01_session={token}"
    if cookie_file:
        Path(cookie_file).write_text(token, encoding="utf-8")

application_id = want_app
if not application_id:
    status, body, _ = req("POST", "/controlled/s01/api/commands/submit",
                          {"scenario_id": "app_r53_bad_engine.json",
                           "idempotency_key": "t54-harness-fact"},
                          {"X-S01-Role": "integrator", "X-S01-Scope": "C-DEMO",
                           "Authorization": f"Bearer {DEMO}"},
                          cookie=cookie)
    if status != 200:
        raise SystemExit(f"submit failed: {status} {body}")
    application_id = json.loads(body)["application_id"]

deadline = time.monotonic() + 90.0
item = None
while time.monotonic() < deadline:
    status, body, _ = req("GET", "/controlled/s01/api/queries/queue",
                          headers={"X-S01-Role": "reviewer", "X-S01-Scope": "C-DEMO",
                                   "Authorization": f"Bearer {DEMO}"},
                          cookie=cookie)
    if status == 200:
        queue = json.loads(body)
        for candidate in queue.get("items", []):
            if candidate.get("application_id") == application_id:
                item = candidate
                break
        if item is not None:
            break
    time.sleep(1.0)
if item is None:
    raise SystemExit(f"application {application_id} never became readable")
print(json.dumps(item, ensure_ascii=False, sort_keys=True))
FACTS
}

# --- Prechecks (capture-free server; no window records yet) -----------------
echo "== prechecks ==" >>"$LOG"
PRE_RESULT="$(start_server "$TMP/site" "" "$STATE_PATH")"
PRE_PID="${PRE_RESULT%% *}"
PRE_PORT="${PRE_RESULT##* }"
for _ in $(seq 1 10); do
  OUT="$(get_shell "$PRE_PORT" "/" "")"
  case "$OUT" in
    200\ react) break ;;
  esac
  sleep 0.5
done
echo "canonical_root_probe: $OUT" >>"$LOG"
[[ "$OUT" == "200 react" ]] || { echo "ERROR: canonical root precheck failed" >>"$LOG"; exit 1; }
S01_OUT="$(get_shell "$PRE_PORT" "/controlled/s01" "Bearer s01-registered-demo-test-credential")"
echo "canonical_s01_probe: $S01_OUT" >>"$LOG"
[[ "$S01_OUT" == 200* ]] || { echo "ERROR: canonical s01 precheck failed" >>"$LOG"; exit 1; }
S02_OUT="$(get_shell "$PRE_PORT" "/controlled/s02" "Bearer t54-s02-credential")"
echo "canonical_s02_probe: $S02_OUT" >>"$LOG"
[[ "$S02_OUT" == 200* ]] || { echo "ERROR: canonical s02 precheck failed" >>"$LOG"; exit 1; }
stop_server "$PRE_PID"

# Catalog + source scan over the installed package tree.
CATALOG_OUT="$TMP/catalog-scan-window.json"
env PYTHONSAFEPATH=1 PYTHONPATH="$TMP/site:$ROOT" "$PY" -P -m \
  task4_consistency.web.legacy_catalog scan --root "$ROOT" --output "$CATALOG_OUT" \
  >>"$LOG" 2>&1
echo "catalog scan: $(tail -1 "$CATALOG_OUT" 2>/dev/null || true)" >>"$LOG"
"$PY" - "$CATALOG_OUT" <<'CATPY' >>"$LOG" 2>&1
import json, sys
report = json.load(open(sys.argv[1], encoding="utf-8"))
assert report["ok"], report
assert report["zero_canonical_source_edges"], report["canonical_source_edges"]
assert report["completeness_ok"], report
print("catalog window scan OK")
CATPY

# Telemetry continuity self-check on a throwaway log (discarded).
THROWAWAY_DIR="$TMP/t54-obs-throwaway"
mkdir -p "$THROWAWAY_DIR"
TC_RESULT="$(start_server "$TMP/site" \
  "TASK4_OBS_LOG_DIR=$THROWAWAY_DIR TASK4_OBS_WINDOW_ID=$WINDOW_ID TASK4_OBS_ARTIFACT_SHA256=$CURRENT_SHA TASK4_OBS_ARTIFACT_STAGE=current TASK4_OBS_PROCESS_CLASS=release TASK4_OBS_PROCESS_ID=t54-telemetry-selfcheck" \
  "$STATE_PATH")"
TC_PID="${TC_RESULT%% *}"
TC_PORT="${TC_RESULT##* }"
get_shell "$TC_PORT" "/" "" >/dev/null
stop_server "$TC_PID"
"$PY" - "$THROWAWAY_DIR" <<'TELPY' >>"$LOG" 2>&1
import json, os, sys
log_dir = sys.argv[1]
records = [json.loads(line) for line in open(os.path.join(log_dir, "requests.jsonl"), encoding="utf-8")]
assert records, "telemetry continuity: no records"
assert all(r["sequence"] == i + 1 for i, r in enumerate(records)), "sequence gap"
assert records[0]["traffic_class"] in ("release", "health"), records[0]["traffic_class"]
life = [json.loads(line) for line in open(os.path.join(log_dir, "process-lifecycle.jsonl"), encoding="utf-8")]
events = [e["event"] for e in life]
assert events == ["start", "end"], events
print("telemetry continuity OK")
TELPY
rm -rf "$THROWAWAY_DIR"

# --- Window start (only after every precheck passed) -------------------------
WINDOW_START_UTC="$("$PY" -c 'import datetime; print(datetime.datetime.now(datetime.timezone.utc).isoformat())')"
WINDOW_TZ="$(date +%Z)"
printf '%s\n' "$WINDOW_TZ" > "$TMP/window-timezone.txt"
echo "window_start_utc=$WINDOW_START_UTC tz=$WINDOW_TZ" >>"$LOG"

# Dedicated release server (traffic class release; /api/health -> health).
REL_RESULT="$(start_server "$TMP/site" \
  "TASK4_OBS_LOG_DIR=$OBS_DIR TASK4_OBS_WINDOW_ID=$WINDOW_ID TASK4_OBS_ARTIFACT_SHA256=$CURRENT_SHA TASK4_OBS_ARTIFACT_STAGE=current TASK4_OBS_PROCESS_CLASS=release TASK4_OBS_PROCESS_ID=t54-release" \
  "$STATE_PATH")"
REL_PID="${REL_RESULT%% *}"
REL_PORT="${REL_RESULT##* }"

# Frozen Playwright collection gate + full matrix (operator-simulated cohort;
# every spec child inherits the observation environment).
step "9/10 window: playwright collection gate (must equal '48 tests in 10 files')"
rm -rf "$PLAYWRIGHT_OUT"
mkdir -p "$PLAYWRIGHT_OUT"
touch "$TMP/t54-playwright-started"
list_output="$(npm run test:e2e -- --list --output "$PLAYWRIGHT_OUT" 2>&1 | tee -a "$LOG")"
collected="$(printf '%s\n' "$list_output" | grep -oE '[0-9]+ tests? in [0-9]+ files?' | tail -1 || true)"
echo "collected: ${collected:-unparsable}" >>"$LOG"
if [[ "${collected:-}" != "48 tests in 10 files" ]]; then
  echo "ERROR: expected '48 tests in 10 files', observed '${collected:-unparsable}'" >>"$LOG"
  exit 1
fi
npm run test:e2e -- --list | grep -oE '[A-Za-z0-9_.-]+\.spec\.js:[0-9]+:[0-9]+ › .*' \
  | sort > "$TMP/cohort-node-ids.txt"
sha256sum tests/*.spec.js playwright.config.js > "$TMP/cohort-spec-sha256.txt"
sha256sum "$TMP/cohort-node-ids.txt" "$TMP/cohort-spec-sha256.txt" >>"$LOG"
cp "$TMP/cohort-node-ids.txt" "$LOG_DIR/cohort-node-ids.txt"
cp "$TMP/cohort-spec-sha256.txt" "$LOG_DIR/cohort-spec-sha256.txt"

step "9/10 window: full installed Playwright matrix (operator-simulated)"
env PYTHONSAFEPATH=1 PYTHONPATH="$TMP/site:$ROOT" TASK4_T10_INSTALLED_ROOT="$TMP/site" \
  TASK4_OBS_LOG_DIR="$OBS_DIR" TASK4_OBS_WINDOW_ID="$WINDOW_ID" \
  TASK4_OBS_ARTIFACT_SHA256="$CURRENT_SHA" TASK4_OBS_ARTIFACT_STAGE="current" \
  TASK4_OBS_PROCESS_CLASS="operator-simulated" \
  npm run test:e2e -- --output "$PLAYWRIGHT_OUT" 2>&1 >>"$LOG"

# Playwright-probe population: one dedicated current-artifact process with
# explicit canonical-shell probes under playwright-probe.
step "9/10 window: playwright-probe population"
PROBE_RESULT="$(start_server "$TMP/site" \
  "TASK4_OBS_LOG_DIR=$OBS_DIR TASK4_OBS_WINDOW_ID=$WINDOW_ID TASK4_OBS_ARTIFACT_SHA256=$CURRENT_SHA TASK4_OBS_ARTIFACT_STAGE=current TASK4_OBS_PROCESS_CLASS=playwright-probe TASK4_OBS_PROCESS_ID=t54-playwright-probe" \
  "$STATE_PATH")"
PROBE_PID="${PROBE_RESULT%% *}"
PROBE_PORT="${PROBE_RESULT##* }"
get_shell "$PROBE_PORT" "/" "" >/dev/null
get_shell "$PROBE_PORT" "/controlled/s01" "Bearer s01-registered-demo-test-credential" >/dev/null
get_shell "$PROBE_PORT" "/api/health" "" >/dev/null
stop_server "$PROBE_PID"

# Accepted-fact snapshot on the release server (same SQLite authority).
step "9/10 window: accepted fact snapshot (current artifact)"
FACT_CURRENT="$(capture_facts "$REL_PORT" "" "$TMP/fact-cookie.txt")"
echo "fact_current=$FACT_CURRENT" >>"$LOG"
echo "$FACT_CURRENT" > "$TMP/fact-current.json"
FACT_APP_ID="$("$PY" -c "import json,sys; print(json.loads(sys.argv[1])['application_id'])" "$FACT_CURRENT")"
echo "fact_application_id=$FACT_APP_ID" >>"$LOG"

# Stop the current artifact before the rollback stage.
stop_server "$REL_PID"

# --- Prior-artifact rollback probe (rollback-probe) --------------------------
step "9/10 window: prior-artifact rollback probe (rollback-probe)"
PRIOR_RESULT="$(start_server "$TMP/prior-site" \
  "TASK4_OBS_LOG_DIR=$OBS_DIR TASK4_OBS_WINDOW_ID=$WINDOW_ID TASK4_OBS_ARTIFACT_SHA256=$PRIOR_SHA TASK4_OBS_ARTIFACT_STAGE=prior TASK4_OBS_PROCESS_CLASS=rollback-probe TASK4_OBS_PROCESS_ID=t54-rollback-prior" \
  "$STATE_PATH" "prior_wrapper_app:app" "$TMP")"
PRIOR_PID="${PRIOR_RESULT%% *}"
PRIOR_PORT="${PRIOR_RESULT##* }"
# Prior artifact: package identity from the prior site (byte-identical wheel).
env PYTHONSAFEPATH=1 PYTHONPATH="$TMP/prior-site:$ROOT" PRIOR_SITE="$TMP/prior-site" \
  "$PY" -P - <<'PRIORPY' >>"$LOG"
import os
import task4_consistency
from pathlib import Path

site = Path(os.environ["PRIOR_SITE"]).resolve()
module_file = Path(task4_consistency.__file__).resolve()
assert module_file.is_relative_to(site), (
    f"prior artifact not imported from prior site: {module_file}"
)
print("prior_import=", module_file)
PRIORPY
PRIOR_ROOT_PROBE="$(get_shell "$PRIOR_PORT" "/" "")"
echo "prior_root_probe: $PRIOR_ROOT_PROBE" >>"$LOG"
[[ "$PRIOR_ROOT_PROBE" == "200 react" ]] || { echo "ERROR: prior root must serve the React shell" >>"$LOG"; exit 1; }
PRIOR_S01_PROBE="$(get_shell "$PRIOR_PORT" "/controlled/s01" "Bearer s01-registered-demo-test-credential")"
echo "prior_s01_probe: $PRIOR_S01_PROBE" >>"$LOG"
[[ "$PRIOR_S01_PROBE" == 200* ]] || { echo "ERROR: prior s01 must serve the React shell" >>"$LOG"; exit 1; }
PRIOR_S02_PROBE="$(get_shell "$PRIOR_PORT" "/controlled/s02" "Bearer t54-s02-credential")"
echo "prior_s02_probe: $PRIOR_S02_PROBE" >>"$LOG"
[[ "$PRIOR_S02_PROBE" == 200* ]] || { echo "ERROR: prior s02 must serve the React shell" >>"$LOG"; exit 1; }
HEALTH_PY "$PRIOR_PORT" >/dev/null || { echo "ERROR: prior health failed" >>"$LOG"; exit 1; }
step "9/10 window: prior-artifact Playwright ownership probe"
env TASK4_T54_PRIOR_BASE_URL="http://127.0.0.1:$PRIOR_PORT" \
  "$ROOT/node_modules/.bin/playwright" test tests/test_t54_prior_artifact.spec.js \
  --output "$PLAYWRIGHT_OUT/prior-artifact" >>"$LOG" 2>&1
FACT_PRIOR="$(capture_facts "$PRIOR_PORT" "$FACT_APP_ID" "$TMP/fact-cookie.txt")"
echo "fact_prior=$FACT_PRIOR" >>"$LOG"
echo "$FACT_PRIOR" > "$TMP/fact-prior.json"
[[ "$FACT_PRIOR" == "$FACT_CURRENT" ]] || {
  echo "ERROR: accepted facts drifted across current -> prior" >>"$LOG"
  exit 1
}
stop_server "$PRIOR_PID"

# --- Current-artifact restoration (release; stage three of the rehearsal) ----
step "9/10 window: current artifact restoration (release)"
RESTORE_RESULT="$(start_server "$TMP/site" \
  "TASK4_OBS_LOG_DIR=$OBS_DIR TASK4_OBS_WINDOW_ID=$WINDOW_ID TASK4_OBS_ARTIFACT_SHA256=$CURRENT_SHA TASK4_OBS_ARTIFACT_STAGE=current TASK4_OBS_PROCESS_CLASS=release TASK4_OBS_PROCESS_ID=t54-restore" \
  "$STATE_PATH")"
RESTORE_PID="${RESTORE_RESULT%% *}"
RESTORE_PORT="${RESTORE_RESULT##* }"
RESTORE_ROOT_PROBE="$(get_shell "$RESTORE_PORT" "/" "")"
echo "restore_root_probe: $RESTORE_ROOT_PROBE" >>"$LOG"
[[ "$RESTORE_ROOT_PROBE" == "200 react" ]] || { echo "ERROR: restored root must serve React" >>"$LOG"; exit 1; }
RESTORE_S01_PROBE="$(get_shell "$RESTORE_PORT" "/controlled/s01" "Bearer s01-registered-demo-test-credential")"
echo "restore_s01_probe: $RESTORE_S01_PROBE" >>"$LOG"
[[ "$RESTORE_S01_PROBE" == 200* ]] || { echo "ERROR: restored s01 must serve React" >>"$LOG"; exit 1; }
RESTORE_S02_PROBE="$(get_shell "$RESTORE_PORT" "/controlled/s02" "Bearer t54-s02-credential")"
echo "restore_s02_probe: $RESTORE_S02_PROBE" >>"$LOG"
[[ "$RESTORE_S02_PROBE" == 200* ]] || { echo "ERROR: restored s02 must serve React" >>"$LOG"; exit 1; }
RESTORE_403="$(get_shell "$RESTORE_PORT" "/controlled/s01" "")"
echo "restore_s01_unauthenticated_probe: $RESTORE_403" >>"$LOG"
[[ "$RESTORE_403" == 403* ]] || { echo "ERROR: restored s01 must stay protected" >>"$LOG"; exit 1; }
# Protected explicit 404s are covered by the installed T01/spec seams and
# by the operator cohort itself; an arbitrary 404 path would be an
# unregistered family and (by design) invalidate the sealed window.
FACT_RESTORE="$(capture_facts "$RESTORE_PORT" "$FACT_APP_ID" "$TMP/fact-cookie.txt")"
echo "fact_restore=$FACT_RESTORE" >>"$LOG"
echo "$FACT_RESTORE" > "$TMP/fact-restored.json"
[[ "$FACT_RESTORE" == "$FACT_CURRENT" ]] || {
  echo "ERROR: accepted facts drifted across prior -> current" >>"$LOG"
  exit 1
}
stop_server "$RESTORE_PID"

# --- Window end + seal + verify ----------------------------------------------
WINDOW_END_UTC="$("$PY" -c 'import datetime; print(datetime.datetime.now(datetime.timezone.utc).isoformat())')"
echo "window_end_utc=$WINDOW_END_UTC" >>"$LOG"

step "9/10 window: seal observation bundle + public verifier"
env PYTHONSAFEPATH=1 PYTHONPATH="$TMP/site:$ROOT" \
  TASK4_S01_STATE_PATH="$STATE_PATH" \
  TASK4_S02_REGISTRY_PATH="$TMP/s02-registry.json" \
  TASK4_S02_OBJECT_ROOT="$S02_OBJECT_ROOT" \
  "$PY" -P - "$OBS_DIR" "$BUNDLE_DIR" "$WINDOW_ID" "$CURRENT_SHA" "$PRIOR_SHA" \
  "$WINDOW_START_UTC" "$WINDOW_END_UTC" "$TMP" <<'SEALPY' 2>&1 >>"$LOG"
import json
import hashlib
import os
import sys
from datetime import datetime
from pathlib import Path

from task4_consistency.web import observation as obs
import task4_consistency.web.app as web

obs_dir, bundle_dir, window_id, current_sha, prior_sha = sys.argv[1:6]
window_start, window_end = sys.argv[6], sys.argv[7]
tmp_root = Path(sys.argv[8])

requests_raw = (Path(obs_dir) / "requests.jsonl").read_bytes()
lifecycle_raw = (Path(obs_dir) / "process-lifecycle.jsonl").read_bytes()
records = [json.loads(line) for line in requests_raw.decode("utf-8").splitlines()]
lifecycle = [json.loads(line) for line in lifecycle_raw.decode("utf-8").splitlines()]
process_ids = sorted({entry["process_id"] for entry in lifecycle})
assert process_ids, "no observed processes"
family_table = obs.app_family_table(web.app)

node_ids = (tmp_root / "cohort-node-ids.txt").read_text(encoding="utf-8").splitlines()
spec_hashes = {}
for line in (tmp_root / "cohort-spec-sha256.txt").read_text(encoding="utf-8").splitlines():
    digest, path = line.split(maxsplit=1)
    spec_hashes[path.lstrip(" *")] = digest
fact_hashes = {
    stage: hashlib.sha256((tmp_root / filename).read_bytes()).hexdigest()
    for stage, filename in {
        "current": "fact-current.json",
        "prior": "fact-prior.json",
        "restored": "fact-restored.json",
    }.items()
}
process_artifacts = {}
for process_id in process_ids:
    process_records = [
        record for record in records if record["process_id"] == process_id
    ]
    classes = {
        record["traffic_class"]
        for record in process_records
        if record["traffic_class"] != "health"
    }
    assert len(classes) <= 1, (process_id, classes)
    traffic_class = next(iter(classes), "release")
    stage = "prior" if process_id == "t54-rollback-prior" else "current"
    process_artifacts[process_id] = {
        "artifact_sha256": prior_sha if stage == "prior" else current_sha,
        "artifact_stage": stage,
        "traffic_class": traffic_class,
    }
elapsed_seconds = (
    datetime.fromisoformat(window_end) - datetime.fromisoformat(window_start)
).total_seconds()
release_evidence = {
    "reviewed_commit": os.environ["REVIEWED_COMMIT"],
    "tracked_tree_clean": True,
    "current_wheel_sha256": current_sha,
    "prior_commit": os.environ["T54_FIXED_BASE"],
    "prior_wheel_sha256": prior_sha,
    "timezone": (tmp_root / "window-timezone.txt").read_text(encoding="utf-8").strip(),
    "elapsed_seconds": elapsed_seconds,
    "node_version": os.environ["NODE_VERSION"],
    "npm_version": os.environ["NPM_VERSION"],
    "package_identity": os.environ["PACKAGE_IDENTITY"],
    "network_routes": (tmp_root / "network-routes.txt").read_text(encoding="utf-8").strip(),
    "cohort_node_ids": node_ids,
    "cohort_node_ids_sha256": hashlib.sha256(
        ("\n".join(node_ids) + "\n").encode("utf-8")
    ).hexdigest(),
    "cohort_spec_sha256": spec_hashes,
    "viewports": ["1280x800", "390x844"],
    "accepted_fact_sha256": fact_hashes,
    "accepted_facts_equal": len(set(fact_hashes.values())) == 1,
}

manifest = obs.build_bundle(
    bundle_dir,
    requests_raw=requests_raw,
    lifecycle_raw=lifecycle_raw,
    window_id=window_id,
    artifact_sha256=current_sha,
    process_id="t54-window",
    process_class="release",
    window_start_utc=window_start,
    window_end_utc=window_end,
    environment_identity=obs.default_environment_identity(),
    cohort=process_ids,
    family_table=family_table,
    prior_artifact={"wheel_sha256": prior_sha, "commit": os.environ["T54_FIXED_BASE"]},
    process_artifacts=process_artifacts,
    release_evidence=release_evidence,
)
# Move the sealed bundle to the run-owned evidence dir under the
# acceptance-command names BEFORE verification, so raw evidence survives
# any window failure for diagnosis.
for name in ("requests.jsonl", "process-lifecycle.jsonl"):
    Path(bundle_dir, name).rename(Path(bundle_dir).parent / name)
Path(bundle_dir, "manifest.json").rename(Path(bundle_dir).parent / "window-manifest.json")
verdict = obs.verify_bundle(Path(bundle_dir).parent / "window-manifest.json")
if not verdict.valid:
    for record in records:
        if record["normalized_path_family"] == obs.UNREGISTERED_PATH_FAMILY:
            print("UNREGISTERED_FAMILY:", record["method"], record["normalized_path_family"],
                  "owner=", record["matched_route_owner"], "status=", record["response_status"],
                  "class=", record["traffic_class"], flush=True)
    raise AssertionError(f"window invalid: {verdict.reason}")
assert verdict.acceptance is not None and verdict.acceptance.zero_caller_ok, (
    f"zero-caller acceptance failed: {verdict.acceptance}"
)
print(json.dumps({
    "valid": verdict.valid,
    "requests": verdict.line_count,
    "per_class": verdict.per_traffic_class_counts,
    "per_entry": verdict.per_entry_counts,
    "operator_catalog_hits": verdict.acceptance.operator_catalog_hits,
    "rollback_probe_catalog_hits": verdict.acceptance.rollback_probe_catalog_hits,
    "processes": process_ids,
}, sort_keys=True))
SEALPY

echo "window OK" >>"$LOG"
echo 0 > "$TMP/t54-window-status"
BODY

WINDOW_PIDS=""
set +e
unshare --user --map-root-user --net \
  env ROOT="$ROOT" TMP="$TMP" LOG="$LOG" PY="$PY" PIP="$PIP" \
  PLAYWRIGHT_OUT="$PLAYWRIGHT_OUT" \
  LOG_DIR="$LOG_DIR" WINDOW_ID="$WINDOW_ID" OBS_DIR="$OBS_DIR" \
  BUNDLE_DIR="$BUNDLE_DIR" STATE_PATH="$STATE_PATH" CURRENT_SHA="$CURRENT_SHA" \
  PRIOR_SHA="$PRIOR_SHA" S02_OBJECT_ROOT="$S02_OBJECT_ROOT" \
  REVIEWED_COMMIT="$REVIEWED_COMMIT" T54_FIXED_BASE="$T54_FIXED_BASE" \
  NODE_VERSION="$NODE_VERSION" NPM_VERSION="$NPM_VERSION" PACKAGE_IDENTITY="$PACKAGE_IDENTITY" \
  bash "$TMP/t54-window-body.sh" 2>&1 | tee -a "$LOG"
WINDOW_EXIT=${PIPESTATUS[0]}
set -e
echo "window_exit=$WINDOW_EXIT" | tee -a "$LOG"
if [[ "$WINDOW_EXIT" -ne 0 ]]; then
  echo "ERROR: controlled runtime window failed" | tee -a "$LOG"
  exit 1
fi
if [[ ! -f "$TMP/t54-window-status" || "$(cat "$TMP/t54-window-status")" != "0" ]]; then
  echo "ERROR: controlled runtime window did not complete" | tee -a "$LOG"
  exit 1
fi

# Steps 10 run unconditionally from the EXIT trap (artifacts, conservation,
# uvicorn stop, temporary-root removal, verdict).
echo "harness steps complete" | tee -a "$LOG"
