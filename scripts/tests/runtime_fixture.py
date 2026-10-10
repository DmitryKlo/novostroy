"""Small pinned routing inputs shared by package/deploy integration fixtures."""
import hashlib
import json


def routing_contents() -> dict[str, bytes]:
    source = {"sha256": "1" * 64}
    payloads = {
        "data/social/osm_moscow_social_pois_261008.parquet": b"fixture POIs",
        "data/social/osm_moscow_roads_261008.parquet": b"fixture pedestrian roads",
        "data/social/osm_moscow_water_261008_v2.parquet": b"fixture water polygons",
        "data/social/osm_moscow_waterways_261008_v2.parquet": b"fixture waterways",
        "data/social/osm_moscow_parks_261008_v2.parquet": b"fixture park polygons",
        "data/social/valhalla_moscow/valhalla_pedestrian.json": b"{}",
    }
    def descriptor(path: str) -> dict:
        return {"file": path, "sha256": hashlib.sha256(payloads[path]).hexdigest()}
    documents = {
        "data/social/osm_moscow_social_pois_261008.manifest.json": {
            "source": source, "output": descriptor("data/social/osm_moscow_social_pois_261008.parquet")},
        "data/social/osm_moscow_roads_261008.manifest.json": {
            "source": source, "output": descriptor("data/social/osm_moscow_roads_261008.parquet")},
        "data/social/osm_moscow_surfaces_261008_v2.manifest.json": {
            "source": source, "layers": {name: descriptor(f"data/social/osm_moscow_{name}_261008_v2.parquet")
                                          for name in ("water", "waterways", "parks")}},
        "data/social/valhalla_moscow/build_manifest.json": {
            "source": source, "builder": {"config_sha256": hashlib.sha256(b"{}").hexdigest()}},
    }
    payloads.update({path: json.dumps(body).encode() for path, body in documents.items()})
    return payloads
