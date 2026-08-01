from pathlib import Path

from task4_consistency.adapters.step2_page_order import load_page_order

ROOT = Path(__file__).resolve().parents[1]
STEP2 = ROOT / "data" / "step2"


def test_step2_adapter_smoke():
    files = list(STEP2.glob("*_page_order.json"))
    assert files, "step2 fixtures missing"
    app = load_page_order(files[0])
    assert app.application_id
    assert app.documents
    # raw texts are placeholders (None) without OCR
    assert any(fv.raw is None for fv in app.documents[0].fields.values())
