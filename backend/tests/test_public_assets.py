"""Public GLB redaction retains render bytes and local feature picking IDs."""

from __future__ import annotations

import json
import struct

from backend.store import ASSET_ROOT
from pipeline.publish_public_assets import sanitize_glb


def chunks(blob: bytes) -> tuple[dict, bytes]:
    size, kind = struct.unpack_from("<I4s", blob, 12)
    assert kind == b"JSON"
    document = json.loads(blob[20:20 + size])
    binary = blob[20 + size:]
    assert struct.unpack_from("<I4s", binary)[1] == b"BIN\x00"
    return document, binary


def test_public_glb_redaction_preserves_binary_and_feature_indices() -> None:
    source = (ASSET_ROOT / "citywide-v3/cell_3761_5575.glb").read_bytes()
    original, raw_binary = chunks(source)
    assert original["asset"]["extras"]["source_ids"]
    revised, changed = sanitize_glb(source)
    assert changed and revised != source
    public, public_binary = chunks(revised)
    assert public_binary == raw_binary
    assert struct.unpack_from("<4sII", revised) == (b"glTF", 2, len(revised))
    assert "source_ids" not in public["asset"]["extras"]
    assert "source_ids" not in public["nodes"][0]["extras"]
    assert "feature_id_mapping" not in public["asset"]["extras"]
    assert public["meshes"] == original["meshes"]
    assert public["accessors"] == original["accessors"]
    assert public["materials"] == original["materials"]
    assert sanitize_glb(revised) == (revised, False)
