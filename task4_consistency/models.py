"""Domain models for Task 4 consistency checks."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Verdict(str, Enum):
    CONSISTENT = "consistent"
    INCONSISTENT = "inconsistent"
    UNCERTAIN = "uncertain"
    SKIPPED = "skipped"


class Severity(str, Enum):
    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    INFO = "info"


@dataclass
class FieldValue:
    raw: str | None
    confidence: float = 1.0
    source_page: int | None = None
    normalized: str | None = None
    field_type: str | None = None
    ocr_fix: bool = False
    pre_ocr: str | None = None
    notes: list[str] = field(default_factory=list)

    @classmethod
    def from_obj(cls, obj: Any) -> "FieldValue":
        if obj is None:
            return cls(raw=None)
        if isinstance(obj, FieldValue):
            return obj
        if isinstance(obj, str):
            return cls(raw=obj)
        if isinstance(obj, dict):
            return cls(
                raw=obj.get("raw"),
                confidence=float(obj.get("confidence", 1.0)),
                source_page=obj.get("source_page"),
                normalized=obj.get("normalized"),
                field_type=obj.get("field_type"),
                ocr_fix=bool(obj.get("ocr_fix", False)),
                pre_ocr=obj.get("pre_ocr"),
                notes=list(obj.get("notes") or []),
            )
        return cls(raw=str(obj))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # omit empty audit fields for compact reports
        if not d.get("ocr_fix"):
            d.pop("ocr_fix", None)
            d.pop("pre_ocr", None)
        if not d.get("notes"):
            d.pop("notes", None)
        return d


@dataclass
class Document:
    doc_id: str
    doc_type: str
    fields: dict[str, FieldValue] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Document":
        fields_raw = data.get("fields") or {}
        fields = {k: FieldValue.from_obj(v) for k, v in fields_raw.items()}
        return cls(
            doc_id=str(data.get("doc_id") or data.get("id") or ""),
            doc_type=str(data.get("doc_type") or data.get("type") or ""),
            fields=fields,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "doc_type": self.doc_type,
            "fields": {k: v.to_dict() for k, v in self.fields.items()},
        }


@dataclass
class Application:
    application_id: str
    documents: list[Document] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Application":
        docs = [Document.from_dict(d) for d in data.get("documents") or []]
        meta = {k: v for k, v in data.items() if k not in {"application_id", "documents"}}
        return cls(
            application_id=str(data.get("application_id") or data.get("id") or ""),
            documents=docs,
            meta=meta,
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "application_id": self.application_id,
            "documents": [d.to_dict() for d in self.documents],
        }
        out.update(self.meta)
        return out

    def docs_by_type(self) -> dict[str, list[Document]]:
        mapping: dict[str, list[Document]] = {}
        for doc in self.documents:
            mapping.setdefault(doc.doc_type, []).append(doc)
        return mapping


@dataclass
class FieldSnapshot:
    doc_id: str
    doc_type: str
    field: str
    raw: str | None
    normalized: str | None
    confidence: float
    ocr_fix: bool = False
    pre_ocr: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if not d.get("ocr_fix"):
            d.pop("ocr_fix", None)
            d.pop("pre_ocr", None)
        if not d.get("notes"):
            d.pop("notes", None)
        return d


@dataclass
class DiffHighlight:
    pos: int | None = None
    left: str | None = None
    right: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class CheckResult:
    rule_id: str
    name: str
    verdict: Verdict
    severity: Severity
    message: str
    snapshots: list[FieldSnapshot] = field(default_factory=list)
    diff_highlight: DiffHighlight | None = None
    score: float | None = None
    rule_type: str | None = None
    flags: list[str] = field(default_factory=list)
    reason_codes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "rule_id": self.rule_id,
            "name": self.name,
            "verdict": self.verdict.value,
            "severity": self.severity.value,
            "message": self.message,
            "snapshots": [s.to_dict() for s in self.snapshots],
        }
        if self.diff_highlight is not None:
            d["diff_highlight"] = self.diff_highlight.to_dict()
        if self.score is not None:
            d["score"] = self.score
        if self.rule_type is not None:
            d["rule_type"] = self.rule_type
        if self.flags:
            d["flags"] = list(self.flags)
        if self.reason_codes:
            d["reason_codes"] = list(self.reason_codes)
        return d


@dataclass
class ReportSummary:
    consistent: int = 0
    inconsistent: int = 0
    uncertain: int = 0
    skipped: int = 0
    coverage: float = 0.0
    total: int = 0  # non-skipped checks (coverage denominator)
    total_including_skipped: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Report:
    application_id: str
    summary: ReportSummary
    checks: list[CheckResult] = field(default_factory=list)
    rule_config_version: str | int | None = None
    rule_package: str | None = None
    rule_changelog: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "application_id": self.application_id,
            "summary": self.summary.to_dict(),
            "checks": [c.to_dict() for c in self.checks],
            "rule_config_version": self.rule_config_version,
        }
        if self.rule_package is not None:
            d["rule_package"] = self.rule_package
        if self.rule_changelog:
            d["rule_changelog"] = list(self.rule_changelog)
        return d
