from task4_consistency.normalize.money import normalize_money


def test_currency_and_comma():
    assert normalize_money("￥128,000.00元") == "128000"
    assert normalize_money("128000.00元") == "128000"


def test_wan():
    assert normalize_money("12.8万") == "128000"


def test_mixed_units():
    assert normalize_money("12万8千") == "128000"
    assert normalize_money("1百万") == "1000000"


def test_decimal_keep():
    assert normalize_money("88000.50") == "88000.5"
