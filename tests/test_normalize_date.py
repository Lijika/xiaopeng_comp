from task4_consistency.normalize.date import normalize_date


def test_cn_date():
    assert normalize_date("2024年5月18日") == "2024-05-18"


def test_iso_and_slash():
    assert normalize_date("2024-05-18") == "2024-05-18"
    assert normalize_date("2024/05/18") == "2024-05-18"


def test_compact():
    assert normalize_date("20240518") == "2024-05-18"


def test_dmy_unambiguous_and_ambiguous():
    assert normalize_date("13/02/2023") == "2023-02-13"  # day>12 → DMY
    assert normalize_date("01/02/2023") is None  # ambiguous ADV-03
    assert normalize_date("2023.2.1") == "2023-02-01"
    assert normalize_date("2023年1月") is None  # incomplete ADV-04
