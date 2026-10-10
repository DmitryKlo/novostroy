from pathlib import Path
import hashlib
import json
import pytest

from deploy.entrypoint import ROUTING_INPUT_FILES, verify_routing_inputs
from scripts.tests.runtime_fixture import routing_contents


def fixture(root: Path) -> dict[str, str]:
    hashes = {}
    for name, body in routing_contents().items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        hashes[name] = hashlib.sha256(body).hexdigest()
    return hashes


@pytest.mark.parametrize("missing", ROUTING_INPUT_FILES)
def test_connector_or_park_input_cannot_silently_disappear(tmp_path: Path, missing: str):
    fixture(tmp_path)
    (tmp_path / missing).unlink()
    with pytest.raises(ValueError, match="Missing pinned routing input"):
        verify_routing_inputs(tmp_path)


def test_barrier_bytes_and_cross_snapshot_mismatch_fail(tmp_path: Path):
    fixture(tmp_path)
    assert verify_routing_inputs(tmp_path)["source_sha256"] == "1" * 64
    (tmp_path / ROUTING_INPUT_FILES[2]).write_bytes(b"replaced water polygons")
    with pytest.raises(ValueError, match="Routing water bytes"):
        verify_routing_inputs(tmp_path)
    fixture(tmp_path)
    path = tmp_path / ROUTING_INPUT_FILES[5]
    manifest = json.loads(path.read_text())
    manifest["source"]["sha256"] = "2" * 64
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="source snapshots differ"):
        verify_routing_inputs(tmp_path)


def test_unlisted_input_is_rejected_even_if_disk_file_exists(tmp_path: Path):
    hashes = fixture(tmp_path)
    del hashes[ROUTING_INPUT_FILES[4]]
    with pytest.raises(ValueError, match="Missing pinned routing input"):
        verify_routing_inputs(tmp_path, hashes)


def test_graph_configuration_and_symlink_are_not_accepted(tmp_path: Path):
    fixture(tmp_path)
    (tmp_path / "data/social/valhalla_moscow/valhalla_pedestrian.json").write_bytes(b"changed")
    with pytest.raises(ValueError, match="graph configuration"):
        verify_routing_inputs(tmp_path)
    fixture(tmp_path)
    park = tmp_path / ROUTING_INPUT_FILES[4]
    park.rename(tmp_path / "external")
    park.symlink_to(tmp_path / "external")
    with pytest.raises(ValueError, match="symlink"):
        verify_routing_inputs(tmp_path)
