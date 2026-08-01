from task4_consistency.match.exact import exact_match
from task4_consistency.match.fuzzy import fuzzy_match, fuzzy_ratio
from task4_consistency.match.list_ops import list_contains
from task4_consistency.match.numeric import numeric_tolerance_match


def test_exact():
    assert exact_match("A", "A").equal
    assert not exact_match("A", "B").equal


def test_fuzzy():
    assert fuzzy_ratio("张三", "张三") == 1.0
    out = fuzzy_match("张三", "张 三".replace(" ", ""), threshold=0.88)
    assert out.match
    out2 = fuzzy_match("陈七", "陈八", threshold=0.88)
    assert not out2.match
    assert not out2.uncertain  # 0.5 ratio is hard mismatch for 2-char


def test_numeric_tol():
    assert numeric_tolerance_match("100", "100.5", abs_tol=1.0).match
    assert not numeric_tolerance_match("100", "110", abs_tol=1.0, rel_tol=0.001).match


def test_list_contains():
    assert list_contains("苏A12345|苏A12346", "苏A12345").match
    assert not list_contains("苏A12345", "苏B00000").match
