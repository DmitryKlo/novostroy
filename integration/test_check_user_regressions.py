"""Focused unit tests for the bounded live-regression contract checks."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from integration import check_user_regressions as gate


def _routed_item(*, target_snap: int = 7, merged_connector: bool = False) -> dict:
    network = [[37.536456, 55.660243], [37.537264, 55.659879]]
    access = [[37.5356202, 55.6596908], [37.536456, 55.660243]]
    return {
        "name": "Воронцовская",
        "route": {
            "status": "routed",
            "distance_m": 146,
            "estimated_minutes": 2,
            "geometry": access + network[1:] if merged_connector else network,
            "network_geometry": network,
            "network_distance_m": 65,
            "network_estimated_minutes": 1,
            "snap_distance_m": [0, target_snap],
            "origin_access_kind": "estimated_connector_to_walk_network",
            "origin_access": {
                "status": "estimated",
                "approximate": True,
                "distance_m": 81,
                "geometry": access,
                "source_segment_id": "segment_f834e25b9f0f6842",
            },
        },
    }


class RoutePayloadTests(unittest.TestCase):
    def test_accepts_separate_estimated_connector_and_network_route(self) -> None:
        payload = {"layers": {"metro": {"items": [_routed_item()]}}}
        self.assertEqual(gate.validate_route_payload(payload), {"routed": 1, "unrouted": 0})

    def test_rejects_target_snap_over_policy_even_when_origin_connector_exists(self) -> None:
        payload = {"layers": {"metro": {"items": [_routed_item(target_snap=51)]}}}
        with self.assertRaisesRegex(gate.GateFailure, "snap policy"):
            gate.validate_route_payload(payload)

    def test_rejects_connector_drawn_as_part_of_network_geometry(self) -> None:
        payload = {"layers": {"metro": {"items": [_routed_item(merged_connector=True)]}}}
        with self.assertRaisesRegex(gate.GateFailure, "merged into network"):
            gate.validate_route_payload(payload)

    def test_rejects_straight_line_metric_inside_public_route(self) -> None:
        payload = {"layers": {"metro": {"items": [_routed_item()]}}}
        payload["layers"]["metro"]["items"][0]["route"]["straight_line_m"] = 600
        with self.assertRaisesRegex(gate.GateFailure, "straight-line distance"):
            gate.validate_route_payload(payload)

    def test_rejects_mismatched_combined_distance(self) -> None:
        item = _routed_item()
        item["route"]["distance_m"] = 148
        payload = {"layers": {"metro": {"items": [item]}}}
        with self.assertRaisesRegex(gate.GateFailure, "omits or miscounts"):
            gate.validate_route_payload(payload)

    def test_rejects_raw_segment_id_and_seconds(self) -> None:
        item = _routed_item()
        item["route"]["origin_access"]["source_segment_id"] = "way/1234"
        payload = {"layers": {"metro": {"items": [item]}}}
        with self.assertRaisesRegex(gate.GateFailure, "connector"):
            gate.validate_route_payload(payload)
        item = _routed_item()
        item["route"]["access_seconds"] = 17
        with self.assertRaisesRegex(gate.GateFailure, "private route seconds"):
            gate.validate_route_payload({"layers": {"metro": {"items": [item]}}})

    def test_unrouted_target_exposes_no_fabricated_totals_or_geometry(self) -> None:
        item = {"route": {
            "status": "snap_exceeds_threshold", "snap_distance_m": [81, 55],
            "distance_m": None, "estimated_minutes": None, "geometry": None,
        }}
        self.assertEqual(gate.validate_route_payload({"layers": {"school": {"items": [item]}}}),
                         {"routed": 0, "unrouted": 1})

    def test_rejects_nonfinite_connector_coordinates(self) -> None:
        item = _routed_item()
        item["route"]["origin_access"]["geometry"][0][0] = float("nan")
        payload = {"layers": {"metro": {"items": [item]}}}
        with self.assertRaisesRegex(gate.GateFailure, "connector"):
            gate.validate_route_payload(payload)


class MediaAndHttpTests(unittest.TestCase):
    def test_house_photo_requires_exact_single_corpus_photo_binding(self) -> None:
        media_id = "media_8aa65b1080609d4957e30f8a"
        registry_id = "registry-b1feab9d36bced349dfa4b1d"

        class Client:
            def get_json(self, path: str) -> dict:
                self.json_path = path
                return {
                    "registry_id": registry_id,
                    "media": [{
                        "media_id": media_id,
                        "registry_id": registry_id,
                        "scope": "project_gallery_single_corpus",
                        "role": "facade_photo",
                        "label": "Фото ЖК",
                        "observed_at": "2026-10-10T16:38:08Z",
                        "availability": "prepared",
                        "asset_url": f"/v1/developer-media/{media_id}",
                    }],
                }

            def get_bytes(self, path: str) -> tuple[bytes, str]:
                self.asset_path = path
                return b"\xff\xd8\xfffixture", "image/jpeg"

        client = Client()
        detail = gate._check_developer_media(client, media_id, registry_id)
        self.assertIn("image HTTP 200", detail)
        self.assertEqual(client.json_path, f"/v1/registry/buildings/{registry_id}")
        self.assertEqual(client.asset_path, f"/v1/developer-media/{media_id}")

    def test_house_photo_rejects_rendering_label_for_facade_photo_role(self) -> None:
        media_id = "media_8aa65b1080609d4957e30f8a"
        registry_id = "registry-b1feab9d36bced349dfa4b1d"

        class Client:
            def get_json(self, _path: str) -> dict:
                return {
                    "registry_id": registry_id,
                    "media": [{
                        "media_id": media_id,
                        "scope": "project_gallery_single_corpus",
                        "role": "facade_photo",
                        "label": "Визуализация ЖК",
                        "observed_at": "2026-10-10T16:38:08Z",
                        "availability": "prepared",
                        "asset_url": f"/v1/developer-media/{media_id}",
                    }],
                }

            def get_bytes(self, _path: str) -> tuple[bytes, str]:
                raise AssertionError("must reject a mismatched visual label before fetching")

        with self.assertRaisesRegex(gate.GateFailure, "label does not describe"):
            gate._check_developer_media(Client(), media_id, registry_id)

    def test_media_fetch_uses_exact_opaque_local_route_and_image_bytes(self) -> None:
        media_id = "media_7c42ddd95b6d4153d7d31810"

        class Client:
            def get_bytes(self, path: str) -> tuple[bytes, str]:
                self.path = path
                return b"\x89PNG\r\n\x1a\nfixture", "image/png"

        client = Client()
        detail = gate._check_image_media(
            client, media_id, registry_id="registry-106b93819cc0478f967fb893",
            unit_id="unit_bd1fc63e1b2f0e6a82214d7e", expected_scope="unit_plan",
            expected_role="apartment_plan", link={
                "media_id": media_id, "scope": "unit_plan", "role": "apartment_plan",
                "observed_at": "2026-10-11T00:00:00Z", "availability": "on_demand",
                "asset_url": f"/v1/developer-media/{media_id}",
            })
        self.assertIn("image/png", detail)
        self.assertEqual(client.path, f"/v1/developer-media/{media_id}")

    def test_media_refuses_absolute_source_url(self) -> None:
        media_id = "media_7c42ddd95b6d4153d7d31810"

        class Client:
            def get_bytes(self, path: str) -> tuple[bytes, str]:
                raise AssertionError("should reject source URL before HTTP")

        with self.assertRaisesRegex(gate.GateFailure, "opaque local asset route"):
            gate._check_image_media(
                Client(), media_id, registry_id="registry-106b93819cc0478f967fb893",
                expected_scope="unit_plan", expected_role="apartment_plan", link={
                    "media_id": media_id, "scope": "unit_plan", "role": "apartment_plan",
                    "observed_at": "2026-10-11T00:00:00Z", "availability": "on_demand",
                    "asset_url": "https://cdn-dataout.trendagent.ru/example.png",
                })

    def test_http_client_rejects_credentials_and_query_in_base_url(self) -> None:
        for value in ("https://user:secret@example.test", "https://example.test?token=x"):
            with self.subTest(base_url=value), self.assertRaises(ValueError):
                gate.JsonHttpClient(value, timeout=1, max_requests=1)

    def test_request_budget_stops_without_retry(self) -> None:
        class FakeResponse:
            status = 200
            headers = {"Content-Type": "application/json"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit: int) -> bytes:
                return b"{}"

        client = gate.JsonHttpClient("http://127.0.0.1:8000", timeout=1, max_requests=1)
        with patch.object(gate, "urlopen", return_value=FakeResponse()) as mocked:
            client.get_json("/v1/health")
            with self.assertRaisesRegex(gate.GateFailure, "request budget exceeded"):
                client.get_json("/v1/health")
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(client.requests[0]["method"], "GET")


if __name__ == "__main__":
    unittest.main()
