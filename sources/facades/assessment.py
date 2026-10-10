"""Assess facade/mesh leads without creating or publishing model assets.

Input is a private JSONL of source observations, not a web scraper. Local files
are inspected within an explicit root and byte budget. A picture, elevation,
panorama, viewer, or unscaled BIM file never silently becomes project geometry.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import os
import re
import struct
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit


RULES_VERSION = "facade-evidence-v1.1"
MAX_CANDIDATES = 1000
MAX_LINE_BYTES = 65536
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_HEADER_BYTES = 2 * 1024 * 1024
KINDS = {"mesh", "facade_image", "elevation_plan", "panorama", "site_plan", "design_report", "unknown"}
RIGHTS_VALUES = {"granted", "denied", "unknown"}
_ID = re.compile(r"^[a-z][a-z0-9_-]{2,79}$")
_FACADE = re.compile(r"(?:\bфасад\w*\b|\bархитектур\w*\s+(?:корпуса|решени\w*)\b|\bfacade\b|\bfaçade\b)", re.I)
_ELEVATION = re.compile(r"(?:\bфасад\w*\s+в\s+осях\b|\bчерт[её]ж\w*\s+фасад\w*\b|\belevation\s+(?:drawing|plan)\b)", re.I)
_PANORAMA = re.compile(r"(?:\bпанорам\w*\b|\b360[° ]?\b|\bpanorama\b)", re.I)
_MESH = re.compile(r"(?:\bmesh\b|\b3d[- ]?model\b|\bifc\b|\bglb\b|\bмодел\w*\s+корпус\w*\b)", re.I)
_SITE_PLAN = re.compile(r"(?:\bплан\s+(?:проведения\s+)?(?:археологическ\w*|сет(?:ей|и)\b|участка\b)|\bsite\s+plan\b)", re.I)
_DESIGN_REPORT = re.compile(r"(?:\bотч[её]т\b|\breport\b)", re.I)


def _timestamp(value: Any, field: str, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a timezone-aware timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be a timezone-aware timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _text(value: Any, field: str, *, limit: int = 512) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    cleaned = value.strip()
    if len(cleaned) > limit:
        raise ValueError(f"{field} exceeds {limit} characters")
    return cleaned or None


def _url(value: Any, field: str) -> str | None:
    original = _text(value, field, limit=4096)
    if original is None:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in original):
        raise ValueError(f"{field} contains control characters")
    parsed = urlsplit(original)
    host = (parsed.hostname or "").casefold().rstrip(".")
    if parsed.scheme != "https" or not host or parsed.username or parsed.password or parsed.port not in {None, 443}:
        raise ValueError(f"{field} must be a credential-free HTTPS URL")
    if host == "localhost" or host.endswith((".local", ".internal", ".localhost")) or "." not in host:
        raise ValueError(f"{field} must not reference a local host")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError(f"{field} must use a public hostname")
    return original


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _png_size(header: bytes) -> tuple[int, int] | None:
    if len(header) < 24 or not header.startswith(b"\x89PNG\r\n\x1a\n") or header[12:16] != b"IHDR":
        return None
    width, height = struct.unpack_from(">II", header, 16)
    return (width, height) if 0 < width <= 100000 and 0 < height <= 100000 else None


def _jpeg_size(header: bytes) -> tuple[int, int] | None:
    if not header.startswith(b"\xff\xd8"):
        return None
    offset = 2
    while offset + 4 <= len(header):
        if header[offset] != 0xFF:
            return None
        while offset < len(header) and header[offset] == 0xFF:
            offset += 1
        if offset >= len(header):
            return None
        marker = header[offset]
        offset += 1
        if marker in {0xD8, 0xD9}:
            continue
        if offset + 2 > len(header):
            return None
        length = struct.unpack_from(">H", header, offset)[0]
        if length < 2 or offset + length > len(header):
            return None
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            if length < 7:
                return None
            height, width = struct.unpack_from(">HH", header, offset + 3)
            return (width, height) if 0 < width <= 100000 and 0 < height <= 100000 else None
        offset += length
    return None


def _glb_summary(path: Path, header: bytes, size: int) -> dict[str, Any]:
    if len(header) < 28 or struct.unpack_from("<4sII", header) != (b"glTF", 2, size):
        raise ValueError("invalid_glb_header_or_length")
    json_bytes, json_kind = struct.unpack_from("<I4s", header, 12)
    if json_kind != b"JSON" or json_bytes > MAX_HEADER_BYTES - 28 or json_bytes < 2:
        raise ValueError("invalid_glb_json_chunk")
    if 20 + json_bytes + 8 > size or 20 + json_bytes + 8 > len(header):
        raise ValueError("truncated_glb_json_chunk")
    binary_bytes, binary_kind = struct.unpack_from("<I4s", header, 20 + json_bytes)
    if binary_kind != b"BIN\x00" or 20 + json_bytes + 8 + binary_bytes != size:
        raise ValueError("invalid_glb_bin_chunk")
    document = json.loads(header[20:20 + json_bytes])
    if document.get("asset", {}).get("version") != "2.0":
        raise ValueError("unsupported_gltf_version")
    accessors = document.get("accessors") or []
    views = document.get("bufferViews") or []
    buffers = document.get("buffers") or []
    if not buffers or not isinstance(buffers[0].get("byteLength"), int) or not 0 < buffers[0]["byteLength"] <= binary_bytes:
        raise ValueError("invalid_glb_buffer_length")
    with path.open("rb") as stream:
        stream.seek(20 + json_bytes + 8)
        binary = stream.read(binary_bytes)
    if len(binary) != binary_bytes:
        raise ValueError("truncated_glb_bin_chunk")

    def values(accessor_index: int, component_type: int, kind: str):
        if not isinstance(accessor_index, int) or not 0 <= accessor_index < len(accessors):
            raise ValueError("invalid_glb_accessor")
        accessor = accessors[accessor_index]
        if accessor.get("sparse") or accessor.get("type") != kind or accessor.get("componentType") != component_type:
            raise ValueError("unsupported_glb_accessor")
        count = accessor.get("count")
        if not isinstance(count, int) or not 0 < count <= 1_000_000:
            raise ValueError("invalid_glb_accessor_count")
        view_index = accessor.get("bufferView")
        if not isinstance(view_index, int) or not 0 <= view_index < len(views):
            raise ValueError("missing_glb_buffer_view")
        view = views[view_index]
        if view.get("buffer") != 0:
            raise ValueError("unsupported_glb_buffer")
        struct_code = {5126: "f", 5123: "H", 5125: "I"}.get(component_type)
        if struct_code is None:
            raise ValueError("unsupported_glb_component_type")
        width = struct.calcsize("<" + struct_code) * (3 if kind == "VEC3" else 1)
        stride = view.get("byteStride", width)
        offset = view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
        view_start = view.get("byteOffset", 0)
        view_length = view.get("byteLength")
        if (not all(isinstance(value, int) for value in (stride, offset, view_start, view_length))
                or stride < width or stride > 252 or offset < view_start
                or view_start < 0 or offset + (count - 1) * stride + width > view_start + view_length
                or view_start + view_length > len(binary)):
            raise ValueError("glb_accessor_outside_bin")
        unpack = struct.Struct("<" + struct_code * (3 if kind == "VEC3" else 1)).unpack_from
        for item_index in range(count):
            yield unpack(binary, offset + item_index * stride)

    primitives = [primitive for mesh in document.get("meshes", []) for primitive in mesh.get("primitives", [])]
    positions = []
    triangles = 0
    for primitive in primitives:
        if primitive.get("mode", 4) != 4:
            continue
        index = primitive.get("attributes", {}).get("POSITION")
        if index is None:
            continue
        point_count = accessors[index].get("count") if isinstance(index, int) and 0 <= index < len(accessors) else 0
        if not all(math.isfinite(coordinate) for point in values(index, 5126, "VEC3") for coordinate in point):
            raise ValueError("glb_nonfinite_position")
        if "indices" in primitive:
            index_id = primitive["indices"]
            if not isinstance(index_id, int) or not 0 <= index_id < len(accessors):
                raise ValueError("invalid_glb_index_accessor")
            index_accessor = accessors[index_id]
            component = index_accessor.get("componentType")
            if component not in {5123, 5125}:
                raise ValueError("unsupported_glb_index_component")
            index_count = index_accessor.get("count", 0)
            if any(item[0] >= point_count for item in values(index_id, component, "SCALAR")):
                raise ValueError("glb_index_outside_positions")
        else:
            index_count = point_count
        if index_count % 3:
            raise ValueError("glb_triangle_count_not_divisible_by_three")
        positions.append(point_count)
        triangles += index_count // 3
    if not positions:
        raise ValueError("glb_has_no_triangle_position_mesh")
    return {"format": "glb", "content_verified": True, "mesh_primitives": len(positions),
            "position_accessor_vertices": sum(positions), "triangles": triangles,
            "metric_dimensions_verified": False}


def _inspect_local(path_value: Any, *, input_root: Path, max_file_bytes: int) -> dict[str, Any]:
    relative = _text(path_value, "local_path", limit=1024)
    if relative is None:
        return {"format": "remote_reference_only", "content_verified": False}
    path = (input_root / relative).resolve()
    if not path.is_relative_to(input_root.resolve()) or not path.is_file():
        raise ValueError("local_path_missing_or_outside_input_root")
    size = path.stat().st_size
    if size == 0 or size > max_file_bytes:
        raise ValueError("local_artifact_exceeds_bounded_file_budget")
    with path.open("rb") as stream:
        header = stream.read(min(size, MAX_HEADER_BYTES))
    result: dict[str, Any] = {"local_path": relative, "sha256": _sha256_file(path), "bytes": size,
                              "content_verified": False}
    if header.startswith(b"glTF"):
        result.update(_glb_summary(path, header, size))
    elif (image_size := _png_size(header)) is not None:
        result.update({"format": "png", "content_verified": True, "image_pixels": list(image_size)})
    elif (image_size := _jpeg_size(header)) is not None:
        result.update({"format": "jpeg", "content_verified": True, "image_pixels": list(image_size)})
    elif header.startswith(b"%PDF-"):
        result.update({"format": "pdf", "content_verified": True})
    elif header.startswith(b"ISO-10303-21;"):
        result.update({"format": "ifc", "content_verified": True, "metric_dimensions_verified": False})
    else:
        result["format"] = "unknown"
    return result


def _semantic_kind(claim: str, caption: str | None, detected_format: str) -> tuple[str, list[str]]:
    text = caption or ""
    cues = {"elevation_plan" if _ELEVATION.search(text) else None,
            "panorama" if _PANORAMA.search(text) else None,
            "facade_image" if _FACADE.search(text) else None}
    cues.discard(None)
    reasons: list[str] = []
    if detected_format == "glb":
        if claim != "mesh":
            reasons.append("artifact_claim_format_conflict")
        return "mesh", reasons
    if detected_format == "ifc":
        reasons.append("bim_source_not_renderable_mesh")
        return "bim_source", reasons
    if claim == "mesh":
        reasons.append("mesh_file_not_verified")
        return "unknown", reasons
    if detected_format == "pdf" and claim == "site_plan" and _SITE_PLAN.search(text):
        return "site_plan", reasons
    if detected_format == "pdf" and claim == "design_report" and _DESIGN_REPORT.search(text):
        return "design_report", reasons
    if "elevation_plan" in cues:
        return "elevation_plan", reasons
    if "panorama" in cues:
        return "panorama", reasons
    if "facade_image" in cues and "elevation_plan" not in cues and detected_format != "pdf":
        return "facade_image", reasons
    if "facade_image" in cues and detected_format == "pdf":
        reasons.append("pdf_visual_not_direct_image")
    if claim != "unknown":
        reasons.append("visual_role_not_confirmed_by_caption")
    return "unknown", reasons


def assess_candidate(record: Mapping[str, Any], *, input_root: Path,
                     max_file_bytes: int = MAX_FILE_BYTES) -> dict[str, Any]:
    """Assess one private source record; never create a public URL or GLB."""
    candidate_id = _text(record.get("candidate_id"), "candidate_id", limit=80)
    if candidate_id is None or not _ID.fullmatch(candidate_id):
        raise ValueError("candidate_id must be a stable lowercase opaque identifier")
    claim = record.get("artifact_kind_claim", "unknown")
    if claim not in KINDS:
        raise ValueError("artifact_kind_claim is unsupported")
    source_url = _url(record.get("source_url"), "source_url")
    if source_url is None:
        raise ValueError("source_url is required")
    observed_at = _timestamp(record.get("observed_at"), "observed_at")
    updated_at = _timestamp(record.get("source_updated_at"), "source_updated_at", required=False)
    artifact_url = _url(record.get("artifact_url"), "artifact_url")
    caption = _text(record.get("caption"), "caption")
    identity = record.get("identity") or {}
    rights = record.get("rights") or {}
    geometry = record.get("geometry") or {}
    privacy = record.get("privacy") or {}
    if not all(isinstance(item, Mapping) for item in (identity, rights, geometry, privacy)):
        raise ValueError("identity, rights, geometry and privacy must be objects")
    reasons: list[str] = []
    try:
        inspection = _inspect_local(record.get("local_path"), input_root=input_root,
                                    max_file_bytes=max_file_bytes)
    except (ValueError, TypeError, AttributeError, IndexError, KeyError, OverflowError,
            json.JSONDecodeError, struct.error) as exc:
        inspection = {"format": "invalid", "content_verified": False, "error": str(exc)}
        reasons.append("local_artifact_invalid_or_unsafe")
    expected_hash = _text(record.get("expected_sha256"), "expected_sha256", limit=64)
    if expected_hash is not None:
        if not re.fullmatch(r"[a-f0-9]{64}", expected_hash):
            raise ValueError("expected_sha256 must be lowercase SHA-256")
        if inspection.get("sha256") != expected_hash:
            reasons.append("observed_hash_mismatch")
    expected_bytes = record.get("expected_bytes")
    if expected_bytes is not None:
        if not isinstance(expected_bytes, int) or isinstance(expected_bytes, bool) or expected_bytes <= 0:
            raise ValueError("expected_bytes must be a positive integer")
        if inspection.get("bytes") != expected_bytes:
            reasons.append("observed_size_mismatch")
    kind, kind_reasons = _semantic_kind(claim, caption, inspection["format"])
    reasons.extend(kind_reasons)
    if kind in {"facade_image", "elevation_plan", "panorama"} and inspection["format"] not in {
        "remote_reference_only", "png", "jpeg", "pdf"
    }:
        reasons.append("visual_format_not_verified")
    if kind == "mesh" and not inspection["content_verified"]:
        reasons.append("mesh_file_not_verified")
    if not inspection["content_verified"]:
        reasons.append("artifact_bytes_not_verified")

    match_level = identity.get("match_level", "unlinked")
    match_basis = identity.get("match_basis")
    match_urls = identity.get("evidence_urls") or []
    if not isinstance(match_urls, list) or len(match_urls) > 8:
        raise ValueError("identity.evidence_urls must be a list of at most 8 URLs")
    identity_evidence = [_url(value, "identity.evidence_url") for value in match_urls]
    corpus_claims_complete = (match_level == "corpus" and match_basis in {
        "official_corpus_id", "official_corpus_explanation", "exact_corpus_document"
    } and bool(identity_evidence) and bool(identity.get("corpus_label"))
        and bool(identity.get("design_revision")))
    # URLs and labels supplied by a lead are evidence claims, not an independent
    # check that the document names the same corpus and design revision.
    reasons.append("corpus_crosscheck_pending" if corpus_claims_complete
                   else "corpus_identity_or_revision_unverified")

    rights_values = {field: rights.get(field, "unknown") for field in ("storage", "display", "derivatives")}
    if any(value not in RIGHTS_VALUES for value in rights_values.values()):
        raise ValueError("rights fields must be granted, denied, or unknown")
    rights_evidence = _url(rights.get("evidence_url"), "rights.evidence_url")
    expiry = _timestamp(rights.get("expires_at"), "rights.expires_at", required=False)
    rights_claims_complete = (all(value == "granted" for value in rights_values.values())
                              and rights_evidence is not None and (expiry is None or expiry > observed_at))
    reasons.append("publication_rights_crosscheck_pending" if rights_claims_complete
                   else "publication_rights_unconfirmed")
    if any(value == "denied" for value in rights_values.values()):
        reasons.append("explicit_rights_denial")

    privacy_status = privacy.get("external_shell_only", "unknown")
    if privacy_status not in {True, False, "unknown"}:
        raise ValueError("privacy.external_shell_only must be true, false, or unknown")
    if kind in {"mesh", "bim_source"} and privacy_status is not True:
        reasons.append("interior_or_personal_content_unchecked")
    if privacy.get("contains_personal_data") is True:
        reasons.append("personal_data_present")

    units = geometry.get("units")
    crs = geometry.get("crs")
    datum = geometry.get("vertical_datum")
    dimensions = geometry.get("dimensions_m")
    controls = geometry.get("control_lengths_m")
    metric_claims_complete = (kind == "mesh" and inspection["content_verified"] and units == "m"
                              and isinstance(crs, str) and bool(crs.strip())
                              and isinstance(datum, str) and bool(datum.strip())
                              and isinstance(dimensions, list) and len(dimensions) == 3
                              and all(isinstance(value, (int, float)) and not isinstance(value, bool)
                                      and math.isfinite(value) and 0 < value < 2000 for value in dimensions)
                              and isinstance(controls, list) and len(controls) >= 2
                              and all(isinstance(value, (int, float)) and not isinstance(value, bool)
                                      and math.isfinite(value) and value > 0 for value in controls))
    # Claimed dimensions and control lengths are not independent measurements.
    # The GLB header scan does not establish its CRS, position, or dimensions.
    metric_verified = False
    if kind == "mesh":
        reasons.append("metric_geometry_not_verified")
    if kind == "elevation_plan" and not (geometry.get("scale_x") and geometry.get("scale_y") and controls):
        reasons.append("elevation_scale_not_calibrated")
    if kind in {"facade_image", "panorama"}:
        reasons.append("image_not_metric_mesh")

    visual_review = record.get("visual_review") or {}
    if not isinstance(visual_review, Mapping):
        raise ValueError("visual_review must be an object")
    visual_kind = "unknown"
    if visual_review:
        reviewed_kind = visual_review.get("kind")
        if reviewed_kind not in {"render", "photo", "unknown"}:
            raise ValueError("visual_review.kind must be render, photo, or unknown")
        reviewed_hash = _text(visual_review.get("artifact_sha256"), "visual_review.artifact_sha256", limit=64)
        if (reviewed_hash == inspection.get("sha256") and inspection["content_verified"]
                and inspection["format"] in {"png", "jpeg"}):
            visual_kind = reviewed_kind
        else:
            reasons.append("visual_review_unbound_to_artifact")
    if kind == "unknown" and visual_kind == "photo":
        kind = "context_photo"
        reasons.append("context_photo_not_corpus_facade")

    assertions_input = record.get("source_assertions") or []
    if not isinstance(assertions_input, list) or len(assertions_input) > 8:
        raise ValueError("source_assertions must be a list of at most 8 items")
    source_assertions: list[dict[str, Any]] = []
    for assertion in assertions_input:
        if not isinstance(assertion, Mapping):
            raise ValueError("source_assertions items must be objects")
        page = assertion.get("source_page")
        if not isinstance(page, int) or isinstance(page, bool) or not 1 <= page <= 5000:
            raise ValueError("source_assertions.source_page must be a PDF page number")
        value = assertion.get("value")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if not math.isfinite(value):
                raise ValueError("source_assertions.value must be finite")
        elif isinstance(value, str):
            value = _text(value, "source_assertions.value", limit=128)
        else:
            raise ValueError("source_assertions.value must be finite number or text")
        source_assertions.append({"type": _text(assertion.get("type"), "source_assertions.type", limit=64),
                                  "value": value, "unit": _text(assertion.get("unit"), "source_assertions.unit", limit=64),
                                  "subject": _text(assertion.get("subject"), "source_assertions.subject", limit=128),
                                  "source_page": page, "status": "source_assertion_not_mesh_measurement"})

    reasons = sorted(set(reasons))
    # No supplied URL or rights flag upgrades itself into an independently
    # verified corpus link or public release permission.
    status = ("rejected" if "local_artifact_invalid_or_unsafe" in reasons or
              "observed_hash_mismatch" in reasons or "observed_size_mismatch" in reasons or
              "explicit_rights_denial" in reasons or "personal_data_present" in reasons
              else "review")
    return {
        "schema_version": "facade-assessment/1", "rules_version": RULES_VERSION,
        "candidate_id": candidate_id, "status": status, "reason_codes": reasons,
        "artifact_kind_claim": claim, "artifact_kind": kind,
        "artifact": {**inspection, "url": artifact_url, "caption": caption,
                     "visual_kind": visual_kind,
                     "visual_review": {"method": "manual_local_visual_inspection" if visual_review else None,
                                       "artifact_sha256": visual_review.get("artifact_sha256"),
                                       "note": _text(visual_review.get("note"), "visual_review.note")}},
        "source": {"url": source_url, "publisher": _text(record.get("publisher"), "publisher"),
                   "observed_at": observed_at, "source_updated_at": updated_at,
                   "access_method": _text(record.get("access_method"), "access_method"),
                   "source_record_id": _text(record.get("source_record_id"), "source_record_id")},
        "identity": {"registry_id": _text(identity.get("registry_id"), "identity.registry_id"),
                     "project_name": _text(identity.get("project_name"), "identity.project_name"),
                     "corpus_label": _text(identity.get("corpus_label"), "identity.corpus_label"),
                     "cadastre": _text(identity.get("cadastre"), "identity.cadastre"),
                     "phase": _text(identity.get("phase"), "identity.phase"),
                     "design_revision": _text(identity.get("design_revision"), "identity.design_revision"),
                     "match_level": match_level, "match_basis": match_basis,
                     "evidence_urls": identity_evidence,
                     "corpus_claims_complete": corpus_claims_complete,
                     "corpus_verified": False},
        "rights": {**rights_values, "evidence_url": rights_evidence,
                   "attribution": _text(rights.get("attribution"), "rights.attribution"),
                   "expires_at": expiry,
                   "read_access_observed": inspection.get("sha256") is not None,
                   "publication_rights_claims_complete": rights_claims_complete,
                   "publication_rights_verified": False},
        "geometry": {"units": units, "crs": crs, "vertical_datum": datum,
                     "dimensions_m": dimensions, "control_lengths_m": controls,
                     "scale_x": geometry.get("scale_x"), "scale_y": geometry.get("scale_y"),
                     "metric_claims_complete": metric_claims_complete,
                     "metric_verified": metric_verified},
        "source_assertions": source_assertions,
        "privacy": {"external_shell_only": privacy_status,
                    "contains_personal_data": privacy.get("contains_personal_data", "unknown")},
        "public_model_url": None, "public_asset_created": False,
    }


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def assess_jsonl(input_path: Path, output_dir: Path, *, input_root: Path,
                 max_file_bytes: int = MAX_FILE_BYTES) -> dict[str, Any]:
    """Write an immutable, content-addressed private run after full validation."""
    if input_path.stat().st_size > MAX_CANDIDATES * MAX_LINE_BYTES:
        raise ValueError("input exceeds bounded JSONL file budget")
    output_dir.mkdir(parents=True, exist_ok=True)
    source_hash = _sha256_file(input_path)
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    with input_path.open("rb") as stream:
        for number, raw in enumerate(stream, start=1):
            if number > MAX_CANDIDATES or len(raw) > MAX_LINE_BYTES:
                raise ValueError("input exceeds bounded candidate or line budget")
            if not raw.strip():
                continue
            row = json.loads(raw)
            if not isinstance(row, Mapping):
                raise ValueError(f"input line {number} must be an object")
            result = assess_candidate(row, input_root=input_root, max_file_bytes=max_file_bytes)
            if result["candidate_id"] in seen:
                raise ValueError(f"duplicate candidate_id: {result['candidate_id']}")
            seen.add(result["candidate_id"])
            records.append(result)
    if not records:
        raise ValueError("no candidate records to assess")
    rows = b"".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
                    for row in sorted(records, key=lambda value: value["candidate_id"]))
    assessment_hash = hashlib.sha256(rows).hexdigest()
    run_id = f"facades-v1-{assessment_hash[:16]}"
    run_dir = output_dir / run_id
    manifest = {"schema_version": "facade-assessment/1", "rules_version": RULES_VERSION,
                "run_id": run_id, "input_sha256": source_hash,
                "assessments_sha256": assessment_hash, "records_total": len(records),
                "status_counts": dict(sorted(Counter(row["status"] for row in records).items())),
                "artifact_kind_counts": dict(sorted(Counter(row["artifact_kind"] for row in records).items())),
                "public_assets_created": 0, "active_pointer_changed": False}
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    if run_dir.exists():
        if ((run_dir / "assessments.jsonl").read_bytes() != rows or
                (run_dir / "manifest.json").read_bytes() != manifest_bytes):
            raise ValueError("content-addressed run path already exists with different bytes")
        return manifest
    with tempfile.TemporaryDirectory(dir=output_dir, prefix=f".{run_id}.") as temporary:
        staging = Path(temporary)
        _atomic_bytes(staging / "assessments.jsonl", rows)
        _atomic_bytes(staging / "manifest.json", manifest_bytes)
        staging.replace(run_dir)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--input-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--max-file-mib", type=int, default=32)
    args = parser.parse_args()
    if not 1 <= args.max_file_mib <= 64:
        parser.error("--max-file-mib must be between 1 and 64")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    result = assess_jsonl(args.input, args.output_dir, input_root=args.input_root,
                          max_file_bytes=args.max_file_mib * 1024 * 1024)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
