"""Build a snapshot-pinned metro station-to-entrance sidecar from OSM relations.

Only explicit direct node co-membership in a ``public_transport=stop_area``
relation can create a link. This reader never infers links from names, refs,
distance, or route results. Inputs are immutable local source snapshots; the
versioned outputs refuse to overwrite an existing artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import osmium
import pyarrow.parquet as pq


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PBF = ROOT / "data/social/osm_moscow_city_261008.osm.pbf"
DEFAULT_PBF_MANIFEST = ROOT / "data/social/osm_moscow_city_261008.manifest.json"
DEFAULT_POI_TABLE = ROOT / "data/social/osm_moscow_social_pois_261008.parquet"
DEFAULT_POI_MANIFEST = ROOT / "data/social/osm_moscow_social_pois_261008.manifest.json"
DEFAULT_OUTPUT = ROOT / "data/social/metro_station_access_261008.json"
DEFAULT_OUTPUT_MANIFEST = ROOT / "data/social/metro_station_access_261008.manifest.json"
MAX_PBF_BYTES = 100_000_000
MAX_POI_TABLE_BYTES = 25_000_000


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def _source_id(osm_id: int, category: str) -> str:
    return f"osm:node:{osm_id}:{category}"


def _poi_node_ids(poi_table: Path) -> tuple[set[int], set[int], dict[str, int]]:
    table = pq.read_table(poi_table, columns=["osm_type", "osm_id", "category"])
    stations: set[int] = set()
    entrances: set[int] = set()
    non_node_counts: Counter[str] = Counter()
    for row in table.to_pylist():
        category = row["category"]
        if category not in {"metro_station", "metro_entrance"}:
            continue
        if row["osm_type"] != "node":
            non_node_counts[category] += 1
            continue
        osm_id = row["osm_id"]
        if isinstance(osm_id, bool) or not isinstance(osm_id, int) or osm_id <= 0:
            raise ValueError(f"Invalid OSM node ID for {category}")
        (stations if category == "metro_station" else entrances).add(osm_id)
    return stations, entrances, dict(non_node_counts)


class _StopAreaCollector(osmium.SimpleHandler):
    """Keep only stop-area relations that mention current metro POI node IDs."""

    def __init__(self, station_ids: set[int], entrance_ids: set[int]) -> None:
        super().__init__()
        self.station_ids = station_ids
        self.entrance_ids = entrance_ids
        self.relations_seen = 0
        self.relations_touching_poi = 0
        self.membership_rows: list[dict[str, Any]] = []

    def relation(self, relation: Any) -> None:
        tags = {tag.k: tag.v for tag in relation.tags}
        if tags.get("public_transport") != "stop_area":
            return
        self.relations_seen += 1
        station_members: set[int] = set()
        entrance_members: set[int] = set()
        for member in relation.members:
            if member.type != "n":
                continue
            node_id = int(member.ref)
            if node_id in self.station_ids:
                station_members.add(node_id)
            if node_id in self.entrance_ids:
                entrance_members.add(node_id)
        if not station_members and not entrance_members:
            return
        self.relations_touching_poi += 1
        self.membership_rows.append(
            {
                "relation_id": int(relation.id),
                "station_ids": sorted(station_members),
                "entrance_ids": sorted(entrance_members),
            }
        )


def build_sidecar(
    *,
    source_pbf: Path = DEFAULT_PBF,
    pbf_manifest_path: Path = DEFAULT_PBF_MANIFEST,
    poi_table: Path = DEFAULT_POI_TABLE,
    poi_manifest_path: Path = DEFAULT_POI_MANIFEST,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read one bounded PBF pass and return sidecar plus provenance manifest."""
    source_pbf = source_pbf.resolve()
    pbf_manifest_path = pbf_manifest_path.resolve()
    poi_table = poi_table.resolve()
    poi_manifest_path = poi_manifest_path.resolve()
    for path in (source_pbf, pbf_manifest_path, poi_table, poi_manifest_path):
        if not path.is_file():
            raise FileNotFoundError(path)
    if source_pbf.stat().st_size > MAX_PBF_BYTES:
        raise ValueError(f"Refusing PBF larger than {MAX_PBF_BYTES} bytes")
    if poi_table.stat().st_size > MAX_POI_TABLE_BYTES:
        raise ValueError(f"Refusing POI table larger than {MAX_POI_TABLE_BYTES} bytes")

    pbf_manifest = _read_json(pbf_manifest_path)
    poi_manifest = _read_json(poi_manifest_path)
    pbf_sha256 = _sha256(source_pbf)
    poi_sha256 = _sha256(poi_table)
    extraction = pbf_manifest.get("extraction") or {}
    poi_source = poi_manifest.get("source") or {}
    poi_output = poi_manifest.get("output") or {}
    if extraction.get("sha256") != pbf_sha256:
        raise ValueError("PBF hash differs from its immutable source manifest")
    if poi_source.get("sha256") != pbf_sha256:
        raise ValueError("POI table and PBF are from different source snapshots")
    if poi_output.get("sha256") != poi_sha256:
        raise ValueError("POI table hash differs from its immutable manifest")
    snapshot_at = poi_source.get("osm_snapshot_timestamp")
    if not isinstance(snapshot_at, str) or not snapshot_at.endswith("Z"):
        raise ValueError("POI source manifest has no UTC OSM snapshot timestamp")

    station_ids, entrance_ids, non_node_counts = _poi_node_ids(poi_table)
    if not station_ids or not entrance_ids:
        raise ValueError("The POI index has no metro station or entrance node IDs")

    collector = _StopAreaCollector(station_ids, entrance_ids)
    collector.apply_file(str(source_pbf), locations=False)

    station_relations: dict[int, set[int]] = defaultdict(set)
    entrance_relations: dict[int, set[int]] = defaultdict(set)
    station_relation_members: dict[int, list[dict[str, Any]]] = defaultdict(list)
    multi_station_relations = 0
    relations_with_station_and_entrance = 0
    multi_station_members: set[int] = set()
    for row in collector.membership_rows:
        relation_id = row["relation_id"]
        stations = row["station_ids"]
        entrances = row["entrance_ids"]
        for station_id in stations:
            station_relations[station_id].add(relation_id)
        for entrance_id in entrances:
            entrance_relations[entrance_id].add(relation_id)
        if len(stations) > 1:
            multi_station_relations += 1
            multi_station_members.update(stations)
        if stations and entrances:
            relations_with_station_and_entrance += 1
        if len(stations) == 1:
            station_relation_members[stations[0]].append(row)

    ambiguous_stations = {
        station_id
        for station_id, relation_ids in station_relations.items()
        if len(relation_ids) != 1
    } | multi_station_members
    # An entrance is ambiguous if it participates in several stop areas, or
    # any stop area that names multiple mapped metro station nodes.
    multi_station_relation_entrances = {
        entrance_id
        for row in collector.membership_rows
        if len(row["station_ids"]) > 1
        for entrance_id in row["entrance_ids"]
    }
    ambiguous_entrances = {
        entrance_id
        for entrance_id, relation_ids in entrance_relations.items()
        if len(relation_ids) != 1
    } | multi_station_relation_entrances

    output_rows: list[dict[str, Any]] = []
    used_entrances: set[int] = set()
    for station_id in sorted(station_ids):
        relation_ids = station_relations.get(station_id, set())
        if station_id in ambiguous_stations or len(relation_ids) != 1:
            continue
        relation_id = next(iter(relation_ids))
        matching = [
            row for row in station_relation_members.get(station_id, [])
            if row["relation_id"] == relation_id
            and len(row["station_ids"]) == 1
        ]
        if len(matching) != 1:
            continue
        entrance_members = [
            entrance_id
            for entrance_id in matching[0]["entrance_ids"]
            if entrance_id not in ambiguous_entrances
            and entrance_relations.get(entrance_id) == {relation_id}
        ]
        if not entrance_members:
            continue
        if used_entrances.intersection(entrance_members):
            raise ValueError("An entrance would be linked to more than one station")
        used_entrances.update(entrance_members)
        output_rows.append(
            {
                "station_source_id": _source_id(station_id, "metro_station"),
                "entrance_source_ids": [
                    _source_id(entrance_id, "metro_entrance")
                    for entrance_id in entrance_members
                ],
                "relation_source_id": f"osm:relation:{relation_id}",
                "link_basis": "explicit_osm_relation_membership",
            }
        )

    linked_station_ids = {
        int(row["station_source_id"].split(":")[2]) for row in output_rows
    }
    sidecar = {
        "schema_version": 1,
        "dataset_id": "metro_station_access",
        "poi_table_sha256": poi_sha256,
        "osm_snapshot_at": snapshot_at,
        "stations": output_rows,
    }
    coverage = {
        "poi_station_nodes": len(station_ids),
        "poi_entrance_nodes": len(entrance_ids),
        "non_node_poi_records": non_node_counts,
        "stop_area_relations_seen": collector.relations_seen,
        "stop_area_relations_touching_metro_pois": collector.relations_touching_poi,
        "relations_with_station_and_entrance": relations_with_station_and_entrance,
        "relations_with_multiple_station_nodes": multi_station_relations,
        "stations_linked": len(linked_station_ids),
        "entrances_linked": len(used_entrances),
        "stations_unlinked": len(station_ids) - len(linked_station_ids),
        "entrances_unlinked": len(entrance_ids) - len(used_entrances),
        "stations_with_multiple_stop_area_relations": sum(
            len(values) > 1 for values in station_relations.values()
        ),
        "entrances_with_multiple_stop_area_relations": sum(
            len(values) > 1 for values in entrance_relations.values()
        ),
        "ambiguous_stations_omitted": len(ambiguous_stations),
        "ambiguous_entrances_omitted": len(ambiguous_entrances),
        "entrance_unique_link_invariant": len(used_entrances)
        == sum(len(row["entrance_source_ids"]) for row in output_rows),
    }
    if not output_rows:
        raise RuntimeError("No explicit unambiguous station-to-entrance links found")
    if not coverage["entrance_unique_link_invariant"]:
        raise ValueError("An entrance appears more than once in the sidecar")

    manifest = {
        "schema_version": 1,
        "dataset_id": "metro_station_access",
        "source": {
            "file": str(source_pbf.relative_to(ROOT)),
            "sha256": pbf_sha256,
            "osm_snapshot_timestamp": snapshot_at,
            "clip_relation_id": (pbf_manifest.get("boundary") or {}).get("osm_relation_id"),
            "clip_relation_version": (pbf_manifest.get("boundary") or {}).get("relation_version"),
            "license": poi_source.get("license", "ODbL-1.0"),
            "attribution": poi_source.get("attribution", "© OpenStreetMap contributors"),
        },
        "poi_index": {
            "file": str(poi_table.relative_to(ROOT)),
            "sha256": poi_sha256,
            "manifest_file": str(poi_manifest_path.relative_to(ROOT)),
            "manifest_sha256": _sha256(poi_manifest_path),
        },
        "output": {
            "file": str(DEFAULT_OUTPUT.relative_to(ROOT)),
            "sidecar_sha256": None,
            "stations": len(output_rows),
            "entrances": len(used_entrances),
        },
        "coverage": coverage,
        "link_policy": (
            "Only direct node members of the same public_transport=stop_area relation; "
            "one mapped metro station per relation; omit stations/entrances with "
            "ambiguous stop-area membership. No name, ref, or proximity inference."
        ),
        "route_policy": (
            "Consumers keep the station marker at its POI-table coordinate and may "
            "route only to linked metro_entrance source points. Select among linked "
            "entrances by walking-route result or straight-line prefilter; do not show "
            "entrances as separate stations."
        ),
    }
    return sidecar, manifest


def _write_immutable(path: Path, payload: dict[str, Any]) -> str:
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite immutable output {path}")
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary = path.with_name(path.name + ".partial")
    if temporary.exists():
        raise FileExistsError(f"Refusing to overwrite temporary output {temporary}")
    try:
        temporary.write_bytes(encoded)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return hashlib.sha256(encoded).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-pbf", type=Path, default=DEFAULT_PBF)
    parser.add_argument("--pbf-manifest", type=Path, default=DEFAULT_PBF_MANIFEST)
    parser.add_argument("--poi-table", type=Path, default=DEFAULT_POI_TABLE)
    parser.add_argument("--poi-manifest", type=Path, default=DEFAULT_POI_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--output-manifest", type=Path, default=DEFAULT_OUTPUT_MANIFEST)
    args = parser.parse_args()
    output = args.output.resolve()
    output_manifest = args.output_manifest.resolve()
    if output.exists() or output_manifest.exists():
        raise FileExistsError("Refusing to overwrite an existing metro access sidecar or manifest")
    sidecar, manifest = build_sidecar(
        source_pbf=args.source_pbf,
        pbf_manifest_path=args.pbf_manifest,
        poi_table=args.poi_table,
        poi_manifest_path=args.poi_manifest,
    )
    manifest["output"]["file"] = str(output.relative_to(ROOT))
    # Check both targets before writing either immutable output.
    if output.exists() or output_manifest.exists():
        raise FileExistsError("Refusing to overwrite an existing metro access sidecar or manifest")
    sidecar_hash = _write_immutable(output, sidecar)
    manifest["output"]["sidecar_sha256"] = sidecar_hash
    try:
        _write_immutable(output_manifest, manifest)
    except BaseException:
        # Leave no orphan artifact if the paired manifest cannot be published.
        output.unlink(missing_ok=True)
        raise
    print(json.dumps({"output": str(output), "manifest": str(output_manifest),
                      "sha256": sidecar_hash, "coverage": manifest["coverage"]},
                     ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
