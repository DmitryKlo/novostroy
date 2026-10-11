import hashlib
import json
import struct

import pytest

from scripts.runtime_overlay_styles import verify_overlay_styles
from scripts.tests.test_package_deploy_data import _fixture
from scripts.tests.test_runtime_model_styles import stage_model_styles
from scripts.tests.test_runtime_overlay import _tile


def _b3dm(document, binary, style_id, registry_ids=None, styles=None):
    registry_ids = registry_ids or ["r1"]
    styles = styles or [style_id]
    encoded = json.dumps(document).encode()
    encoded += b" " * (-len(encoded) % 4)
    binary += b"\0" * (-len(binary) % 4)
    glb = (struct.pack("<4sII", b"glTF", 2, 28 + len(encoded) + len(binary))
           + struct.pack("<II", len(encoded), 0x4E4F534A) + encoded
           + struct.pack("<II", len(binary), 0x004E4942) + binary)
    ft = json.dumps({"BATCH_LENGTH": len(registry_ids)}).encode()
    ft += b" " * (-(28 + len(ft)) % 8)
    bt = json.dumps({"registry_id": registry_ids, "facade_style_id": styles}).encode()
    bt += b" " * (-len(bt) % 8)
    return struct.pack("<4s6I", b"b3dm", 1, 28 + len(ft) + len(bt) + len(glb),
                       len(ft), 0, len(bt), 0) + ft + bt + glb


@pytest.mark.parametrize("failure", [None, "deduplicated_image", "wrong_project", "batch_style", "wrong_image",
    "neighbor_primitive", "textured_flat", "external_buffer", "missing_uv", "wrong_count"])
def test_embedded_near_style_is_bound_to_project_batch_and_wall_vertices(tmp_path, failure):
    _fixture(tmp_path)
    _, model_run, model_manifest, material, _, _, _ = stage_model_styles(tmp_path)
    pointer = json.loads((tmp_path / "data/property/developer_experience/active.json").read_text())
    near = material["lods"][0]
    png = (model_run / "styles" / near["asset_path"]).read_bytes()
    style_id = material["style_id"]
    feature = {"registry_id": "r1", "style_id": style_id, "render_policy": "persistent_volume",
               "publisher_project_id": material["publisher_project_id"],
               "facade_style_basis": "project_style_inference", "facade_texture_sha256": near["sha256"]}
    descriptor = {"basis": "project_style_inference", "label": "Реконструкция",
        "source_model_run_id": pointer["model_run_id"],
        "styles_materials_sha256": model_manifest["styles_materials_sha256"], "texture_level": "near",
        "styled_projects_total": 1, "styled_persistent_features_total": 1,
        "far_untextured": True, "near_textures_embedded_in_detail_tiles": True}
    binary = struct.pack("<Hxxff", 0, 0.0, 0.0) + png
    document = {"asset": {"version": "2.0"}, "buffers": [{"byteLength": len(binary)}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": 2},
                        {"buffer": 0, "byteOffset": 4, "byteLength": 8},
                        {"buffer": 0, "byteOffset": 12, "byteLength": len(png)}],
        "accessors": [{"bufferView": 0, "componentType": 5123, "count": 1, "type": "SCALAR"},
                      {"bufferView": 1, "componentType": 5126, "count": 1, "type": "VEC2"}],
        "images": [{"bufferView": 2, "mimeType": "image/png"}], "textures": [{"source": 0}],
        "materials": [{"name": "project_style_" + style_id,
                       "pbrMetallicRoughness": {"baseColorTexture": {"index": 0}}}],
        "meshes": [{"primitives": [{"material": 0, "attributes": {"_BATCHID": 0, "TEXCOORD_0": 1}}]}]}
    batch_style = style_id
    features = [feature]
    registry_ids, styles = ["r1"], [style_id]
    if failure == "deduplicated_image":
        second_style = "style_" + "e" * 24
        second_material = {**material, "style_id": second_style, "publisher_project_id": "second-project"}
        body = b"".join((json.dumps(row) + "\n").encode() for row in (material, second_material))
        (model_run / "styles/materials.jsonl").write_bytes(body)
        model_manifest.update(styles_materials_total=2, styles_materials_sha256=hashlib.sha256(body).hexdigest())
        model_bytes = json.dumps(model_manifest).encode()
        (model_run / "manifest.json").write_bytes(model_bytes)
        pointer["model_manifest_sha256"] = hashlib.sha256(model_bytes).hexdigest()
        descriptor.update(styles_materials_sha256=model_manifest["styles_materials_sha256"],
                          styled_projects_total=2, styled_persistent_features_total=2)
        features.append({**feature, "registry_id": "r2", "style_id": second_style,
                         "publisher_project_id": "second-project"})
        registry_ids.append("r2")
        styles.append(second_style)
        document["bufferViews"].append({"buffer": 0, "byteOffset": len(binary), "byteLength": 2})
        binary += struct.pack("<H", 1)
        document["buffers"][0]["byteLength"] = len(binary)
        document["accessors"].append({"bufferView": 3, "componentType": 5123, "count": 1, "type": "SCALAR"})
        document["materials"].append({"name": "project_style_" + second_style,
            "pbrMetallicRoughness": {"baseColorTexture": {"index": 0}}})
        document["meshes"][0]["primitives"].append({"material": 1,
            "attributes": {"_BATCHID": 2, "TEXCOORD_0": 1}})
    if failure == "wrong_project":
        feature["publisher_project_id"] = "neighbor-project"
    elif failure == "batch_style":
        batch_style = "style_" + "f" * 24
        styles = [batch_style]
    elif failure == "wrong_image":
        binary = binary[:-1] + bytes([binary[-1] ^ 1])
    elif failure == "neighbor_primitive":
        binary = b"\x01\x00" + binary[2:]
    elif failure == "external_buffer":
        document["buffers"][0]["uri"] = "https://external.invalid/tile.bin"
    elif failure == "missing_uv":
        del document["meshes"][0]["primitives"][0]["attributes"]["TEXCOORD_0"]
    elif failure == "wrong_count":
        descriptor["styled_projects_total"] = 2
    run = tmp_path / "overlay"
    run.mkdir()
    detail = _b3dm(document, binary, batch_style, registry_ids, styles)
    (run / "detail.b3dm").write_bytes(detail)
    (run / "flat.b3dm").write_bytes(detail if failure == "textured_flat" else _tile())
    inventory = [{"path": "flat.b3dm", "lod": "flat"}, {"path": "detail.b3dm", "lod": "detail"}]
    if failure in {None, "deduplicated_image"}:
        verify_overlay_styles(tmp_path, run, pointer, {"facade_styles": descriptor}, features, inventory)
    else:
        with pytest.raises(ValueError):
            verify_overlay_styles(tmp_path, run, pointer, {"facade_styles": descriptor}, features, inventory)
