"""Focused tests for the internal OSM proximity score and geometry handling."""

from __future__ import annotations

import json
from pathlib import Path

from pyproj import Transformer
from shapely.geometry import LineString, Point, Polygon, shape
from shapely.ops import transform

from sources.property.environment_noise import (
    CRS_DISTANCE,
    CRS_SOURCE,
    NearestFeatureIndex,
    SEARCH_HALO_M,
    _nearest_evidence,
    classify_tags,
    extract_feature_layers,
    score_component,
    score_registry_point,
)


def test_distance_decay_and_source_filters() -> None:
    assert score_component(0, weight=40, zero_distance_m=1_000) == 40
    assert score_component(500, weight=40, zero_distance_m=1_000) == 20
    assert score_component(1_000, weight=40, zero_distance_m=1_000) == 0
    assert classify_tags({"highway": "primary"}, "LineString") == ["major_road"]
    assert classify_tags({"railway": "rail", "tunnel": "yes"}, "LineString") == []
    assert classify_tags({"railway": "subway"}, "LineString") == []
    assert classify_tags({"highway": "trunk", "disused": "yes"}, "LineString") == []
    assert classify_tags({"aeroway": "runway", "layer": "-1"}, "LineString") == []


def test_feature_extraction_clips_geometry_and_preserves_osm_identity(tmp_path: Path) -> None:
    forward = Transformer.from_crs(CRS_SOURCE, CRS_DISTANCE, always_xy=True)
    inverse = Transformer.from_crs(CRS_DISTANCE, CRS_SOURCE, always_xy=True)
    center_x, center_y = forward.transform(37.5, 55.75)
    halo = Polygon([
        (center_x - 250, center_y - 250),
        (center_x + 250, center_y - 250),
        (center_x + 250, center_y + 250),
        (center_x - 250, center_y + 250),
    ])
    export = tmp_path / "features.geojsonseq"
    feature = {
        "type": "Feature",
        "properties": {
            "@type": "way", "@id": 456, "@version": 7,
            "@timestamp": "2026-10-08T20:21:06Z", "highway": "primary",
        },
        "geometry": {
            "type": "LineString",
            "coordinates": [[37.49, 55.75], [37.51, 55.75]],
        },
    }
    export.write_text(json.dumps(feature) + "\n", encoding="utf-8")

    layers, stats = extract_feature_layers(export, halo, forward, inverse)

    assert len(layers["major_road"]) == 1
    record = layers["major_road"][0]["record"]
    assert record["source_id"] == "osm:way:456"
    assert record["osm_version"] == 7
    assert record["osm_timestamp"] == "2026-10-08T20:21:06Z"
    clipped = shape(record["geometry_wgs84_clipped_to_15km_halo"])
    clipped_metric = transform(forward.transform, clipped)
    assert clipped_metric.bounds[0] >= halo.bounds[0] - 0.01
    assert clipped_metric.bounds[2] <= halo.bounds[2] + 0.01
    assert stats["feature_count_by_component"]["major_road"] == 1


def test_score_is_distance_based_and_unlocated_stays_null() -> None:
    forward = Transformer.from_crs(CRS_SOURCE, CRS_DISTANCE, always_xy=True)
    boundary = Polygon([
        (37.0, 55.0), (38.0, 55.0), (38.0, 56.0), (37.0, 56.0), (37.0, 55.0),
    ])
    lon, lat = 37.5, 55.75
    x, y = forward.transform(lon, lat)
    road = {
        "geometry_metric": LineString([(x, y), (x + 2_000, y)]),
        "record": {
            "source_id": "osm:way:456", "osm_type": "way", "osm_id": 456,
            "osm_version": 7, "osm_timestamp": "2026-10-08T20:21:06Z",
            "geometry_type": "LineString", "tags": {"highway": "primary"},
        },
    }
    indexes = {
        "major_road": NearestFeatureIndex([road]),
        "railway": NearestFeatureIndex([]),
        "industrial": NearestFeatureIndex([]),
        "airport": NearestFeatureIndex([]),
    }
    hashes = {key: "a" * 64 for key in indexes}
    covered = score_registry_point(
        {"registry_id": "registry-test", "location": [lon, lat],
         "location_basis": "source_corpus_polygon_representative_point"},
        boundary=boundary, forward=forward, indexes=indexes, layer_hashes=hashes,
        pbf_boxes=[[30, 48, 48, 60]], pbf_sha256="b" * 64, as_of_date="2026-10-08",
    )
    assert covered["noise_index"] == 40
    assert covered["noise_basis"] == "internal_osm_proximity_v1"
    assert covered["noise_coverage"] == "covered"
    assert covered["noise_inputs"]["components"]["major_road"]["nearest_source"]["source_id"] == "osm:way:456"
    assert covered["noise_inputs"]["measured_db_a"] is None
    no_sources = score_registry_point(
        {"registry_id": "registry-empty", "location": [lon, lat]},
        boundary=boundary, forward=forward,
        indexes={key: NearestFeatureIndex([]) for key in indexes},
        layer_hashes=hashes, pbf_boxes=[[30, 48, 48, 60]],
        pbf_sha256="b" * 64, as_of_date="2026-10-08",
    )
    assert no_sources["noise_coverage"] == "covered"
    assert no_sources["noise_index"] == 0
    missing = score_registry_point(
        {"registry_id": "registry-unlocated", "location": None},
        boundary=boundary, forward=forward, indexes=indexes, layer_hashes=hashes,
        pbf_boxes=[[30, 48, 48, 60]], pbf_sha256="b" * 64, as_of_date="2026-10-08",
    )
    assert missing["noise_index"] is None
    assert missing["noise_basis"] is None
    assert missing["noise_coverage"] == "unlocated"


def test_nearest_evidence_uses_exact_candidate_and_distance() -> None:
    feature = {
        "record": {
            "source_id": "osm:way:456", "osm_type": "way", "osm_id": 456,
            "osm_version": 7, "osm_timestamp": None, "geometry_type": "LineString",
            "tags": {"highway": "primary"},
        }
    }
    evidence = _nearest_evidence(feature, 250.0, "major_road", "c" * 64)
    assert evidence["distance_m"] == 250
    assert evidence["component_points"] == 30
    assert evidence["nearest_source"]["source_id"] == "osm:way:456"
    assert SEARCH_HALO_M == 15_000
