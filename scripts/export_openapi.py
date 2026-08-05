"""Deterministic OpenAPI export for the frontend contract (T01).

The FastAPI application is the sole OpenAPI authority.  This script dumps its
generated document with sorted keys so the committed artifact is byte-stable;
``openapi-typescript`` then converts it into the committed generated TypeScript
types which the drift check re-verifies on every run.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from task4_consistency.web.app import app


def main() -> int:
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        "frontend/src/generated/openapi.json"
    )
    spec = app.openapi()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
