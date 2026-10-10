from __future__ import annotations

import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path

from sources.property.estateline import (
    SourceFile,
    SourceRecord,
    classify_record,
    normalize_handover,
    parse_floors,
    parse_source_file,
    _facts_equal,
    _latest_by_page_id,
    _latest_records_conflict,
    _location,
    _source_address_tuple,
)


def write_minimal_xlsx(path: Path) -> None:
    shared = [
        "Отчет в период с 01.09.2026 по 07.09.2026 по региону «Москва»",
        "- Многоквартирные дома",
        "Информация о строительном объекте: жилой дом",
        "ID",
        "12345",
        "Наименование",
        "Тестовый жилой дом",
        "Характеристика",
        "Монолитный жилой дом",
        "Вид объекта",
        "Жилой дом",
        "Состояние",
        "Строительство",
        "Рынок",
        "Первичный",
        "Этажность",
        "9–17",
        "Местоположение",
        "г. Москва, ул. Тестовая, д. 1",
        "Срок ввода",
        "II Квартал2028",
        "Телефон",
        "+7 999 123-45-67",
        "РАБОТЫ ПО ОБЪЕКТУ",
    ]
    shared_xml = "<sst xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\">" + "".join(
        f"<si><t>{item}</t></si>" for item in shared
    ) + "</sst>"
    workbook_xml = (
        "<workbook xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\" "
        "xmlns:r=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships\">"
        "<sheets><sheet name=\"Жилая недвижимость\" sheetId=\"1\" r:id=\"rId1\"/></sheets></workbook>"
    )
    workbook_rels = (
        "<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">"
        "<Relationship Id=\"rId1\" Target=\"worksheets/sheet1.xml\" "
        "Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet\"/>"
        "</Relationships>"
    )
    cells = []
    rows = [
        (1, 0, None), (3, 1, None), (6, 2, None), (7, 3, 4),
        (8, 5, 6), (9, 7, 8), (10, 9, 10), (11, 11, 12),
        (12, 13, 14), (13, 15, 16), (14, 17, 18), (15, 19, 20),
        (16, 21, 22), (17, 23, None),
    ]
    for row_num, a_idx, b_idx in rows:
        a = f'<c r="A{row_num}" t="s"><v>{a_idx}</v></c>'
        b = f'<c r="B{row_num}" t="s"><v>{b_idx}</v></c>' if b_idx is not None else ""
        cells.append(f'<row r="{row_num}">{a}{b}</row>')
    sheet_xml = (
        "<worksheet xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\" "
        "xmlns:r=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships\">"
        f"<sheetData>{''.join(cells)}</sheetData><hyperlinks>"
        "<hyperlink ref=\"A6\" r:id=\"rId_hyperlink_1\"/>"
        "</hyperlinks></worksheet>"
    )
    sheet_rels = (
        "<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">"
        "<Relationship Id=\"rId_hyperlink_1\" Target=\"https://estateline.ru/"
        "baza-stroyashchikhsya-obektov/987654\" TargetMode=\"External\" "
        "Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink\"/>"
        "</Relationships>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/sharedStrings.xml", shared_xml)
        archive.writestr("xl/workbook.xml", workbook_xml)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        archive.writestr("xl/worksheets/_rels/sheet1.xml.rels", sheet_rels)


class EstateLineParserTests(unittest.TestCase):
    def test_parses_minimal_realistic_ooxml_and_drops_contact_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "moskva-week-test.xlsx"
            write_minimal_xlsx(path)
            source, records, counts = parse_source_file(path, "a" * 64)
        self.assertEqual((source.period_start.isoformat(), source.period_end.isoformat(), source.region),
                         ("2026-09-01", "2026-09-07", "Москва"))
        self.assertEqual(counts, {"Многоквартирные дома": 1})
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.page_id, "987654")
        self.assertEqual(record.source_row_id, "12345")
        self.assertEqual(record.fields["floors"], "9–17")
        self.assertNotIn("Телефон", record.fields)
        self.assertEqual(classify_record(record)[0], "multifamily")

    def test_reads_real_estateline_snapshot_record(self) -> None:
        source_path = Path(
            "/home/pirojban/Загрузки/1_xls_estateline/"
            "moskva-week-06092026-63413cd2bfd5d65483248153bec133c3.xlsx"
        )
        if not source_path.exists():
            self.skipTest("user-supplied source corpus is not mounted")
        source, records, _ = parse_source_file(source_path)
        record = next(item for item in records if item.page_id == "4118454")
        self.assertEqual(source.region, "Москва")
        self.assertEqual(source.period_end.isoformat(), "2026-09-06")
        self.assertEqual(record.fields["name"], "Жилой дом")
        self.assertEqual(record.fields["status"], "Нулевой цикл")
        self.assertIn("Рублевское шоссе", record.fields["address"])

    def test_apartment_use_stays_separate_from_residential_housing(self) -> None:
        source = SourceFile(Path("source.xlsx"), "0" * 64, 1, date(2026, 1, 1),
                            date(2026, 1, 7), "Москва", "")
        apartment = SourceRecord("1", "1", "url", source, "Коммерческая недвижимость",
                                 "Апартаменты, Апарт-отели, МФК",
                                 {"name": "Апарт-комплекс Тест", "characteristic": "Апартаменты"}, 1, 1)
        residential = SourceRecord("2", "2", "url", source, "Коммерческая недвижимость",
                                   "Апартаменты, Апарт-отели, МФК",
                                   {"name": "Жилой комплекс", "characteristic": "Жилой дом"}, 1, 1)
        self.assertEqual(classify_record(apartment)[0], "apartment_complex")
        self.assertEqual(classify_record(residential)[0], "multifamily")

    def test_nonresidential_wording_is_not_a_residential_signal(self) -> None:
        source = SourceFile(Path("source.xlsx"), "0" * 64, 1, date(2026, 1, 1),
                            date(2026, 1, 7), "Москва", "")
        business_center = SourceRecord(
            "3", "3", "url", source, "Коммерческая недвижимость",
            "Апартаменты, Апарт-отели, МФК",
            {"name": "Многофункциональный офисный центр",
             "characteristic": "Нежилые офисные помещения"},
            1, 1,
        )
        kind, reasons = classify_record(business_center)
        self.assertIsNone(kind)
        self.assertIn("commercial_group_without_explicit_residential_or_apartment_use", reasons)

    def test_office_in_residential_development_is_reviewed_unless_mixed_use_is_explicit(self) -> None:
        source = SourceFile(Path("source.xlsx"), "0" * 64, 1, date(2026, 1, 1),
                            date(2026, 1, 7), "Москва", "")
        contextual_office = SourceRecord(
            "4", "4", "url", source, "Коммерческая недвижимость",
            "Апартаменты, Апарт-отели, МФК",
            {
                "name": "Офисный центр",
                "source_type": "Офисные здания",
                "characteristic": "Офисный центр построят в рамках многофункционального жилого комплекса.",
            },
            1, 1,
        )
        mixed_office = SourceRecord(
            "5", "5", "url", source, "Коммерческая недвижимость",
            "Апартаменты, Апарт-отели, МФК",
            {
                "name": "Офисный центр",
                "source_type": "Офисные здания",
                "characteristic": "В офисном центре предусмотрены апартаменты и офисные помещения.",
            },
            1, 1,
        )
        kind, reasons = classify_record(contextual_office)
        self.assertIsNone(kind)
        self.assertIn("residential_signal_only_describes_neighboring_complex", reasons)
        self.assertEqual(classify_record(mixed_office)[0], "apartment_complex")

    def test_latest_selection_precedes_category_filtering(self) -> None:
        old = SourceFile(Path("moskva-week-old.xlsx"), "0" * 64, 1,
                         date(2025, 1, 1), date(2025, 1, 7), "Москва", "")
        new = SourceFile(Path("moskva-week-new.xlsx"), "1" * 64, 1,
                         date(2026, 1, 1), date(2026, 1, 7), "Москва", "")
        old_housing = SourceRecord(
            "900", "90", "url", old, "Жилая недвижимость", "Многоквартирные дома",
            {"name": "Старый ЖК", "source_type": "Жилой дом"}, 1, 2,
        )
        latest_office = SourceRecord(
            "900", "91", "url", new, "Коммерческая недвижимость", "Офисные здания",
            {"name": "Офисный центр", "source_type": "Офисные здания"}, 1, 2,
        )
        selected = _latest_by_page_id([old_housing, latest_office])["900"]
        self.assertEqual(len(selected), 1)
        self.assertIs(selected[0], latest_office)
        self.assertIsNone(classify_record(selected[0])[0])
        same_day_office = SourceRecord(
            "901", "92", "url-office", new, "Коммерческая недвижимость", "Офисные здания",
            {"name": "Офисный центр", "source_type": "Офисные здания"}, 3, 4,
        )
        same_day_housing = SourceRecord(
            "901", "93", "url-housing", new, "Жилая недвижимость", "Многоквартирные дома",
            {"name": "Жилой комплекс", "source_type": "Жилой дом"}, 5, 6,
        )
        tied = _latest_by_page_id([same_day_office, same_day_housing])["901"]
        self.assertEqual(len(tied), 2)
        self.assertTrue(_latest_records_conflict(tied))

    def test_exact_address_refuses_holdings_plots_and_unmapped_suffixes(self) -> None:
        try:
            from sources.city.local_address_index import address_key_tuple
        except ImportError:
            self.skipTest("run address tests in .runtime/geodata-env with Shapely installed")
        self.assertEqual(
            _source_address_tuple("г. Москва, ул. Главмосстроя, д. 1"),
            address_key_tuple("ул. Главмосстроя", "1"),
        )
        self.assertIsNone(_source_address_tuple("г. Москва, ул. Амурская, вл. 2А"))
        self.assertIsNone(_source_address_tuple("г. Москва, ул. Амурская, д. 2А, стр. 1"))
        self.assertIsNone(_source_address_tuple("г. Москва, ул. Амурская, д. 2А, корпус 1"))
        self.assertIsNone(_source_address_tuple("г. Москва, ул. Амурская, д. 2А (кадастровый номер участка 77:1)"))
        self.assertIsNone(_source_address_tuple("г. Москва, рядом с ул. Амурской, д. 2А"))

    def test_osm_location_requires_a_strict_exact_address_key(self) -> None:
        try:
            from sources.city.local_address_index import address_key_tuple
        except ImportError:
            self.skipTest("run location test in .runtime/geodata-env with Shapely installed")

        address = "г. Москва, ул. Тестовая, д. 1"
        key = _source_address_tuple(address)
        self.assertEqual(key, address_key_tuple("ул. Тестовая", "1"))
        index = {key: {"lon": 37.6, "lat": 55.7, "osm_type": "way", "osm_id": 42, "osm_version": 3}}
        location, quality, method, reason, _provenance = _location(address, index)
        self.assertEqual(location, [37.6, 55.7])
        self.assertEqual((quality, method, reason), ("verified", "strict_osm_address_tuple", None))
        absent = _location("г. Москва, ул. Тестовая, д. 2", index)
        self.assertIsNone(absent[0])
        self.assertEqual(absent[1], "unknown")

    def test_preserves_floor_ranges_and_handover_precision(self) -> None:
        self.assertEqual(parse_floors("9–17"), "9–17")
        self.assertEqual(parse_floors("18-30 этажей"), "18–30")
        self.assertEqual(parse_floors("12"), 12)
        self.assertEqual(normalize_handover("II Квартал2028"), ("2028-Q2", "2028", "2"))
        self.assertEqual(normalize_handover("2029"), ("2029", "2029", None))


if __name__ == "__main__":
    unittest.main()
