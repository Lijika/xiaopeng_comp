#!/usr/bin/env python3
"""Import offline external OCR intermediate JSON into fixtures/semi/.

Round19: no OCR engine. Path must stay inside repo. 2MB cap.

  .venv/bin/python scripts/import_external_ocr.py fixtures/layout_slots/example.json -o fixtures/semi/
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from task4_consistency.adapters.external_ocr_import import (  # noqa: E402
    ExternalOcrImportError,
    import_external_ocr_to_dir,
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Import external OCR intermediate JSON → fixtures/semi")
    p.add_argument("input", help="Path to intermediate OCR JSON (must be under repo)")
    p.add_argument(
        "-o",
        "--output-dir",
        default=str(ROOT / "fixtures" / "semi"),
        help="Output directory (default fixtures/semi)",
    )
    p.add_argument(
        "--demo-note",
        default=None,
        help="Optional meta.note (e.g. demo for layout_slots examples)",
    )
    args = p.parse_args(argv)
    try:
        out = import_external_ocr_to_dir(
            args.input,
            args.output_dir,
            repo_root=ROOT,
            demo_note=args.demo_note,
        )
    except ExternalOcrImportError as e:
        print(f"ERROR [{e.error}]: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    print(f"OK wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
