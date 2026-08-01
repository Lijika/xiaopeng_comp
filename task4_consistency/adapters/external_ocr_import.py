"""Import offline external OCR intermediate JSON → Application (Round19).

No OCR engine. Schema is the only ingress for field_source=external_ocr.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from task4_consistency.models import Application, Document, FieldValue

SCHEMA_VERSION = 1
MAX_BYTES = 2 * 1024 * 1024  # 2MB


class ExternalOcrImportError(ValueError):
    """Invalid intermediate OCR JSON."""

    def __init__(self, error: str, message: str):
        self.error = error
        super().__init__(message)


def validate_external_ocr_payload(data: Any) -> dict[str, Any]:
    """Validate intermediate schema; return the same dict if ok."""
    if not isinstance(data, dict):
        raise ExternalOcrImportError("invalid_root", "root must be a JSON object")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ExternalOcrImportError(
            "bad_schema_version",
            f"schema_version must be {SCHEMA_VERSION}, got {data.get('schema_version')!r}",
        )
    ocr_model = data.get("ocr_model")
    ocr_version = data.get("ocr_version")
    if not isinstance(ocr_model, str) or not ocr_model.strip():
        raise ExternalOcrImportError("missing_ocr_model", "ocr_model required non-empty string")
    if not isinstance(ocr_version, str) or not ocr_version.strip():
        raise ExternalOcrImportError(
            "missing_ocr_version", "ocr_version required non-empty string"
        )
    app_id = data.get("application_id")
    if not isinstance(app_id, str) or not app_id.strip():
        raise ExternalOcrImportError("missing_application_id", "application_id required")
    docs = data.get("documents")
    if not isinstance(docs, list) or len(docs) < 1:
        raise ExternalOcrImportError("bad_documents", "documents must be non-empty list")
    for i, doc in enumerate(docs):
        if not isinstance(doc, dict):
            raise ExternalOcrImportError("bad_document", f"documents[{i}] must be object")
        if not str(doc.get("doc_id") or "").strip():
            raise ExternalOcrImportError("missing_doc_id", f"documents[{i}].doc_id required")
        if not str(doc.get("doc_type") or "").strip():
            raise ExternalOcrImportError("missing_doc_type", f"documents[{i}].doc_type required")
        fields = doc.get("fields")
        if fields is None:
            fields = {}
        if not isinstance(fields, dict):
            raise ExternalOcrImportError("bad_fields", f"documents[{i}].fields must be object")
        for fname, fval in fields.items():
            if not isinstance(fname, str):
                raise ExternalOcrImportError("bad_field_key", f"field key must be str at [{i}]")
            if fval is None:
                continue
            if isinstance(fval, str):
                continue
            if not isinstance(fval, dict):
                raise ExternalOcrImportError(
                    "bad_field_value",
                    f"documents[{i}].fields.{fname} must be str|null|object",
                )
            if "raw" not in fval:
                raise ExternalOcrImportError(
                    "missing_raw",
                    f"documents[{i}].fields.{fname}.raw required (may be null)",
                )
            raw = fval.get("raw")
            if raw is not None and not isinstance(raw, str):
                raise ExternalOcrImportError(
                    "bad_raw",
                    f"documents[{i}].fields.{fname}.raw must be str|null",
                )
            if "confidence" in fval and fval["confidence"] is not None:
                conf = float(fval["confidence"])
                if conf < 0.0 or conf > 1.0:
                    raise ExternalOcrImportError(
                        "bad_confidence",
                        f"documents[{i}].fields.{fname}.confidence must be in [0,1]",
                    )
    return data


def external_ocr_to_application(
    data: dict[str, Any],
    *,
    demo_note: str | None = None,
) -> Application:
    """Convert validated payload → Application with forced meta."""
    data = validate_external_ocr_payload(data)
    docs: list[Document] = []
    for d in data["documents"]:
        fields: dict[str, FieldValue] = {}
        for name, val in (d.get("fields") or {}).items():
            if isinstance(val, str) or val is None:
                fields[name] = FieldValue(raw=val)
            else:
                conf = float(val.get("confidence", 1.0))
                fields[name] = FieldValue(
                    raw=val.get("raw"),
                    confidence=conf,
                    source_page=val.get("source_page"),
                    field_type=val.get("field_type"),
                )
        docs.append(
            Document(
                doc_id=str(d["doc_id"]),
                doc_type=str(d["doc_type"]),
                fields=fields,
            )
        )
    meta: dict[str, Any] = {
        "source": "external_ocr",
        "field_source": "external_ocr",
        "ocr_model": str(data["ocr_model"]).strip(),
        "ocr_version": str(data["ocr_version"]).strip(),
        "ocr_imported_at": datetime.now(timezone.utc).isoformat(),
        "schema_version": SCHEMA_VERSION,
    }
    if demo_note:
        meta["note"] = demo_note
    # optional passthrough
    if data.get("label") is not None:
        meta["label"] = data["label"]
    if isinstance(data.get("expected_verdicts"), dict):
        meta["expected_verdicts"] = data["expected_verdicts"]
    return Application(
        application_id=str(data["application_id"]).strip(),
        documents=docs,
        meta=meta,
    )


def load_external_ocr_file(path: str | Path, *, demo_note: str | None = None) -> Application:
    path = Path(path)
    if not path.is_file():
        raise ExternalOcrImportError("not_found", f"file not found: {path}")
    size = path.stat().st_size
    if size > MAX_BYTES:
        raise ExternalOcrImportError(
            "file_too_large",
            f"file size {size} exceeds {MAX_BYTES} bytes cap",
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ExternalOcrImportError("invalid_json", str(e)) from e
    return external_ocr_to_application(data, demo_note=demo_note)


def application_to_fixture_dict(app: Application) -> dict[str, Any]:
    """Serialize for fixtures/semi/*.json with nested meta + top-level fields."""
    meta = dict(app.meta)
    expected = meta.pop("expected_verdicts", None)
    label = meta.pop("label", None)
    out: dict[str, Any] = {
        "application_id": app.application_id,
        "meta": meta,
        "documents": [d.to_dict() for d in app.documents],
    }
    if label is not None:
        out["label"] = label
    if expected is not None:
        out["expected_verdicts"] = expected
    return out


def import_external_ocr_to_dir(
    src: str | Path,
    out_dir: str | Path,
    *,
    repo_root: str | Path | None = None,
    demo_note: str | None = None,
) -> Path:
    """Validate path under repo_root, import, write fixtures/semi-style JSON."""
    src = Path(src).resolve()
    out_dir = Path(out_dir)
    root = Path(repo_root).resolve() if repo_root else None
    if root is not None:
        try:
            src.relative_to(root)
        except ValueError as e:
            raise ExternalOcrImportError(
                "path_outside_repo",
                f"source path must be under repo root {root}: {src}",
            ) from e
    app = load_external_ocr_file(src, demo_note=demo_note)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{app.application_id}.json"
    # safety: also cap output
    payload = application_to_fixture_dict(app)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ExternalOcrImportError("output_too_large", "serialized application exceeds 2MB")
    out_path.write_text(text, encoding="utf-8")
    return out_path
