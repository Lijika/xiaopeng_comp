from task4_consistency.normalize.address import normalize_address
from task4_consistency.normalize.base import normalize_field
from task4_consistency.normalize.id_number import normalize_id_number
from task4_consistency.normalize.person import normalize_person_name
from task4_consistency.normalize.plate import normalize_plate


def test_plate():
    assert normalize_plate("苏A·12345") == "苏A12345"
    assert normalize_plate("苏A 12345") == "苏A12345"


def test_person():
    assert normalize_person_name("张 三") == "张三"
    assert normalize_person_name("欧阳　修") == "欧阳修"


def test_id():
    from task4_consistency.normalize.id_number import make_valid_id18

    vid = make_valid_id18("11010119900101123")
    assert normalize_id_number(vid.lower()) == vid
    assert normalize_id_number("12345") is None


def test_address_alias():
    a = normalize_address("江苏省南京市鼓楼区中山路100号")
    b = normalize_address("江苏南京市鼓楼区中山路100号")
    assert a == b


def test_field_router():
    assert normalize_field("苏A·1", "plate_no") == "苏A1"
    assert normalize_field("2024年1月2日", "reg_date") == "2024-01-02"
