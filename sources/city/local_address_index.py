#!/usr/bin/env python3
"""Build a strict, offline OSM address-key index for the pinned Moscow mask.

The index contains only complete ``addr:street`` + ``addr:housenumber`` keys.
It never guesses a nearby building: an address point is attached to a building
only when exactly one clipped building polygon contains it and the address
components do not conflict. Colliding keys are written to ``ambiguous.jsonl``.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.metadata
import json
import os
import tempfile
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import shapely
from shapely.geometry import GeometryCollection, MultiPolygon, Point, Polygon, shape
from shapely.strtree import STRtree

try:
    import osmium
except ModuleNotFoundError:  # normalization helpers are also used by lightweight joiners
    osmium = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PBF = ROOT / "data/social/osm_moscow_city_261008.osm.pbf"
DEFAULT_PBF_MANIFEST = ROOT / "data/social/osm_moscow_city_261008.manifest.json"
DEFAULT_BOUNDARY = ROOT / "data/city/osm_moscow_city_boundary.geojson"
DEFAULT_BOUNDARY_MANIFEST = ROOT / "data/city/osm_moscow_city_boundary.manifest.json"
DEFAULT_OUTPUT_DIR = ROOT / "data/city/local_address_index"
INDEX_VERSION = "1.0.0"

# Only unambiguous Russian street-type abbreviations are expanded. Punctuation
# in the street name and the ё/е distinction are preserved.
_STREET_TYPES = {
    "аллея": "аллея", "ал.": "аллея",
    "бульвар": "бульвар", "бул.": "бульвар", "б-р": "бульвар", "бр": "бульвар",
    "набережная": "набережная", "наб.": "набережная", "наб": "набережная",
    "переулок": "переулок", "пер.": "переулок", "пер": "переулок",
    "площадь": "площадь", "пл.": "площадь", "пл": "площадь",
    "проезд": "проезд", "пр-д": "проезд", "пр-д.": "проезд",
    "проспект": "проспект", "просп.": "проспект", "просп": "проспект",
    "пр-т": "проспект", "пр-т.": "проспект",
    "улица": "улица", "ул.": "улица", "ул": "улица",
    "шоссе": "шоссе", "ш.": "шоссе", "ш": "шоссе",
    "тупик": "тупик", "туп.": "тупик", "туп": "тупик",
    "линия": "линия", "лин.": "линия",
    "тракт": "тракт", "тракт.": "тракт",
    "дорога": "дорога", "дор.": "дорога",
}


def _canonical_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = unicodedata.normalize("NFC", value).casefold()
    normalized = " ".join(normalized.split())
    return normalized or None


def normalize_component(value: str | None) -> str | None:
    """Normalize a non-street address component without changing its syntax."""
    return _canonical_text(value)


def normalize_street(value: str | None) -> str | None:
    """Case/space normalize a street and safely canonicalize known type aliases.

    A known type at the beginning or end is moved to the same canonical prefix.
    Unknown tokens, name punctuation, and the distinction between ё and е stay
    intact. Repeated recognized types are left untouched as potentially
    malformed input instead of being silently repaired.
    """
    text = _canonical_text(value)
    if text is None:
        return None
    tokens = text.split(" ")
    first = _STREET_TYPES.get(tokens[0])
    last = _STREET_TYPES.get(tokens[-1])
    if first is not None and last is not None:
        return text
    if first is not None and len(tokens) > 1:
        return f"{first} {' '.join(tokens[1:])}"
    if last is not None and len(tokens) > 1:
        return f"{last} {' '.join(tokens[:-1])}"
    return text


AddressKey = tuple[str, str, str | None, str | None, str | None]


def address_key_tuple(
    street: str | None,
    housenumber: str | None,
    *,
    block: str | None = None,
    building: str | None = None,
    conscriptionnumber: str | None = None,
) -> AddressKey | None:
    """Return the strict join tuple; optional components remain key material.

    For OSM, pass the raw values of ``addr:street``, ``addr:housenumber``,
    ``addr:block``, ``addr:building``, and ``addr:conscriptionnumber``. A
    source that cannot confidently separate these fields should leave the
    optional arguments null; text such as ``корп. 2`` is never split here.
    """
    street_key = normalize_street(street)
    house_key = normalize_component(housenumber)
    if street_key is None or house_key is None:
        return None
    return (
        street_key,
        house_key,
        normalize_component(block),
        normalize_component(building),
        normalize_component(conscriptionnumber),
    )


def address_key_from_tags(tags: Mapping[str, str]) -> AddressKey | None:
    """Build an index key from raw OSM ``addr:*`` tags."""
    return address_key_tuple(
        tags.get("addr:street"),
        tags.get("addr:housenumber"),
        block=tags.get("addr:block"),
        building=tags.get("addr:building"),
        conscriptionnumber=tags.get("addr:conscriptionnumber"),
    )


def key_json(key: AddressKey) -> list[str | None]:
    return list(key)


def _key_string(key: AddressKey) -> str:
    return json.dumps(key_json(key), ensure_ascii=False, separators=(",", ":"))


def _addr_tags(tags: Mapping[str, str]) -> dict[str, str]:
    return {key: value for key, value in tags.items() if key.startswith("addr:")}


def _has_address_tags(tags: Mapping[str, str]) -> bool:
    return any(key.startswith("addr:") and value.strip() for key, value in tags.items())


def _has_key_address_tags(tags: Mapping[str, str]) -> bool:
    return any(tags.get(key, "").strip() for key in (
        "addr:street", "addr:housenumber", "addr:block", "addr:building", "addr:conscriptionnumber"
    ))


def _timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    text = str(value)
    return text[:-6] + "Z" if text.endswith("+00:00") else text


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _polygonal(geometry: Any) -> Any | None:
    if geometry is None or geometry.is_empty:
        return None
    if not geometry.is_valid:
        geometry = shapely.make_valid(geometry)
    polygons: list[Polygon] = []
    if isinstance(geometry, Polygon):
        polygons = [geometry]
    elif isinstance(geometry, MultiPolygon):
        polygons = list(geometry.geoms)
    elif isinstance(geometry, GeometryCollection):
        for part in geometry.geoms:
            clipped = _polygonal(part)
            if isinstance(clipped, Polygon):
                polygons.append(clipped)
            elif isinstance(clipped, MultiPolygon):
                polygons.extend(clipped.geoms)
    if not polygons:
        return None
    result = shapely.union_all(polygons)
    if result.is_empty or not result.is_valid:
        return None
    return result


def _clip_polygon(geometry: Any, boundary: Any) -> Any | None:
    geometry = _polygonal(geometry)
    if geometry is None or not geometry.intersects(boundary):
        return None
    clipped = geometry if boundary.covers(geometry) else _polygonal(geometry.intersection(boundary))
    return _polygonal(clipped)


@dataclass
class Candidate:
    entity: dict[str, Any]
    tags: dict[str, str]
    key: AddressKey | None
    geometry: Any | None
    point: Point | None
    kind: str
    members: list[dict[str, Any]] | None = None

    @property
    def identity(self) -> tuple[str, int]:
        return self.entity["osm_type"], int(self.entity["osm_id"])


def _entity(osm_type: str, obj: Any) -> dict[str, Any]:
    return {
        "osm_type": osm_type,
        "osm_id": int(obj.id),
        "osm_version": int(getattr(obj, "version", 0)),
        "osm_timestamp": _timestamp(getattr(obj, "timestamp", None)),
    }


def _candidate_record(candidate: Candidate, *, reason: str | None = None) -> dict[str, Any]:
    geometry = candidate.geometry
    if geometry is not None:
        point = geometry.representative_point()
        method = "point_on_surface"
    else:
        point = candidate.point
        method = "source_node" if point is not None else None
    record = {
        "address_key": key_json(candidate.key) if candidate.key is not None else None,
        "street_key": candidate.key[0] if candidate.key else None,
        "housenumber_key": candidate.key[1] if candidate.key else None,
        "block_key": candidate.key[2] if candidate.key else None,
        "building_key": candidate.key[3] if candidate.key else None,
        "conscriptionnumber_key": candidate.key[4] if candidate.key else None,
        "lon": float(point.x) if point is not None else None,
        "lat": float(point.y) if point is not None else None,
        "representative_point_method": method,
        **candidate.entity,
        "osm_tags": candidate.tags,
        "candidate_kind": candidate.kind,
        "members": candidate.members or [{**candidate.entity, "osm_tags": candidate.tags}],
        "source_attribution": "© OpenStreetMap contributors",
    }
    if reason is not None:
        record["reason"] = reason
    return record


def _compatible_building_address(building: Candidate, key: AddressKey) -> bool:
    if building.key is not None:
        return building.key == key
    return not _has_key_address_tags(building.tags)


def resolve_candidates(
    address_nodes: list[Candidate],
    address_areas: list[Candidate],
    building_areas: list[Candidate],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Compose proven node/building pairs and split unique keys from ambiguity.

    Containment is exact polygon containment. A point on a boundary, a point in
    overlapping buildings, or a conflicting address component is never matched
    by distance. ``address_areas`` and ``building_areas`` may contain the same
    source feature; source identity is used to suppress only that exact duplicate.
    """
    buildings = [item for item in building_areas if item.geometry is not None]
    tree = STRtree([item.geometry for item in buildings]) if buildings else None
    attached: dict[tuple[str, int], list[Candidate]] = defaultdict(list)
    standalone_nodes: list[Candidate] = []
    ambiguous: list[dict[str, Any]] = []

    for node in address_nodes:
        assert node.point is not None and node.key is not None
        hits: list[int] = []
        if tree is not None:
            for raw_index in tree.query(node.point):
                index = int(raw_index)
                if buildings[index].geometry.contains(node.point):
                    hits.append(index)
        if len(hits) > 1:
            ambiguous.append(_candidate_record(node, reason="multiple_containing_buildings"))
        elif len(hits) == 1:
            attached[buildings[hits[0]].identity].append(node)
        else:
            standalone_nodes.append(node)

    composite_by_building: dict[tuple[str, int], Candidate] = {}
    buildings_by_identity = {item.identity: item for item in buildings}
    for identity, nodes in attached.items():
        building = buildings_by_identity[identity]
        node_keys = {node.key for node in nodes}
        if len(node_keys) != 1:
            ambiguous.extend(_candidate_record(node, reason="conflicting_address_points_in_building") for node in nodes)
            continue
        key = next(iter(node_keys))
        if not _compatible_building_address(building, key):
            ambiguous.extend(_candidate_record(node, reason="conflicting_building_address") for node in nodes)
            continue
        merged_tags = dict(building.tags)
        member_records = [{**building.entity, "osm_tags": building.tags}]
        for node in nodes:
            member_records.append({**node.entity, "osm_tags": node.tags})
            merged_tags.update(node.tags)
        composite = Candidate(building.entity, merged_tags, key, building.geometry, None,
                              "building_with_address_point", member_records)
        composite_by_building[identity] = composite

    candidates: list[dict[str, Any]] = []
    consumed_area_ids: set[tuple[str, int]] = set()
    area_by_identity = {item.identity: item for item in address_areas}
    for identity, composite in composite_by_building.items():
        candidates.append(_candidate_record(composite))
        if identity in area_by_identity and area_by_identity[identity].key == composite.key:
            consumed_area_ids.add(identity)
    for area in address_areas:
        if area.identity not in consumed_area_ids:
            candidates.append(_candidate_record(area))
    candidates.extend(_candidate_record(node) for node in standalone_nodes)

    grouped: dict[AddressKey, list[dict[str, Any]]] = defaultdict(list)
    for record in candidates:
        key_value = record["address_key"]
        if key_value is None:
            continue
        key = (key_value[0], key_value[1], key_value[2], key_value[3], key_value[4])
        grouped[key].append(record)

    unique: list[dict[str, Any]] = []
    for key, records in grouped.items():
        if len(records) == 1:
            unique.append(records[0])
        else:
            for record in records:
                ambiguous.append({**record, "reason": "duplicate_exact_address_key"})
    unique.sort(key=lambda row: (_key_string(tuple(row["address_key"])), row["osm_type"], row["osm_id"]))
    ambiguous.sort(key=lambda row: (
        _key_string(tuple(row["address_key"])) if row["address_key"] is not None else "",
        row.get("reason", ""), row["osm_type"], row["osm_id"],
    ))
    return unique, ambiguous


_OSMIUM_HANDLER_BASE = osmium.SimpleHandler if osmium is not None else object


class AddressHandler(_OSMIUM_HANDLER_BASE):
    """Collect address points and polygon Areas with source OSM identities."""

    def __init__(self, boundary: Any) -> None:
        super().__init__()
        if osmium is None:
            raise RuntimeError("pyosmium is required to build the index; use the local geodata environment")
        self.boundary = boundary
        self.factory = osmium.geom.WKBFactory()
        self.address_nodes: list[Candidate] = []
        self.address_areas: list[Candidate] = []
        self.building_areas: list[Candidate] = []
        self.address_entity_ids: set[tuple[str, int]] = set()
        self.area_ids: set[tuple[str, int]] = set()
        self.counts: Counter[str] = Counter()

    def node(self, node: Any) -> None:
        tags = {tag.k: tag.v for tag in node.tags}
        if not _has_address_tags(tags):
            return
        self.counts["address_nodes_seen"] += 1
        key = address_key_from_tags(tags)
        if key is None:
            self.counts["address_nodes_incomplete"] += 1
            return
        if not node.location.valid():
            self.counts["address_nodes_no_location"] += 1
            return
        point = Point(float(node.location.lon), float(node.location.lat))
        if not self.boundary.covers(point):
            self.counts["address_nodes_outside_mask"] += 1
            return
        entity = _entity("node", node)
        self.address_nodes.append(Candidate(entity, _addr_tags(tags), key, None, point, "address_node"))
        self.address_entity_ids.add(("node", int(node.id)))

    def way(self, way: Any) -> None:
        tags = {tag.k: tag.v for tag in way.tags}
        if _has_address_tags(tags):
            self.counts["address_ways_seen"] += 1
            self.address_entity_ids.add(("way", int(way.id)))

    def relation(self, relation: Any) -> None:
        tags = {tag.k: tag.v for tag in relation.tags}
        if _has_address_tags(tags):
            self.counts["address_relations_seen"] += 1
            self.address_entity_ids.add(("relation", int(relation.id)))

    def area(self, area: Any) -> None:
        tags = {tag.k: tag.v for tag in area.tags}
        osm_type = "way" if area.from_way() else "relation"
        osm_id = int(area.orig_id())
        identity = (osm_type, osm_id)
        if _has_address_tags(tags):
            self.area_ids.add(identity)
        has_building = bool(tags.get("building")) and tags.get("building", "").casefold() not in {"no", "false", "0"}
        if not has_building and not _has_address_tags(tags):
            return
        try:
            geometry = shapely.from_wkb(bytes.fromhex(self.factory.create_multipolygon(area)))
        except Exception:
            self.counts["area_geometry_errors"] += 1
            return
        clipped = _clip_polygon(geometry, self.boundary)
        if clipped is None:
            if _has_address_tags(tags):
                self.counts["address_areas_outside_or_empty"] += 1
            return
        entity = {
            "osm_type": osm_type,
            "osm_id": osm_id,
            "osm_version": int(area.version),
            "osm_timestamp": _timestamp(area.timestamp),
        }
        candidate = Candidate(entity, _addr_tags(tags), address_key_from_tags(tags), clipped, None,
                              "building_area" if has_building else "address_area")
        if has_building:
            self.building_areas.append(candidate)
        if _has_address_tags(tags):
            self.counts["address_areas_seen"] += 1
            if candidate.key is None:
                self.counts["address_areas_incomplete"] += 1
            else:
                self.address_areas.append(candidate)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            stream.write("\n")


def build_index(pbf: Path, pbf_manifest_path: Path, boundary_path: Path,
                boundary_manifest_path: Path, output_dir: Path) -> dict[str, Any]:
    if osmium is None:
        raise RuntimeError("pyosmium is required to build the index; use the local geodata environment")
    output_dir.mkdir(parents=True, exist_ok=True)
    unique_path = output_dir / "addresses.jsonl"
    ambiguous_path = output_dir / "ambiguous.jsonl"
    manifest_path = output_dir / "manifest.json"
    if any(path.exists() for path in (unique_path, ambiguous_path, manifest_path)):
        raise FileExistsError(f"refusing to overwrite existing index under {output_dir}")

    pbf_manifest = json.loads(pbf_manifest_path.read_text(encoding="utf-8"))
    pbf_hash = _sha256(pbf)
    expected_pbf_hash = pbf_manifest["extraction"]["sha256"]
    if pbf_hash != expected_pbf_hash:
        raise ValueError("PBF SHA256 does not match its pinned extraction manifest")
    boundary_manifest = json.loads(boundary_manifest_path.read_text(encoding="utf-8"))
    boundary_hash = _sha256(boundary_path)
    expected_boundary_hash = boundary_manifest["output_sha256"]
    if boundary_hash != expected_boundary_hash:
        raise ValueError("boundary GeoJSON SHA256 does not match its pinned manifest")
    boundary_doc = json.loads(boundary_path.read_text(encoding="utf-8"))
    boundary = shape(boundary_doc["features"][0]["geometry"])
    if not boundary.is_valid or boundary.is_empty:
        raise ValueError("pinned Moscow boundary must be valid and nonempty")

    handler = AddressHandler(boundary)
    handler.apply_file(str(pbf), locations=True, idx="flex_mem")
    handler.counts["address_entities_without_area_geometry"] = sum(
        1 for identity in handler.address_entity_ids
        if identity[0] != "node" and identity not in handler.area_ids
    )
    unique, ambiguous = resolve_candidates(
        handler.address_nodes, handler.address_areas, handler.building_areas
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="local-address-index-", dir=output_dir) as temp_name:
        temp_dir = Path(temp_name)
        temp_unique = temp_dir / "addresses.jsonl"
        temp_ambiguous = temp_dir / "ambiguous.jsonl"
        temp_manifest = temp_dir / "manifest.json"
        _write_jsonl(temp_unique, unique)
        _write_jsonl(temp_ambiguous, ambiguous)
        created = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        source = pbf_manifest["source"]
        manifest = {
            "dataset": "strict local OSM Moscow address index",
            "index_version": INDEX_VERSION,
            "created_at_utc": created,
            "inputs": {
                "pbf": {
                    "path": str(pbf.relative_to(ROOT)) if pbf.is_relative_to(ROOT) else pbf.name,
                    "bytes": pbf.stat().st_size,
                    "sha256": pbf_hash,
                    "source_dataset": source.get("dataset"),
                    "source_last_modified": source.get("source_last_modified"),
                    "osm_replication_timestamp": source.get("osm_replication_timestamp"),
                    "data_last_object_timestamp": pbf_manifest["extraction"].get("data_last_object_timestamp"),
                    "license": source.get("license", "ODbL-1.0"),
                    "attribution": source.get("attribution", "© OpenStreetMap contributors"),
                },
                "city_mask": {
                    "path": str(boundary_path.relative_to(ROOT)) if boundary_path.is_relative_to(ROOT) else boundary_path.name,
                    "sha256": boundary_hash,
                    "osm_relation_id": boundary_manifest.get("relation_id"),
                    "osm_relation_version": boundary_manifest.get("relation_version"),
                    "osm_relation_timestamp": boundary_manifest.get("relation_timestamp_utc"),
                    "legal_boundary_certification": boundary_manifest.get("boundary_is_authoritative_legal_source", False),
                },
            },
            "scope": "Records are retained only when their representative geometry is covered by the pinned Moscow OSM relation 102269 mask.",
            "normalization": {
                "unicode": "NFC",
                "case": "casefold",
                "whitespace": "trim and collapse runs",
                "street_type_policy": "fixed Russian alias allowlist and prefix/suffix canonicalization; street type and name retained",
                "housenumber_policy": "whole addr:housenumber value; punctuation, ё, корпус/строение/владение text preserved",
                "optional_key_tags": {
                    "block_key": "addr:block",
                    "building_key": "addr:building",
                    "conscriptionnumber_key": "addr:conscriptionnumber",
                },
                "key_order": ["street_key", "housenumber_key", "block_key", "building_key", "conscriptionnumber_key"],
                "fuzzy_matching": False,
            },
            "representative_point_policy": {
                "building_or_address_polygon": "Shapely representative_point (GEOS PointOnSurface) computed on geometry clipped to mask",
                "standalone_address_node": "source node coordinate",
                "node_to_building": "strict contains() and exactly one building polygon; no nearest fallback",
            },
            "counts": {
                **dict(sorted(handler.counts.items())),
                "address_nodes_indexed_input": len(handler.address_nodes),
                "address_area_candidates_input": len(handler.address_areas),
                "building_polygons_input": len(handler.building_areas),
                "unique_address_keys": len(unique),
                "ambiguous_candidates": len(ambiguous),
            },
            "outputs": {
                "addresses": {"file": unique_path.name, "rows": len(unique), "bytes": temp_unique.stat().st_size,
                              "sha256": _sha256(temp_unique)},
                "ambiguous": {"file": ambiguous_path.name, "rows": len(ambiguous), "bytes": temp_ambiguous.stat().st_size,
                              "sha256": _sha256(temp_ambiguous)},
            },
            "dependency_versions": {
                "pyosmium": importlib.metadata.version("osmium"),
                "shapely": shapely.__version__,
            },
        }
        temp_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        # Output names are new, so these renames publish the set atomically per file.
        os.replace(temp_unique, unique_path)
        os.replace(temp_ambiguous, ambiguous_path)
        os.replace(temp_manifest, manifest_path)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pbf", type=Path, default=DEFAULT_PBF)
    parser.add_argument("--pbf-manifest", type=Path, default=DEFAULT_PBF_MANIFEST)
    parser.add_argument("--boundary", type=Path, default=DEFAULT_BOUNDARY)
    parser.add_argument("--boundary-manifest", type=Path, default=DEFAULT_BOUNDARY_MANIFEST)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    for path in (args.pbf, args.pbf_manifest, args.boundary, args.boundary_manifest):
        if not path.is_file():
            parser.error(f"required file does not exist: {path}")
    try:
        manifest = build_index(args.pbf, args.pbf_manifest, args.boundary,
                               args.boundary_manifest, args.output_dir)
    except (FileExistsError, ValueError, KeyError, OSError, RuntimeError) as error:
        parser.error(str(error))
    print(json.dumps(manifest["counts"], ensure_ascii=False, sort_keys=True))
    print(f"wrote {args.output_dir / 'addresses.jsonl'}")
    print(f"wrote {args.output_dir / 'ambiguous.jsonl'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
