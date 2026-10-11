"""Bind embedded near facade textures to each exact-project tile feature."""

from __future__ import annotations

import hashlib
from pathlib import Path
import struct
from typing import Any

from scripts.runtime_overlay import _json, _regular_file


def _glb_parts(data: bytes) -> tuple[dict[str, Any], bytes, dict[str, Any]]:
    _, _, _, ftj, ftb, btj, btb = struct.unpack("<4s6I", data[:28])
    offset = 28 + ftj + ftb
    batch = _json(data[offset:offset + btj])
    glb = data[offset + btj + btb:]
    document = None
    binary = b""
    position = 12
    while position < len(glb):
        if position + 8 > len(glb):
            raise ValueError("Truncated styled overlay GLB chunk")
        length, kind = struct.unpack("<II", glb[position:position + 8])
        position += 8
        if position + length > len(glb):
            raise ValueError("Styled overlay GLB chunk exceeds tile")
        chunk = glb[position:position + length]
        if kind == 0x4E4F534A and document is None:
            document = _json(chunk)
        elif kind == 0x004E4942 and not binary:
            binary = chunk
        else:
            raise ValueError("Unexpected or duplicate styled overlay GLB chunk")
        position += length
    if not isinstance(document, dict) or not isinstance(batch, dict):
        raise ValueError("Invalid styled overlay GLB/batch document")
    return document, binary, batch


def verify_overlay_styles(root: Path, run: Path, pointer: dict[str, Any], manifest: dict[str, Any],
                           features: list[dict[str, Any]], inventory: list[dict[str, Any]]) -> None:
    descriptor = manifest.get("facade_styles")
    if descriptor is None:
        if any(row.get("style_id") is not None for row in features):
            raise ValueError("Styled overlay features lack their descriptor")
        return
    keys = {"basis", "label", "source_model_run_id", "styles_materials_sha256", "texture_level",
            "styled_projects_total", "styled_persistent_features_total", "far_untextured",
            "near_textures_embedded_in_detail_tiles"}
    if (not isinstance(descriptor, dict) or set(descriptor) != keys
            or descriptor["basis"] != "project_style_inference" or descriptor["label"] != "Реконструкция"
            or descriptor["source_model_run_id"] != pointer["model_run_id"]
            or descriptor["texture_level"] != "near" or descriptor["far_untextured"] is not True
            or descriptor["near_textures_embedded_in_detail_tiles"] is not True):
        raise ValueError("Invalid embedded overlay facade descriptor")
    prefix = f"data/property/developer_models/runs/{pointer['model_run_id']}"
    model_bytes = _regular_file(root, prefix + "/manifest.json").read_bytes()
    model_manifest = _json(model_bytes)
    material_bytes = _regular_file(root, prefix + "/styles/materials.jsonl").read_bytes()
    if (hashlib.sha256(model_bytes).hexdigest() != pointer["model_manifest_sha256"]
            or hashlib.sha256(material_bytes).hexdigest() != descriptor["styles_materials_sha256"]
            or descriptor["styles_materials_sha256"] != model_manifest.get("styles_materials_sha256")):
        raise ValueError("Overlay facade styles differ from the pinned model materials")
    materials = {row["style_id"]: row for row in map(_json, material_bytes.splitlines())}
    near = {style: next(lod for lod in material["lods"] if lod["level"] == "near")
            for style, material in materials.items()}
    by_id = {row["registry_id"]: row for row in features}
    styled = {key: row for key, row in by_id.items() if row.get("style_id") is not None}
    for row in styled.values():
        material = materials.get(row["style_id"])
        if (material is None or row.get("publisher_project_id") != material["publisher_project_id"]
                or row.get("render_policy") != "persistent_volume"
                or row.get("facade_style_basis") != "project_style_inference"
                or row.get("facade_texture_sha256") != near[row["style_id"]]["sha256"]):
            raise ValueError("Overlay styled feature differs from its exact project material")
    if (type(descriptor["styled_projects_total"]) is not int
            or type(descriptor["styled_persistent_features_total"]) is not int
            or descriptor["styled_projects_total"] != len({row["style_id"] for row in styled.values()})
            or descriptor["styled_persistent_features_total"] != len(styled) or not styled):
        raise ValueError("Overlay facade style coverage count differs")
    rendered: set[str] = set()
    for item in inventory:
        document, binary, batch = _glb_parts((run / item["path"]).read_bytes())
        registry_ids = batch["registry_id"]
        wanted = [styled[key]["style_id"] if key in styled else None for key in registry_ids]
        if item["lod"] == "flat":
            if document.get("images") or document.get("textures"):
                raise ValueError("Far overlay tiles must remain untextured")
            continue
        expected_styles = {style for style in wanted if style is not None}
        if not expected_styles:
            if document.get("images") or document.get("textures"):
                raise ValueError("Unstyled overlay tile has undeclared textures")
            continue
        if batch.get("facade_style_id") != wanted:
            raise ValueError("Overlay tile batch styles differ from feature identities")
        buffers = document.get("buffers")
        if (not isinstance(buffers, list) or len(buffers) != 1
                or set(buffers[0]) != {"byteLength"}
                or type(buffers[0]["byteLength"]) is not int
                or not 0 <= buffers[0]["byteLength"] <= len(binary)):
            raise ValueError("Styled overlay must use one embedded binary buffer")
        def view_bytes(index: int) -> bytes:
            views = document.get("bufferViews", [])
            if type(index) is not int or not 0 <= index < len(views):
                raise ValueError("Invalid styled overlay buffer view")
            view = views[index]
            start, size = view.get("byteOffset", 0), view.get("byteLength")
            if (view.get("buffer") != 0 or type(start) is not int or type(size) is not int
                    or start < 0 or size < 0 or start + size > len(binary)):
                raise ValueError("Styled overlay buffer view exceeds embedded bytes")
            return binary[start:start + size]
        images = document.get("images", [])
        image_hashes = []
        for image in images:
            if set(image) != {"bufferView", "mimeType"} or image["mimeType"] != "image/png":
                raise ValueError("Overlay facade image is not an embedded PNG")
            image_hashes.append(hashlib.sha256(view_bytes(image["bufferView"])).hexdigest())
        material_styles = {}
        used_images = set()
        for index, material in enumerate(document.get("materials", [])):
            texture = material.get("pbrMetallicRoughness", {}).get("baseColorTexture")
            if texture is None:
                continue
            name = material.get("name", "")
            if not isinstance(name, str) or not name.startswith("project_style_"):
                raise ValueError("Overlay facade material lacks its exact style identity")
            style = name.removeprefix("project_style_")
            try:
                texture_index = texture["index"]
                if type(texture_index) is not int or not 0 <= texture_index < len(document["textures"]):
                    raise ValueError("Invalid overlay facade texture index")
                image_index = document["textures"][texture_index]["source"]
                if type(image_index) is not int or not 0 <= image_index < len(image_hashes):
                    raise ValueError("Invalid overlay facade image index")
                digest = image_hashes[image_index]
            except (KeyError, IndexError, TypeError) as exc:
                raise ValueError("Invalid overlay facade image reference") from exc
            if style not in expected_styles or digest != near[style]["sha256"]:
                raise ValueError("Overlay embedded facade PNG differs from exact project style")
            material_styles[index] = style
            used_images.add(image_index)
        # Projects may share identical generated PNG bytes. The producer can
        # share one image across their separate, identity-bound materials.
        if (set(material_styles.values()) != expected_styles or not 0 < len(images) <= len(expected_styles)
                or used_images != set(range(len(images)))):
            raise ValueError("Overlay tile material/image style coverage differs")
        primitive_styles = set()
        for mesh in document.get("meshes", []):
            for primitive in mesh.get("primitives", []):
                style = material_styles.get(primitive.get("material"))
                if style is None:
                    continue
                try:
                    accessor = document["accessors"][primitive["attributes"]["_BATCHID"]]
                    data = view_bytes(accessor["bufferView"])
                except (KeyError, IndexError, TypeError) as exc:
                    raise ValueError("Styled overlay primitive lacks feature IDs") from exc
                count = accessor.get("count")
                if (accessor.get("componentType") != 5123 or accessor.get("type") != "SCALAR"
                        or accessor.get("byteOffset", 0) != 0 or type(count) is not int
                        or count <= 0 or len(data) != count * 2):
                    raise ValueError("Invalid styled overlay feature-ID accessor")
                try:
                    uv = document["accessors"][primitive["attributes"]["TEXCOORD_0"]]
                    uv_bytes = view_bytes(uv["bufferView"])
                except (KeyError, IndexError, TypeError) as exc:
                    raise ValueError("Styled overlay wall lacks texture coordinates") from exc
                if (uv.get("componentType") != 5126 or uv.get("type") != "VEC2"
                        or uv.get("count") != count or uv.get("byteOffset", 0) != 0
                        or len(uv_bytes) != count * 8):
                    raise ValueError("Styled overlay texture coordinates differ from wall vertices")
                indices = set(struct.unpack("<" + "H" * count, data))
                if any(index >= len(wanted) or wanted[index] != style for index in indices):
                    raise ValueError("Overlay wall material is assigned to a different building project")
                rendered.update(registry_ids[index] for index in indices)
                primitive_styles.add(style)
        if primitive_styles != expected_styles:
            raise ValueError("Overlay material lacks its styled wall primitive")
    if rendered != set(styled):
        raise ValueError("Overlay tiles omit styled persistent building walls")
