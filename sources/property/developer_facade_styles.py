"""Procedural, project-level facade style tiles for labeled reconstructions.

Only human-reviewed appearance tokens are rendered. This module does not read
or change building geometry and does not project source pixels onto a facade.
All RGB values below are illustrative palette swatches, not measured colors.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import shutil
import struct
import tempfile
import zlib
from typing import Any, Iterable, Mapping


OBSERVATION_SCHEMA = "developer-facade-style-observation/1"
MATERIAL_SCHEMA = "developer-facade-style-material/1"
RENDERER_ID = "procedural-project-facade-tile/1"
BATCH_SCHEMA = "developer-facade-style-batch/1"
RECONSTRUCTION_LABEL = "Реконструкция"
BASIS = "project_style_inference"
LOD_SIZES: tuple[tuple[str, int, int], ...] = (
    ("near", 256, 85),
    ("closer", 512, 171),
    ("close", 1024, 341),
)

# Named swatches intentionally communicate visual resemblance only.
_WALL: dict[str, tuple[int, int, int]] = {
    "charcoal": (73, 77, 79),
    "warm_gray": (157, 148, 137),
    "off_white": (220, 216, 203),
    "light_concrete": (190, 190, 183),
    "terracotta": (164, 91, 66),
    "rose_brick": (183, 143, 128),
    "brick_red": (145, 75, 62),
    "graphite": (57, 62, 65),
    "sandstone": (190, 170, 143),
    "medium_gray": (137, 143, 143),
    "pale_stone": (207, 202, 188),
    "unknown": (168, 169, 164),
}
_GLAZING: dict[str, tuple[int, int, int]] = {
    "blue_gray": (83, 113, 130),
    "dark_blue_gray": (57, 76, 87),
    "neutral_gray": (91, 101, 104),
    "reflective_silver": (151, 165, 167),
    "green_gray": (82, 108, 99),
    "dark": (48, 58, 62),
    "unknown": (76, 88, 91),
}
_FRAME: dict[str, tuple[int, int, int]] = {
    "dark_bronze": (91, 69, 52),
    "bronze": (131, 94, 62),
    "black": (39, 43, 44),
    "graphite": (67, 73, 74),
    "white": (219, 218, 207),
    "silver": (166, 171, 167),
    "warm_gray": (125, 117, 107),
    "unknown": (82, 86, 86),
}
_ACCENT: dict[str, tuple[int, int, int]] = {
    "rust": (153, 78, 57),
    "copper": (157, 101, 68),
    "ochre": (174, 132, 72),
    "ivory": (224, 218, 198),
    "white": (222, 222, 214),
    "graphite": (73, 78, 80),
    "green": (91, 113, 94),
    "blue": (80, 113, 138),
    "unknown": (165, 169, 165),
}
_WINDOW_RHYTHMS = frozenset({"regular_grid", "vertical_bays", "large_glazing", "mixed"})
_BALCONY_RHYTHMS = frozenset({"recessed", "projecting", "mixed", "not_observed"})
_BAND_RHYTHMS = frozenset({"strong", "subtle", "none", "not_observed"})
_VERTICAL_STRIPS = frozenset({"none", "sparse", "periodic", "not_observed"})
_PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class InsufficientStyleEvidence(ValueError):
    """The reviewed observation cannot safely drive a facade-style tile."""


@dataclass(frozen=True)
class FacadeStyleBundle:
    """Generated assets plus private source evidence for one exact project."""

    material_record: dict[str, Any]
    textures: dict[str, bytes]
    private_evidence: dict[str, Any]


@dataclass(frozen=True)
class FacadeStyleBatch:
    """Verified immutable batch contents, with private evidence kept separate."""

    manifest: dict[str, Any]
    material_rows: tuple[dict[str, Any], ...]
    evidence_rows: tuple[dict[str, Any], ...]
    textures: dict[str, bytes]


def _jsonl_bytes(rows: Iterable[Mapping[str, Any]]) -> bytes:
    return b"".join(_canonical_bytes(row) + b"\n" for row in rows)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"Expected a JSON object at {path.name}:{line_number}")
        rows.append(value)
    return rows


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _check_observation(observation: Mapping[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
    if observation.get("schema_version") != OBSERVATION_SCHEMA:
        raise InsufficientStyleEvidence("Unsupported observation schema")
    if observation.get("source_scope") != "project_gallery":
        raise InsufficientStyleEvidence("Only exact project-gallery observations are accepted")
    project_id = observation.get("publisher_project_id")
    if not isinstance(project_id, str) or not _PROJECT_ID.fullmatch(project_id):
        raise InsufficientStyleEvidence("Missing or unsafe exact publisher project ID")
    if not isinstance(observation.get("project_name"), str) or not observation["project_name"].strip():
        raise InsufficientStyleEvidence("Missing project identity label")

    sources = observation.get("sources")
    if not isinstance(sources, list) or not sources:
        raise InsufficientStyleEvidence("At least one reviewed source image is required")
    source_rows: list[dict[str, str]] = []
    for source in sources:
        if not isinstance(source, Mapping):
            raise InsufficientStyleEvidence("Malformed source evidence")
        page_url = source.get("page_url")
        page_sha = source.get("page_sha256")
        asset_url = source.get("asset_url")
        asset_sha = source.get("asset_sha256")
        role = source.get("role")
        observed_at = source.get("observed_at")
        source_updated_at = source.get("source_updated_at")
        access_method = source.get("access_method")
        rights = source.get("rights_status")
        if (not isinstance(page_url, str) or not page_url.startswith("https://")
                or not isinstance(page_sha, str) or not _SHA256.fullmatch(page_sha)
                or not isinstance(asset_url, str) or not asset_url.startswith("https://")
                or not isinstance(asset_sha, str) or not _SHA256.fullmatch(asset_sha)
                or role not in {"render", "facade_photo", "elevation", "unknown"}
                or not isinstance(observed_at, str) or not observed_at
                or not isinstance(access_method, str) or not access_method
                or not isinstance(rights, str) or not rights):
            raise InsufficientStyleEvidence("Source must have HTTPS identity, page/image hashes, access method, date, role, and rights status")
        try:
            parsed_at = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise InsufficientStyleEvidence("Source observation time must be ISO-8601") from error
        if parsed_at.tzinfo is None or parsed_at.utcoffset() is None:
            raise InsufficientStyleEvidence("Source observation time must include a timezone")
        if source_updated_at is not None:
            if not isinstance(source_updated_at, str):
                raise InsufficientStyleEvidence("Source update time must be ISO-8601 or null")
            try:
                parsed_updated_at = datetime.fromisoformat(source_updated_at.replace("Z", "+00:00"))
            except ValueError as error:
                raise InsufficientStyleEvidence("Source update time must be ISO-8601 or null") from error
            if parsed_updated_at.tzinfo is None or parsed_updated_at.utcoffset() is None:
                raise InsufficientStyleEvidence("Source update time must include a timezone")
        source_rows.append({
            "page_url": page_url,
            "page_sha256": page_sha,
            "source_updated_at": source_updated_at,
            "asset_url": asset_url,
            "asset_sha256": asset_sha,
            "role": role,
            "access_method": access_method,
            "observed_at": observed_at,
            "rights_status": rights,
        })

    visual = observation.get("visual_review")
    if not isinstance(visual, Mapping):
        raise InsufficientStyleEvidence("Missing visual review")
    if visual.get("confidence") not in {"medium", "high"}:
        raise InsufficientStyleEvidence("Overall visual confidence must be medium or high")
    visual_palette = visual.get("palette")
    visual_rhythm = visual.get("rhythm")
    if not isinstance(visual_palette, Mapping) or not isinstance(visual_rhythm, Mapping):
        raise InsufficientStyleEvidence("Visual palette/rhythm review is malformed")
    tokens = observation.get("generator_tokens")
    if not isinstance(tokens, Mapping):
        raise InsufficientStyleEvidence("Missing normalized generator tokens")
    palette = tokens.get("palette")
    rhythm = tokens.get("rhythm")
    if not isinstance(palette, Mapping) or not isinstance(rhythm, Mapping):
        raise InsufficientStyleEvidence("Malformed normalized palette or rhythm")
    wall, glazing, frames = palette.get("wall"), palette.get("glazing"), palette.get("frames")
    if wall not in _WALL:
        raise InsufficientStyleEvidence("Wall appearance must use a known reviewed color token")
    if glazing not in _GLAZING:
        raise InsufficientStyleEvidence("Glazing appearance must use a known reviewed color token")
    if frames not in _FRAME:
        raise InsufficientStyleEvidence("Frame appearance must use a known reviewed color token")
    accents = palette.get("accents", [])
    if (not isinstance(accents, list) or len(accents) > 4
            or any(value not in _ACCENT for value in accents)):
        raise InsufficientStyleEvidence("Accent tokens must be a bounded list of at most four known visual swatches")
    if visual_palette.get("confidence") not in {"medium", "high"}:
        raise InsufficientStyleEvidence("Palette confidence must be medium or high")
    windows = rhythm.get("windows")
    balconies = rhythm.get("balconies")
    bands = rhythm.get("horizontal_bands")
    vertical_strips = rhythm.get("vertical_strips")
    if windows not in _WINDOW_RHYTHMS:
        raise InsufficientStyleEvidence("A reviewed window rhythm is required")
    if (balconies not in _BALCONY_RHYTHMS or bands not in _BAND_RHYTHMS
            or vertical_strips not in _VERTICAL_STRIPS):
        raise InsufficientStyleEvidence("Unsupported balcony or band rhythm")
    if visual_rhythm.get("confidence") not in {"medium", "high"}:
        raise InsufficientStyleEvidence("Rhythm confidence must be medium or high")

    normalized = {
        "palette": {"wall": wall, "glazing": glazing, "frames": frames,
                    "accents": list(accents)},
        "rhythm": {"windows": windows, "balconies": balconies,
                   "horizontal_bands": bands, "vertical_strips": vertical_strips},
    }
    private_evidence = {
        "schema_version": "developer-facade-style-private-evidence/1",
        "provider": observation.get("provider"),
        "publisher_project_id": project_id,
        "project_name": observation["project_name"],
        "source_scope": "project_gallery",
        "sources": source_rows,
        "visual_review": dict(visual),
        "generator_tokens": normalized,
        "checked_at": observation.get("checked_at"),
        "rights_status": "unknown_or_source_asserted",
        "source_evidence_sha256": hashlib.sha256(_canonical_bytes(observation)).hexdigest(),
    }
    checked_at = observation.get("checked_at")
    try:
        parsed_checked_at = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise InsufficientStyleEvidence("Visual review time must be ISO-8601") from error
    if parsed_checked_at.tzinfo is None or parsed_checked_at.utcoffset() is None:
        raise InsufficientStyleEvidence("Visual review time must include a timezone")
    return project_id, normalized, private_evidence


def _blend(color: tuple[int, int, int], toward: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    return tuple(round(a * (1.0 - amount) + b * amount) for a, b in zip(color, toward))


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))


def _paint_rect(rows: list[bytearray], x0: int, y0: int, x1: int, y1: int,
                color: tuple[int, int, int]) -> None:
    height = len(rows)
    width = len(rows[0]) // 3
    x0, x1 = max(0, min(width, x0)), max(0, min(width, x1))
    y0, y1 = max(0, min(height, y0)), max(0, min(height, y1))
    if x1 <= x0 or y1 <= y0:
        return
    span = bytes(color) * (x1 - x0)
    for y in range(y0, y1):
        start = x0 * 3
        rows[y][start:start + len(span)] = span


def _render_png(width: int, height: int, tokens: Mapping[str, Any]) -> bytes:
    palette = tokens["palette"]
    rhythm = tokens["rhythm"]
    wall = _WALL[palette["wall"]]
    glass = _GLAZING[palette["glazing"]]
    frame = _FRAME[palette["frames"]]
    visible_accents = [value for value in palette["accents"] if value != "unknown"]
    accent = _ACCENT[visible_accents[0]] if visible_accents else _blend(wall, (255, 255, 255), 0.23)
    shadow = _blend(wall, (0, 0, 0), 0.22)
    reflection = _blend(glass, (218, 229, 228), 0.22)
    rows = [bytearray(bytes(wall) * width) for _ in range(height)]

    # Repeated 9 m x 3 m schematic tile. Top and bottom borders match so the
    # texture repeats without claiming a surveyed scale or facade orientation.
    band = rhythm["horizontal_bands"]
    if band != "none" and band != "not_observed":
        thickness = max(1, round(height * (0.038 if band == "strong" else 0.018)))
        band_color = accent if band == "strong" else _blend(wall, accent, 0.35)
        _paint_rect(rows, 0, 0, width, thickness, band_color)
        _paint_rect(rows, 0, height - thickness, width, height, band_color)

    vertical_strips = rhythm["vertical_strips"]
    if vertical_strips not in {"none", "not_observed"}:
        strip_width = max(1, round(width * (0.018 if vertical_strips == "sparse" else 0.028)))
        strip_color = _ACCENT[visible_accents[0]] if visible_accents else _blend(wall, (255, 255, 255), 0.55)
        positions = ([round(width / 2)] if vertical_strips == "sparse"
                     else [0, round(width / 3), round(2 * width / 3)])
        for x in positions:
            _paint_rect(rows, x - strip_width // 2, 0,
                        x + (strip_width + 1) // 2, height, strip_color)

    cell = width / 3.0
    window_mode = rhythm["windows"]
    for index in range(3):
        if window_mode == "regular_grid":
            relative_width, top, bottom = 0.42, 0.22, 0.78
        elif window_mode == "vertical_bays":
            relative_width, top, bottom = 0.48, 0.12, 0.88
        elif window_mode == "large_glazing":
            relative_width, top, bottom = 0.68, 0.16, 0.84
        else:  # mixed, a visibly varied but bounded project-style rhythm
            relative_width = (0.40, 0.60, 0.46)[index]
            top, bottom = ((0.20, 0.76), (0.14, 0.84), (0.23, 0.78))[index]
        center = (index + 0.5) * cell
        window_width = max(3, round(cell * relative_width))
        outer_x0 = round(center - window_width / 2)
        outer_x1 = outer_x0 + window_width
        y0, y1 = round(height * top), round(height * bottom)
        frame_px = max(1, round(width / 256))
        _paint_rect(rows, outer_x0 - frame_px, y0 - frame_px,
                    outer_x1 + frame_px, y1 + frame_px, frame)
        _paint_rect(rows, outer_x0, y0, outer_x1, y1, glass)
        mullion_x = round((outer_x0 + outer_x1) / 2)
        _paint_rect(rows, mullion_x, y0, mullion_x + frame_px, y1, frame)
        reflection_x = outer_x0 + max(1, frame_px * 2)
        _paint_rect(rows, reflection_x, y0 + max(1, frame_px * 2),
                    reflection_x + max(1, frame_px), y1 - max(1, frame_px * 2), reflection)

        balcony_mode = rhythm["balconies"]
        has_balcony = (balcony_mode == "projecting"
                       or (balcony_mode == "mixed" and index in (0, 2)))
        if has_balcony:
            rail_y = min(height - 2, y1 + max(2, round(height * 0.06)))
            rail_x0 = max(0, outer_x0 - round(cell * 0.12))
            rail_x1 = min(width, outer_x1 + round(cell * 0.12))
            rail = max(1, frame_px)
            _paint_rect(rows, rail_x0, rail_y, rail_x1, rail_y + rail, frame)
            for post_x in range(rail_x0, rail_x1, max(2, round(cell * 0.18))):
                _paint_rect(rows, post_x, rail_y - max(1, round(height * 0.05)),
                            post_x + rail, rail_y, frame)
        elif balcony_mode == "recessed":
            # A shallow dark reveal is only a 2D appearance cue, not a balcony
            # volume or claim about the underlying building geometry.
            _paint_rect(rows, outer_x0 - frame_px, y1 + frame_px,
                        outer_x1 + frame_px, y1 + max(2, round(height * 0.045)), shadow)

    raw = b"".join(b"\x00" + row for row in rows)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"IDAT", zlib.compress(raw, level=9))
            + _png_chunk(b"IEND", b""))


def build_project_facade_style(observation: Mapping[str, Any]) -> FacadeStyleBundle:
    """Build hashed 2D material tiles from one reviewed exact project source.

    This function deliberately accepts no building geometry, corpus ID,
    height, floor count, orientation, or facade-face mapping. Source image
    URLs and source hashes are returned only in ``private_evidence``.
    """
    project_id, tokens, private_evidence = _check_observation(observation)
    style_basis = {
        "renderer": RENDERER_ID,
        "publisher_project_id": project_id,
        "tokens": tokens,
        "source_hashes": sorted(source["asset_sha256"]
                                 for source in private_evidence["sources"]),
    }
    style_digest = hashlib.sha256(_canonical_bytes(style_basis)).hexdigest()
    style_id = "style_" + style_digest[:24]
    textures: dict[str, bytes] = {}
    lods: list[dict[str, Any]] = []
    for level, width, height in LOD_SIZES:
        png = _render_png(width, height, tokens)
        asset_sha = hashlib.sha256(png).hexdigest()
        relative_path = f"textures/{asset_sha}.png"
        textures[relative_path] = png
        lods.append({
            "level": level,
            "width": width,
            "height": height,
            "mime_type": "image/png",
            "sha256": asset_sha,
            "asset_uri": f"facade-style://sha256/{asset_sha}",
            "asset_path": relative_path,
        })
    record = {
        "schema_version": MATERIAL_SCHEMA,
        "renderer": RENDERER_ID,
        "style_id": style_id,
        "publisher_project_id": project_id,
        "basis": BASIS,
        "label": RECONSTRUCTION_LABEL,
        "geometry_scope": "appearance_only_no_geometry_change",
        "rights_status": "unknown",
        "lods": lods,
    }
    private_evidence["style_id"] = style_id
    private_evidence["material_record_sha256"] = hashlib.sha256(_canonical_bytes(record)).hexdigest()
    private_evidence["renderer"] = RENDERER_ID
    private_evidence["source_observation"] = dict(observation)
    return FacadeStyleBundle(record, textures, private_evidence)


def write_frozen_style_batch(observations: Iterable[Mapping[str, Any]],
                             output_dir: Path) -> FacadeStyleBatch:
    """Write a new immutable source-private batch and immediately verify it.

    The destination must not exist. Revisions get a new directory so that
    already-pinned material batches cannot be silently changed.
    """
    ordered = sorted(list(observations), key=lambda row: str(row.get("publisher_project_id", "")))
    if not ordered:
        raise ValueError("A facade batch must contain at least one reviewed project")
    project_ids = [row.get("publisher_project_id") for row in ordered]
    if len(project_ids) != len(set(project_ids)):
        raise ValueError("Facade batch contains duplicate project IDs")
    bundles = [build_project_facade_style(row) for row in ordered]

    material_rows: list[dict[str, Any]] = []
    evidence_rows: list[dict[str, Any]] = []
    textures: dict[str, bytes] = {}
    for bundle in bundles:
        private = bundle.private_evidence
        material = dict(bundle.material_record)
        material["source_evidence_sha256"] = private["source_evidence_sha256"]
        material_rows.append(material)
        evidence_rows.append({
            "source_evidence_sha256": private["source_evidence_sha256"],
            "publisher_project_id": private["publisher_project_id"],
            "style_id": private["style_id"],
            "source_observation": private["source_observation"],
        })
        for relative_path, data in bundle.textures.items():
            previous = textures.setdefault(relative_path, data)
            if previous != data:
                raise ValueError("Content-addressed texture collision")

    source_observations_sha256 = hashlib.sha256(_jsonl_bytes(ordered)).hexdigest()
    materials_bytes = _jsonl_bytes(material_rows)
    evidence_bytes = _jsonl_bytes(evidence_rows)
    file_bytes = {"materials.jsonl": materials_bytes,
                  "style-evidence.jsonl": evidence_bytes, **textures}
    file_hashes = {name: hashlib.sha256(data).hexdigest()
                   for name, data in sorted(file_bytes.items())}
    manifest = {
        "schema_version": BATCH_SCHEMA,
        "renderer": RENDERER_ID,
        "materials_file": "materials.jsonl",
        "evidence_file": "style-evidence.jsonl",
        "styles_count": len(material_rows),
        "textures_count": len(textures),
        "source_observations_sha256": source_observations_sha256,
        "files": file_hashes,
    }

    output_dir = Path(output_dir)
    if output_dir.exists() or output_dir.is_symlink():
        raise FileExistsError(f"Immutable style batch destination already exists: {output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=output_dir.parent))
    try:
        for relative_path, data in file_bytes.items():
            target = temp_dir / PurePosixPath(relative_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        manifest_bytes = _canonical_bytes(manifest) + b"\n"
        with (temp_dir / "manifest.json").open("xb") as stream:
            stream.write(manifest_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        directory_fd = os.open(temp_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        os.replace(temp_dir, output_dir)
        parent_fd = os.open(output_dir.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    return load_frozen_style_batch(output_dir)


def load_frozen_style_batch(batch_dir: Path) -> FacadeStyleBatch:
    """Load and verify a batch directory or its ``manifest.json`` path.

    ``FacadeStyleBatch.material_rows`` is the source-private project-keyed
    index (including ``source_evidence_sha256``); ``textures`` maps
    batch-root-relative content-addressed paths to verified PNG bytes.
    """
    batch_dir = Path(batch_dir)
    if batch_dir.name == "manifest.json":
        batch_dir = batch_dir.parent
    manifest_path = batch_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != BATCH_SCHEMA or manifest.get("renderer") != RENDERER_ID:
        raise ValueError("Unsupported facade style batch manifest")
    if (manifest.get("materials_file") != "materials.jsonl"
            or manifest.get("evidence_file") != "style-evidence.jsonl"):
        raise ValueError("Unexpected facade style batch file layout")
    files = manifest.get("files")
    if (not isinstance(files, dict)
            or not {"materials.jsonl", "style-evidence.jsonl"} <= set(files)):
        raise ValueError("Incomplete batch file inventory")
    file_data: dict[str, bytes] = {}
    for relative, expected_hash in files.items():
        if not isinstance(relative, str) or PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts:
            raise ValueError("Unsafe path in facade material manifest")
        if not isinstance(expected_hash, str) or not _SHA256.fullmatch(expected_hash):
            raise ValueError("Malformed file digest in facade material manifest")
        path = batch_dir / PurePosixPath(relative)
        if (path.is_symlink() or not path.is_file()
                or not path.resolve().is_relative_to(batch_dir.resolve())):
            raise ValueError(f"Missing or linked batch file: {relative}")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected_hash:
            raise ValueError(f"Batch file digest mismatch: {relative}")
        file_data[relative] = data

    material_rows = _read_jsonl(batch_dir / "materials.jsonl")
    evidence_rows = _read_jsonl(batch_dir / "style-evidence.jsonl")
    if (len(material_rows) != manifest.get("styles_count")
            or len(evidence_rows) != manifest.get("styles_count")):
        raise ValueError("Batch record counts do not match manifest")
    if len({row.get("publisher_project_id") for row in material_rows}) != len(material_rows):
        raise ValueError("Duplicate project style row")
    evidence_by_sha: dict[str, dict[str, Any]] = {}
    for row in evidence_rows:
        digest = row.get("source_evidence_sha256")
        observation = row.get("source_observation")
        if (not isinstance(digest, str) or not _SHA256.fullmatch(digest)
                or not isinstance(observation, dict)
                or hashlib.sha256(_canonical_bytes(observation)).hexdigest() != digest
                or digest in evidence_by_sha):
            raise ValueError("Private source evidence hash is invalid or duplicated")
        if row.get("publisher_project_id") != observation.get("publisher_project_id"):
            raise ValueError("Evidence row project ID mismatch")
        evidence_by_sha[digest] = row

    expected_texture_paths: set[str] = set()
    textures: dict[str, bytes] = {}
    source_rows_for_hash: list[dict[str, Any]] = []
    for material in material_rows:
        evidence_sha = material.get("source_evidence_sha256")
        evidence = evidence_by_sha.get(evidence_sha)
        if evidence is None or evidence.get("publisher_project_id") != material.get("publisher_project_id"):
            raise ValueError("Material is not linked to matching exact-project evidence")
        if evidence.get("style_id") != material.get("style_id"):
            raise ValueError("Material/evidence style ID mismatch")
        observation = evidence["source_observation"]
        source_rows_for_hash.append(observation)
        bundle = build_project_facade_style(observation)
        expected_record = dict(bundle.material_record)
        expected_record["source_evidence_sha256"] = evidence_sha
        if material != expected_record:
            raise ValueError("Material record does not match reviewed source tokens")
        validate_material_record(material)
        for item in material["lods"]:
            relative = item["asset_path"]
            expected_texture_paths.add(relative)
            png = file_data.get(relative)
            if png is None or hashlib.sha256(png).hexdigest() != item["sha256"]:
                raise ValueError("Material texture is missing or has an invalid digest")
            expected_png = bundle.textures[relative]
            if png != expected_png:
                raise ValueError("Texture pixels do not match the pinned project style")
            textures[relative] = png
    if set(file_data) != {"materials.jsonl", "style-evidence.jsonl", *expected_texture_paths}:
        raise ValueError("Manifest file inventory and material assets disagree")
    source_rows_for_hash.sort(key=lambda row: row["publisher_project_id"])
    if hashlib.sha256(_jsonl_bytes(source_rows_for_hash)).hexdigest() != manifest.get(
            "source_observations_sha256"):
        raise ValueError("Source observations digest does not match manifest")
    if len(textures) != manifest.get("textures_count"):
        raise ValueError("Texture count does not match manifest")
    return FacadeStyleBatch(manifest, tuple(material_rows), tuple(evidence_rows), textures)


def validate_material_record(record: Mapping[str, Any]) -> None:
    """Fail closed on unsupported public descriptors before overlay assembly."""
    if record.get("schema_version") != MATERIAL_SCHEMA or record.get("renderer") != RENDERER_ID:
        raise ValueError("Unsupported facade material record")
    if record.get("basis") != BASIS or record.get("label") != RECONSTRUCTION_LABEL:
        raise ValueError("Facade material must carry its reconstruction basis and label")
    if record.get("geometry_scope") != "appearance_only_no_geometry_change":
        raise ValueError("Facade style cannot modify model geometry")
    project_id = record.get("publisher_project_id")
    if not isinstance(project_id, str) or not _PROJECT_ID.fullmatch(project_id):
        raise ValueError("Unsafe project join key")
    lods = record.get("lods")
    if not isinstance(lods, list) or [item.get("level") for item in lods] != [
            "near", "closer", "close"]:
        raise ValueError("Expected the three bounded near texture tiers")
    for item, (level, width, height) in zip(lods, LOD_SIZES, strict=True):
        if (item.get("level"), item.get("width"), item.get("height")) != (level, width, height):
            raise ValueError("Unexpected facade texture tier dimensions")
        if item.get("mime_type") != "image/png" or not _SHA256.fullmatch(str(item.get("sha256"))):
            raise ValueError("Invalid texture asset descriptor")
        if item.get("asset_uri") != f"facade-style://sha256/{item['sha256']}":
            raise ValueError("Texture URI must be content-addressed")
        expected = f"textures/{item['sha256']}.png"
        if item.get("asset_path") != expected:
            raise ValueError("Texture path must be content-addressed")
