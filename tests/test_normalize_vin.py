from task4_consistency.normalize.vin import is_valid_vin, normalize_vin, normalize_vin_ex


def test_normalize_vin_basic():
    assert normalize_vin("lsv aa4182n2123456") == "LSVAA4182N2123456"


def test_normalize_vin_ioq_fix_flagged():
    r = normalize_vin_ex("LSVAO4182N2123456")
    assert r.value == "LSVA04182N2123456"
    assert r.ocr_fix is True
    r2 = normalize_vin_ex("LSVAI4182N2123456")
    assert r2.value == "LSVA14182N2123456"
    assert r2.ocr_fix is True
    # no IOQ → no flag
    r3 = normalize_vin_ex("LSVAA4182N2123456")
    assert r3.ocr_fix is False


def test_is_valid_vin():
    assert is_valid_vin("LSVAA4182N2123456")
    assert not is_valid_vin("SHORT")
