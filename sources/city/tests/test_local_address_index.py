from __future__ import annotations

import unittest

from shapely.geometry import Point, Polygon

from sources.city import local_address_index as index


def entity(osm_type: str, osm_id: int) -> dict[str, object]:
    return {"osm_type": osm_type, "osm_id": osm_id, "osm_version": 1, "osm_timestamp": "2026-10-08T20:00:00Z"}


class LocalAddressIndexTests(unittest.TestCase):
    def test_street_type_alias_and_position_are_canonicalized_without_fuzzy_name_edits(self) -> None:
        self.assertEqual(index.normalize_street("ул. Ленина"), "улица ленина")
        self.assertEqual(index.normalize_street("Ленина улица"), "улица ленина")
        self.assertEqual(index.normalize_street("Ёлочная ул."), "улица ёлочная")
        self.assertNotEqual(index.normalize_street("Ёлочная ул."), index.normalize_street("Елочная улица"))
        self.assertEqual(index.normalize_street("ул. Новая, 2-я"), "улица новая, 2-я")

    def test_housenumber_is_whole_component_and_explicit_slots_are_part_of_key(self) -> None:
        key = index.address_key_tuple(
            "пр-т Мира",
            "вл. 15, корп. 2, стр. 4",
            block="  A-02 ",
            building="лит. Б",
        )
        self.assertEqual(key, ("проспект мира", "вл. 15, корп. 2, стр. 4", "a-02", "лит. б", None))
        self.assertNotEqual(key, index.address_key_tuple("Мира проспект", "15", block="a-02", building="лит. б"))
        self.assertIsNone(index.address_key_tuple("  ", "15"))

    def test_unique_containing_building_uses_polygon_point_on_surface(self) -> None:
        shell = [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]
        hole = [(4, 4), (6, 4), (6, 6), (4, 6), (4, 4)]
        polygon = Polygon(shell, [hole])
        key = index.address_key_tuple("ул. Ленина", "7")
        building = index.Candidate(entity("way", 20), {"building": "yes"}, None, polygon, None, "building_area")
        node_tags = {"addr:street": "Ленина улица", "addr:housenumber": "7"}
        address = index.Candidate(entity("node", 30), node_tags, key, None, Point(2, 2), "address_node")

        unique, ambiguous = index.resolve_candidates([address], [], [building])

        self.assertEqual(len(unique), 1)
        self.assertEqual(ambiguous, [])
        row = unique[0]
        self.assertEqual(row["candidate_kind"], "building_with_address_point")
        self.assertEqual(row["representative_point_method"], "point_on_surface")
        self.assertEqual({member["osm_id"] for member in row["members"]}, {20, 30})
        self.assertTrue(polygon.contains(Point(row["lon"], row["lat"])))
        self.assertFalse(Polygon(hole).contains(Point(row["lon"], row["lat"])))

    def test_overlapping_buildings_are_ambiguous_without_nearest_fallback(self) -> None:
        key = index.address_key_tuple("ул. Тестовая", "1")
        node = index.Candidate(entity("node", 11), {}, key, None, Point(1, 1), "address_node")
        left = index.Candidate(entity("way", 21), {"building": "yes"}, None,
                               Polygon([(0, 0), (2, 0), (2, 2), (0, 2)]), None, "building_area")
        right = index.Candidate(entity("relation", 22), {"building": "yes"}, None,
                                Polygon([(0.5, 0.5), (3, 0.5), (3, 3), (0.5, 3)]), None, "building_area")

        unique, ambiguous = index.resolve_candidates([node], [], [left, right])

        self.assertEqual(unique, [])
        self.assertEqual(len(ambiguous), 1)
        self.assertEqual(ambiguous[0]["reason"], "multiple_containing_buildings")

    def test_duplicate_exact_keys_remain_ambiguous(self) -> None:
        key = index.address_key_tuple("ул. Тестовая", "1")
        nodes = [
            index.Candidate(entity("node", 1), {}, key, None, Point(1, 1), "address_node"),
            index.Candidate(entity("node", 2), {}, key, None, Point(2, 2), "address_node"),
        ]

        unique, ambiguous = index.resolve_candidates(nodes, [], [])

        self.assertEqual(unique, [])
        self.assertEqual(len(ambiguous), 2)
        self.assertTrue(all(row["reason"] == "duplicate_exact_address_key" for row in ambiguous))


if __name__ == "__main__":
    unittest.main()
