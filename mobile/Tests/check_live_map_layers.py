"""HTTP smoke check for the shared cartographic 3D Tiles used by native Cesium."""

import json
import sys
import urllib.parse
import urllib.request


BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")


def get_json(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=15) as response:
        return json.load(response)


def first_glb_uri(node: dict) -> str | None:
    content = node.get("content")
    contents = [content] if isinstance(content, dict) else node.get("contents", [])
    for item in contents:
        uri = item.get("uri") or item.get("url")
        if isinstance(uri, str) and uri.lower().endswith(".glb"):
            return uri
    for child in node.get("children", []):
        found = first_glb_uri(child)
        if found:
            return found
    return None


def first_branch_glbs(tileset_url: str) -> list[str]:
    """Follow one branch through optional external tilesets (city v7)."""
    current_json_url = tileset_url
    node = get_json(current_json_url)["root"]
    seen_json = {tileset_url}
    glbs: list[str] = []
    for _ in range(12):
        content = node.get("content")
        if isinstance(content, dict):
            uri = content.get("uri") or content.get("url")
            assert isinstance(uri, str) and uri and not uri.startswith("/")
            assert ".." not in uri.split("/") and "?" not in uri and "#" not in uri
            resolved = urllib.parse.urljoin(current_json_url, uri)
            assert resolved.startswith(tileset_url.rsplit("/", 1)[0] + "/")
            if uri.endswith(".glb"):
                glbs.append(resolved)
            elif uri.endswith(".json"):
                assert resolved not in seen_json
                seen_json.add(resolved)
                current_json_url = resolved
                node = get_json(resolved)["root"]
                continue
            else:
                raise AssertionError(uri)
        if not node.get("children"):
            break
        node = node["children"][0]
    else:
        raise AssertionError(f"tileset branch too deep: {tileset_url}")
    return glbs


map_data = get_json("/v1/map")
expected = {
    "city_volumes": "synthetic_massing",
    "roads": "cartographic_ribbon",
    "parks": "cartographic_surface",
    "water": "cartographic_surface",
}
layers = {item["kind"]: item for item in map_data["tilesets"]
             if item["kind"] in expected}
city = layers.get("city_volumes")
if city and "/public-citywide-v8/" in city["url"]:
    expected["ground_buildings"] = "cartographic_surface"
    layers = {item["kind"]: item for item in map_data["tilesets"]
              if item["kind"] in expected}
assert set(layers) == set(expected), layers
for kind, quality in expected.items():
    layer = layers[kind]
    assert layer["model_quality"] == quality
    assert layer["url"].startswith("/assets/")
    assert layer["attribution"].strip(), kind
    tileset = get_json(layer["url"])
    assert tileset["asset"]["version"].startswith("1.")
    glb_urls = first_branch_glbs(layer["url"])
    assert glb_urls, kind
    for glb_url in glb_urls:
        content_url = BASE + glb_url
        parsed = urllib.parse.urlparse(content_url)
        assert content_url.startswith(BASE + "/assets/") and parsed.query == ""
        with urllib.request.urlopen(content_url, timeout=20) as response:
            assert response.read(4) == b"glTF", content_url
    print(kind, layer["url"], quality, "GLB reachable")

print("Native shared layer HTTP routes passed; Unity rendering remains untested.")
