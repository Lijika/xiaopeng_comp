"""JSONL audit log for mutable ops (rules / KB).

Round43 (Arch 终裁 B): envelope schema_ver=1.
Old lines without schema_ver are treated as legacy (schema_ver=0) on read.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_LOG = _ROOT / "out" / "audit.log"
_lock = threading.Lock()

# Arch Round43 envelope
AUDIT_SCHEMA_VER = 1


def audit_log_path() -> Path:
    override = os.environ.get("TASK4_AUDIT_LOG")
    return Path(override) if override else _DEFAULT_LOG


def write_audit(
    action: str,
    *,
    actor: str = "web",
    detail: dict[str, Any] | None = None,
    ok: bool = True,
) -> bool:
    """Append one JSONL event with schema_ver=1 envelope.

    Never raises to callers (best-effort). Does not validate detail shape.
    """
    rec = {
        "schema_ver": AUDIT_SCHEMA_VER,
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": str(action),
        "actor": str(actor),
        "ok": bool(ok),
        "detail": detail if isinstance(detail, dict) else {},
    }
    path = audit_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        with _lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(line)
        return True
    except OSError:
        return False


def audit_status() -> dict[str, Any]:
    """Health-friendly audit path status (does not write)."""
    path = audit_log_path()
    parent = path.parent
    return {
        "path": str(path),
        "exists": path.is_file(),
        "parent_writable": parent.exists() and os.access(parent, os.W_OK)
        if parent.exists()
        else parent.parent.exists(),
        "size_bytes": path.stat().st_size if path.is_file() else 0,
        "schema_ver_current": AUDIT_SCHEMA_VER,
    }


def normalize_audit_record(rec: dict[str, Any]) -> dict[str, Any]:
    """Normalize legacy (no schema_ver) and v1 records for readers."""
    out = dict(rec)
    if "schema_ver" not in out:
        out["schema_ver"] = 0  # legacy
    else:
        try:
            out["schema_ver"] = int(out["schema_ver"])
        except (TypeError, ValueError):
            out["schema_ver"] = 0
    out.setdefault("action", "")
    out.setdefault("actor", "")
    out.setdefault("ok", True)
    if not isinstance(out.get("detail"), dict):
        out["detail"] = {}
    return out


def read_audit_tail(n: int = 20) -> list[dict[str, Any]]:
    """Read last n JSONL audit events (best-effort). Compatible with pre-v1 lines."""
    path = audit_log_path()
    if not path.is_file() or n <= 0:
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines[-n:]:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            if isinstance(rec, dict):
                out.append(normalize_audit_record(rec))
        except json.JSONDecodeError:
            continue
    return out
