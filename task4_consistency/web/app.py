"""FastAPI demo: check applications, edit rules, maintain entity KB."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

import yaml
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.base import BaseHTTPMiddleware

from task4_consistency.audit import audit_log_path, audit_status, read_audit_tail, write_audit
from task4_consistency.kb.store import get_kb, reload_kb

from task4_consistency.models import Application
from task4_consistency.report import report_to_html
from task4_consistency.rules.critical_guard import (
    CriticalGuardError,
    enforce_critical_fingerprints,
    fingerprints_as_dicts,
)
from task4_consistency.rules.engine import RuleEngine
from task4_consistency.rules.loader import load_rules

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RULES = ROOT / "configs" / "rules_auto_lease.yaml"
RUNTIME_RULES = ROOT / "configs" / "runtime_rules.yaml"
FIXTURES = ROOT / "fixtures" / "applications"
STATIC = Path(__file__).resolve().parent / "static"
TEMPLATES = Path(__file__).resolve().parent / "templates"
_KB_SECTIONS = {"address_aliases", "org_aliases", "plate_prefixes"}
# ARCH Round16 W1: serialize put/reset; no concurrent half-writes
RULES_WRITE_LOCK = threading.Lock()

app = FastAPI(title="Task4 Consistency Demo", version="1.0.0")
if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


class OptionalTokenAuth(BaseHTTPMiddleware):
    """If TASK4_WEB_TOKEN set: require Authorization: Bearer <token> or X-Task4-Token.
    Unset token → open demo mode (no auth).
    """

    _PUBLIC_PREFIXES = ("/static",)
    _PUBLIC_EXACT = {"/api/health"}

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        token = os.environ.get("TASK4_WEB_TOKEN", "").strip()
        if not token:
            return await call_next(request)
        path = request.url.path
        if path in self._PUBLIC_EXACT or any(path.startswith(p) for p in self._PUBLIC_PREFIXES):
            return await call_next(request)
        # UI shell open; APIs protected (except health)
        if path == "/":
            return await call_next(request)
        provided = request.headers.get("X-Task4-Token") or ""
        auth = request.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            provided = auth[7:].strip()
        if provided != token:
            write_audit(
                "auth_denied",
                actor="web",
                ok=False,
                detail={"path": path, "method": request.method},
            )
            return JSONResponse(
                status_code=401,
                content={
                    "detail": {
                        "error": "unauthorized",
                        "message": "需要有效 TASK4_WEB_TOKEN",
                        "hint": "Header: Authorization: Bearer <token> 或 X-Task4-Token",
                    }
                },
            )
        return await call_next(request)


app.add_middleware(OptionalTokenAuth)


def _quarantine_bad_runtime(err: Exception) -> None:
    """ADV-W1 self-heal: move poisoned runtime aside so default package reactivates."""
    if not RUNTIME_RULES.exists():
        return
    bad = RUNTIME_RULES.with_suffix(".yaml.bad")
    try:
        if bad.exists():
            bad.unlink()
        RUNTIME_RULES.replace(bad)
    except OSError:
        try:
            RUNTIME_RULES.unlink()
        except OSError:
            pass
    write_audit(
        "rules_auto_heal",
        ok=True,
        detail={"error": str(err), "quarantined": _rel_to_root(bad) if bad.exists() else None},
    )


def _active_rules_path() -> Path:
    if RUNTIME_RULES.exists():
        try:
            load_rules(RUNTIME_RULES)
            return RUNTIME_RULES
        except Exception as e:
            _quarantine_bad_runtime(e)
    return DEFAULT_RULES


def _rel_to_root(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT.resolve()))
    except ValueError:
        return str(path)


def _engine() -> RuleEngine:
    return RuleEngine(load_rules(_active_rules_path()))


def _parse_rules_payload(body: "RulesBody") -> tuple[dict[str, Any], str]:
    """Return (data, yaml_text). Raises HTTPException 400 with clear tip."""
    if body.yaml_text is not None:
        text = body.yaml_text
        if not str(text).strip():
            raise HTTPException(
                400,
                detail={
                    "error": "empty_yaml",
                    "message": "规则 YAML 为空，请粘贴完整规则包后再保存",
                    "hint": "至少包含 package / version / rules 列表",
                },
            )
        try:
            data = yaml.safe_load(text) or {}
        except yaml.YAMLError as e:
            raise HTTPException(
                400,
                detail={
                    "error": "invalid_yaml",
                    "message": f"YAML 语法错误: {e}",
                    "hint": "检查缩进、冒号、引号；可用在线 YAML 校验",
                },
            ) from e
        if not isinstance(data, dict):
            raise HTTPException(
                400,
                detail={
                    "error": "yaml_not_mapping",
                    "message": "规则根节点必须是 mapping/object",
                    "hint": "顶层应是 package: ... rules: [...] 结构",
                },
            )
        return data, text
    if body.content is not None:
        if not isinstance(body.content, dict):
            raise HTTPException(
                400,
                detail={
                    "error": "content_not_object",
                    "message": "content 必须是 JSON 对象",
                },
            )
        data = body.content
        yaml_text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
        return data, yaml_text
    raise HTTPException(
        400,
        detail={
            "error": "missing_body",
            "message": "需要 content 或 yaml_text",
            "hint": "PUT /api/rules  body: {\"yaml_text\": \"...\"}",
        },
    )


def _validate_rules_yaml(yaml_text: str) -> Any:
    """Load rules via temp file; never touch runtime path.

    load_rules already runs schema + package policy + critical fingerprints.
    """
    with tempfile.NamedTemporaryFile(
        "w", suffix=".yaml", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(yaml_text)
        tmp = Path(fh.name)
    try:
        cfg = load_rules(tmp)
        # explicit re-assert (load_rules already enforces; keep for clarity)
        enforce_critical_fingerprints(cfg)
        return cfg
    except CriticalGuardError as e:
        raise HTTPException(
            400,
            detail={
                "error": e.error,
                "message": str(e),
                "hint": "critical 三剑客 R_VIN_CROSS/R_ENGINE_CROSS/R_ID_EXACT 语义指纹不可改（见 CONFIG_GUIDE）",
            },
        ) from e
    except Exception as e:
        err = "rules_schema_invalid"
        msg = str(e)
        if "ADV-W" in msg or "rel_tol" in msg or "field_aliases" in msg:
            err = "rules_policy_invalid"
        raise HTTPException(
            400,
            detail={
                "error": err,
                "message": f"规则校验失败: {e}",
                "hint": "检查 rules[].id/type/field；critical 指纹与 policy 护栏",
            },
        ) from e
    finally:
        tmp.unlink(missing_ok=True)


class CheckBody(BaseModel):
    application: dict[str, Any]
    rules_path: str | None = None


class RulesBody(BaseModel):
    content: dict[str, Any] | None = None
    yaml_text: str | None = None


class KBItem(BaseModel):
    section: str = Field(description="address_aliases | org_aliases | plate_prefixes")
    key: str
    value: str

    @field_validator("section")
    @classmethod
    def _section_ok(cls, v: str) -> str:
        s = str(v).strip()
        if s not in _KB_SECTIONS:
            raise ValueError(
                f"section 必须是 {sorted(_KB_SECTIONS)} 之一，收到: {v!r}"
            )
        return s

    @field_validator("key", "value")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        s = str(v).strip()
        if not s:
            raise ValueError("key/value 不能为空")
        return s


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    html = (TEMPLATES / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/api/health")
def health() -> dict[str, Any]:
    """Liveness + config pointers (Round22: rules_path / kb_ok / version / audit)."""
    p = _active_rules_path()
    cfg = load_rules(p)
    token_on = bool(os.environ.get("TASK4_WEB_TOKEN", "").strip())
    kb_ok = False
    kb_err: str | None = None
    try:
        kb = get_kb()
        _ = kb.list_section("org_aliases")
        kb_ok = True
    except Exception as e:
        kb_err = str(e)
    astat = audit_status()
    ok = kb_ok
    try:
        from task4_consistency import __version__ as lib_version
    except Exception:
        lib_version = "unknown"
    return {
        "ok": ok,
        "rules_path": _rel_to_root(p),
        "package": cfg.package,
        "version": cfg.version,  # rules YAML package version
        "lib_version": lib_version,  # task4_consistency / pyproject version
        "kb_ok": kb_ok,
        "kb_error": kb_err,
        "auth_required": token_on,
        "audit": {
            "path": astat["path"],
            "exists": astat["exists"],
            "size_bytes": astat["size_bytes"],
        },
        "audit_log": str(audit_log_path()),
    }


@app.get("/api/audit/recent")
def audit_recent(limit: int = 20) -> dict[str, Any]:
    """Tail audit JSONL for ops readability (demo; open unless TASK4_WEB_TOKEN)."""
    limit = max(1, min(int(limit or 20), 200))
    events = read_audit_tail(limit)
    return {
        "path": str(audit_log_path()),
        "n": len(events),
        "events": events,
    }



@app.get("/api/fixtures")
def list_fixtures() -> dict[str, Any]:
    items = []
    for fp in sorted(FIXTURES.glob("*.json")):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
            items.append(
                {
                    "file": fp.name,
                    "application_id": data.get("application_id"),
                    "label": data.get("label"),
                    "step2_sample_id": meta.get("step2_sample_id"),
                    "field_source": meta.get("field_source"),
                }
            )
        except Exception:
            items.append({"file": fp.name, "application_id": None, "label": None})
    return {"fixtures": items}


@app.get("/api/step2/samples")
def list_step2_samples() -> dict[str, Any]:
    """List competition-side page_order extractions (bboxes, no OCR text)."""
    step2_dir = ROOT / "data" / "step2"
    items: list[dict[str, Any]] = []
    if not step2_dir.is_dir():
        return {"samples": [], "note": "data/step2 missing"}
    for fp in sorted(step2_dir.glob("*_page_order.json")):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            sid = data.get("sample_id") or fp.name.replace("_page_order.json", "")
            stats = data.get("statistics") or {}
            # fixtures linked to this sample
            linked = []
            for fx in sorted(FIXTURES.glob("*.json")):
                try:
                    app = json.loads(fx.read_text(encoding="utf-8"))
                    meta = app.get("meta") if isinstance(app.get("meta"), dict) else {}
                    if meta.get("step2_sample_id") == sid:
                        linked.append(fx.name)
                except Exception:
                    pass
            items.append(
                {
                    "sample_id": sid,
                    "file": fp.name,
                    "n_pages": len(data.get("pages") or []),
                    "page_type_counts": stats.get("page_type_counts") or {},
                    "linked_fixtures": linked[:12],
                    "n_linked_fixtures": len(linked),
                }
            )
        except Exception as e:
            items.append({"file": fp.name, "error": str(e)})
    return {
        "samples": items,
        "note": "step2 来自赛题影像的页序/检测框提取，无 OCR 文本；任务4演示用结构化字段模拟多单据交叉。",
    }


@app.get("/api/ocr_inbox")
def list_ocr_inbox() -> dict[str, Any]:
    """List step2→OCR slot manifests (raw usually null until external OCR)."""
    inbox = ROOT / "fixtures" / "ocr_inbox"
    items: list[dict[str, Any]] = []
    if inbox.is_dir():
        for fp in sorted(inbox.glob("step2_slots_*.json")):
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
                slots = data.get("slots") or []
                filled = sum(1 for s in slots if s.get("raw"))
                items.append(
                    {
                        "file": fp.name,
                        "sample_id": data.get("sample_id"),
                        "n_slots": data.get("n_slots") or len(slots),
                        "n_filled": filled,
                        "note": data.get("note"),
                    }
                )
            except Exception as e:
                items.append({"file": fp.name, "error": str(e)})
    return {
        "items": items,
        "note": "raw 为空表示待外部 OCR；见 docs/STEP2_TO_TASK4_PIPELINE.md",
    }


@app.get("/api/step2/{sample_id}")
def get_step2_sample(sample_id: str) -> dict[str, Any]:
    if "/" in sample_id or ".." in sample_id:
        raise HTTPException(400, "invalid sample_id")
    fp = ROOT / "data" / "step2" / f"{sample_id}_page_order.json"
    if not fp.exists():
        raise HTTPException(404, "step2 sample not found")
    data = json.loads(fp.read_text(encoding="utf-8"))
    # compact detections for UI
    pages_out = []
    for p in data.get("pages") or []:
        classes = sorted(
            {
                d.get("class_name_cn")
                for d in (p.get("detections") or [])
                if d.get("class_name_cn")
            }
        )
        pages_out.append(
            {
                "order": p.get("order"),
                "filename": p.get("filename"),
                "page_type": p.get("page_type"),
                "page_numbers": p.get("page_numbers"),
                "detected_fields": classes,
            }
        )
    return {
        "sample_id": data.get("sample_id") or sample_id,
        "pages": pages_out,
        "statistics": data.get("statistics"),
        "note": "检测框类别可用于理解登记证上有哪些字段区域；跨单据一致性仍需多源结构化值。",
    }


@app.get("/api/fixtures/{name}")
def get_fixture(name: str) -> dict[str, Any]:
    if "/" in name or ".." in name:
        raise HTTPException(400, "invalid name")
    fp = FIXTURES / name
    if not fp.exists():
        raise HTTPException(404, "fixture not found")
    return json.loads(fp.read_text(encoding="utf-8"))


@app.post("/api/check")
def api_check(body: CheckBody) -> dict[str, Any]:
    if not isinstance(body.application, dict):
        raise HTTPException(
            400,
            detail={
                "error": "invalid_application_type",
                "message": "application 必须是 JSON 对象",
                "hint": "顶层字段: application_id, documents[], 可选 expected_verdicts",
            },
        )
    if "documents" not in body.application:
        raise HTTPException(
            400,
            detail={
                "error": "missing_documents",
                "message": "application.documents 缺失",
                "hint": "documents 为单据数组，每项含 doc_type 与 fields",
            },
        )
    try:
        app_obj = Application.from_dict(body.application)
    except Exception as e:
        raise HTTPException(
            400,
            detail={
                "error": "invalid_application",
                "message": f"申请单解析失败: {e}",
                "hint": "检查 documents[].fields 值是否为 {{raw, confidence}} 或纯字符串",
            },
        ) from e
    rules_path = Path(body.rules_path) if body.rules_path else _active_rules_path()
    if not rules_path.is_absolute():
        rules_path = ROOT / rules_path
    if not rules_path.exists():
        raise HTTPException(
            400,
            detail={
                "error": "rules_not_found",
                "message": f"规则文件不存在: {rules_path}",
                "hint": "省略 rules_path 使用当前激活规则包",
            },
        )
    try:
        eng = RuleEngine(load_rules(rules_path))
        report = eng.run(app_obj)
    except Exception as e:
        raise HTTPException(
            500,
            detail={
                "error": "check_failed",
                "message": f"校验执行失败: {e}",
            },
        ) from e
    return {
        "report": report.to_dict(),
        "html": report_to_html(report),
        "rules_path": _rel_to_root(rules_path),
    }


# Demo batch check soft cap (Arch Round26: no job queue / no async evaluate batch)
BATCH_CHECK_MAX_N = 50


class BatchCheckBody(BaseModel):
    """Run check on multiple applications (inline or fixture filenames).

    Soft limit: BATCH_CHECK_MAX_N (default 50). For full labeled metrics use CLI
    ``evaluate --suite main`` — there is no ``/api/evaluate/batch`` or job queue.
    """

    applications: list[dict[str, Any]] | None = None
    fixture_files: list[str] | None = None


@app.post("/api/check/batch")
def api_check_batch(body: BatchCheckBody) -> dict[str, Any]:
    apps: list[dict[str, Any]] = list(body.applications or [])
    for name in body.fixture_files or []:
        if "/" in name or ".." in name:
            raise HTTPException(400, f"invalid fixture name: {name}")
        fp = FIXTURES / name
        if not fp.exists():
            raise HTTPException(404, f"fixture not found: {name}")
        apps.append(json.loads(fp.read_text(encoding="utf-8")))
    if not apps:
        raise HTTPException(400, "applications or fixture_files required")
    if len(apps) > BATCH_CHECK_MAX_N:
        raise HTTPException(
            400,
            detail={
                "error": "batch_too_large",
                "message": f"batch size {len(apps)} exceeds max {BATCH_CHECK_MAX_N}",
                "hint": (
                    f"拆分批次（建议 ≤{BATCH_CHECK_MAX_N}）；"
                    "全量带标签评估请用 CLI: "
                    "python -m task4_consistency evaluate --suite main -c configs/rules_auto_lease.yaml"
                    "（无 /api/evaluate/batch，无异步 job 队列）"
                ),
                "max_n": BATCH_CHECK_MAX_N,
            },
        )

    eng = _engine()
    results = []
    tot = {"consistent": 0, "inconsistent": 0, "uncertain": 0, "skipped": 0}
    for raw in apps:
        try:
            app_obj = Application.from_dict(raw)
            report = eng.run(app_obj)
            s = report.summary
            tot["consistent"] += s.consistent
            tot["inconsistent"] += s.inconsistent
            tot["uncertain"] += s.uncertain
            tot["skipped"] += s.skipped
            fails = [
                {
                    "rule_id": c.rule_id,
                    "verdict": c.verdict.value,
                    "message": c.message,
                    "reason_codes": list(c.reason_codes or []),
                }
                for c in report.checks
                if c.verdict.value in ("inconsistent", "uncertain")
            ]
            results.append(
                {
                    "application_id": report.application_id,
                    "summary": s.to_dict(),
                    "issues": fails,
                }
            )
        except Exception as e:
            results.append(
                {
                    "application_id": raw.get("application_id"),
                    "error": str(e),
                }
            )
    return {
        "n": len(results),
        "totals": tot,
        "results": results,
        "rules_path": _rel_to_root(_active_rules_path()),
    }


@app.get("/api/evaluate/summary")
def api_evaluate_summary(suite: str = "main") -> dict[str, Any]:
    """Run evaluate by suite (default main). Round19/20 honesty: only main is delivery."""
    from task4_consistency.evaluate import evaluate_suite, metrics_to_html

    suite = (suite or "main").lower()
    if suite not in {"main", "semi", "all"}:
        raise HTTPException(
            400,
            detail={
                "error": "bad_suite",
                "message": f"suite must be main|semi|all, got {suite!r}",
                "hint": "交付数字只用 suite=main",
            },
        )
    metrics = evaluate_suite(suite, _active_rules_path())
    return {
        "metrics": metrics.to_dict(),
        "html": metrics_to_html(metrics),
        "rules_path": _rel_to_root(_active_rules_path()),
        "suite": suite,
    }


@app.get("/api/rules")
def get_rules() -> dict[str, Any]:
    path = _active_rules_path()
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    return {
        "path": _rel_to_root(path),
        "is_runtime": path == RUNTIME_RULES,
        "yaml_text": text,
        "content": data,
    }


@app.post("/api/rules/validate")
def validate_rules(body: RulesBody) -> dict[str, Any]:
    """Dry-run rule package validation without writing runtime_rules.yaml."""
    _data, yaml_text = _parse_rules_payload(body)
    cfg = _validate_rules_yaml(yaml_text)
    return {
        "ok": True,
        "package": cfg.package,
        "version": cfg.version,
        "n_rules": len(cfg.rules),
        "critical_fingerprints": fingerprints_as_dicts(),
        "message": "规则校验通过（未写入磁盘）",
    }


@app.put("/api/rules")
def put_rules(body: RulesBody) -> dict[str, Any]:
    """Atomic save: validate+fingerprint first; then lock → tmp+fsync+replace.

    ARCH Round16 W1: on any failure **never touch** active runtime_rules.yaml
    (no write_text rollback).
    """
    _data, yaml_text = _parse_rules_payload(body)
    # full validation BEFORE lock / BEFORE any write near active path
    cfg = _validate_rules_yaml(yaml_text)

    RUNTIME_RULES.parent.mkdir(parents=True, exist_ok=True)
    if not RUNTIME_RULES.exists() and DEFAULT_RULES.exists():
        bak = ROOT / "configs" / "rules_auto_lease.yaml.bak"
        if not bak.exists():
            shutil.copy2(DEFAULT_RULES, bak)

    tmp_path = RUNTIME_RULES.with_suffix(".yaml.tmp")
    with RULES_WRITE_LOCK:
        try:
            # write only to sibling tmp; fsync; then atomic replace
            with open(tmp_path, "w", encoding="utf-8") as fh:
                fh.write(yaml_text)
                fh.flush()
                os.fsync(fh.fileno())
            # re-validate from the bytes about to become active
            cfg2 = load_rules(tmp_path)
            enforce_critical_fingerprints(cfg2)
            os.replace(tmp_path, RUNTIME_RULES)
            cfg = cfg2
        except HTTPException:
            tmp_path.unlink(missing_ok=True)
            raise
        except CriticalGuardError as e:
            tmp_path.unlink(missing_ok=True)
            write_audit(
                "rules_save",
                ok=False,
                detail={"error": e.error, "message": str(e)},
            )
            raise HTTPException(
                400,
                detail={
                    "error": e.error,
                    "message": str(e),
                    "hint": "critical 指纹未通过；active runtime 未改动",
                },
            ) from e
        except Exception as e:
            # failure: zero touch active (delete tmp only)
            tmp_path.unlink(missing_ok=True)
            write_audit(
                "rules_save",
                ok=False,
                detail={"error": "rules_save_failed", "message": str(e)},
            )
            raise HTTPException(
                400,
                detail={
                    "error": "rules_save_failed",
                    "message": f"规则保存失败（active 未改动）: {e}",
                    "hint": "修复 YAML 后重试；当前仍使用上一版 runtime 或默认包",
                },
            ) from e

    write_audit(
        "rules_save",
        ok=True,
        detail={
            "path": _rel_to_root(RUNTIME_RULES),
            "package": cfg.package,
            "version": cfg.version,
            "n_rules": len(cfg.rules),
        },
    )
    return {
        "ok": True,
        "path": _rel_to_root(RUNTIME_RULES),
        "package": cfg.package,
        "version": cfg.version,
        "n_rules": len(cfg.rules),
        "message": "规则已保存并通过校验与 critical 指纹",
    }


@app.post("/api/rules/reset")
def reset_rules() -> dict[str, Any]:
    with RULES_WRITE_LOCK:
        existed = RUNTIME_RULES.exists()
        if existed:
            RUNTIME_RULES.unlink()
    write_audit("rules_reset", ok=True, detail={"had_runtime": existed})
    return {"ok": True, "active": _rel_to_root(_active_rules_path())}


@app.get("/api/kb/graph")
def kb_graph() -> dict[str, Any]:
    """Lightweight entity graph (synonym/part_of) for demo + future linking."""
    data = get_kb().to_dict()
    g = data.get("graph") if isinstance(data, dict) else None
    if not g:
        return {"graph": {"nodes": [], "edges": []}, "note": "no graph section"}
    return {"graph": g, "note": "same_as 边会投影到 address/org 别名供 normalize 使用"}


@app.get("/api/kb")
def kb_get() -> dict[str, Any]:
    return get_kb().to_dict()


@app.post("/api/kb")
def kb_add(item: KBItem) -> dict[str, Any]:
    try:
        get_kb().add_alias(item.section, item.key, item.value)
    except KeyError as e:
        raise HTTPException(
            400,
            detail={
                "error": "unknown_section",
                "message": str(e),
                "hint": f"section 仅支持 {sorted(_KB_SECTIONS)}",
            },
        ) from e
    except ValueError as e:
        raise HTTPException(
            400,
            detail={
                "error": "invalid_kb_item",
                "message": str(e),
                "hint": "key 与 value 均需非空字符串",
            },
        ) from e
    reload_kb()
    write_audit(
        "kb_add",
        ok=True,
        detail={"section": item.section, "key": item.key, "value": item.value},
    )
    return {"ok": True, "kb": get_kb().to_dict(), "message": "别名已添加"}


@app.delete("/api/kb/{section}/{key}")
def kb_delete(section: str, key: str) -> dict[str, Any]:
    if section not in _KB_SECTIONS:
        raise HTTPException(
            400,
            detail={
                "error": "unknown_section",
                "message": f"未知 section: {section}",
                "hint": f"section 仅支持 {sorted(_KB_SECTIONS)}",
            },
        )
    ok = get_kb().remove_alias(section, key)
    if not ok:
        write_audit(
            "kb_delete",
            ok=False,
            detail={"section": section, "key": key, "error": "not_found"},
        )
        raise HTTPException(
            404,
            detail={
                "error": "kb_key_not_found",
                "message": f"未找到 {section}/{key}",
                "hint": "先 GET /api/kb 查看现有别名",
            },
        )
    reload_kb()
    write_audit("kb_delete", ok=True, detail={"section": section, "key": key})
    return {"ok": True, "kb": get_kb().to_dict(), "message": "别名已删除"}


@app.post("/api/kb/reload")
def kb_reload() -> dict[str, Any]:
    kb = reload_kb()
    return {"ok": True, "kb": kb.to_dict()}


def create_app() -> FastAPI:
    return app
