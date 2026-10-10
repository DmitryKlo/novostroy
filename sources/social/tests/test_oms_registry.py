from __future__ import annotations

from sources.social.oms_registry import _candidate_matches


def test_specialty_center_crosswalk_requires_exact_alias_and_address() -> None:
    evidence = {
        "organization_name": (
            "Государственное бюджетное учреждение здравоохранения города Москвы "
            "«Московский научно-практический центр дерматовенерологии и косметологии "
            "Департамента здравоохранения города Москвы»"
        ),
        "address": {
            "street": "улица Новопетровская",
            "house_number": "20",
        },
        "osm_name_aliases": ["ГБУЗ МНПЦДК ДЗМ филиал Тимирязевский"],
    }
    tags = {
        "name": "ГБУЗ МНПЦДК ДЗМ филиал Тимирязевский",
        "addr:city": "Москва",
        "addr:street": "Новопетровская улица",
        "addr:housenumber": "20",
        "amenity": "clinic",
        "healthcare": "clinic",
    }

    assert _candidate_matches(tags, evidence)
    assert not _candidate_matches({**tags, "addr:housenumber": "20А"}, evidence)
    assert not _candidate_matches({**tags, "name": "Филиал Тимирязевский"}, evidence)
    assert not _candidate_matches({**tags, "amenity": "doctors", "healthcare": None}, evidence)
