"""Field normalizers."""

from __future__ import annotations

from task4_consistency.normalize.address import normalize_address
from task4_consistency.normalize.base import normalize_field, register_normalizer
from task4_consistency.normalize.date import normalize_date
from task4_consistency.normalize.id_number import normalize_id_number
from task4_consistency.normalize.money import normalize_money
from task4_consistency.normalize.person import normalize_person_name
from task4_consistency.normalize.plate import normalize_plate, normalize_plate_list
from task4_consistency.normalize.vin import normalize_vin

__all__ = [
    "normalize_field",
    "register_normalizer",
    "normalize_vin",
    "normalize_date",
    "normalize_money",
    "normalize_address",
    "normalize_person_name",
    "normalize_plate",
    "normalize_plate_list",
    "normalize_id_number",
]
