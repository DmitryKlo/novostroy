"""Validated, offer-free model-candidate run and public review projection."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from backend.registry import load_registry
from backend.generation import current_generation
from backend.store import ASSET_ROOT, ROOT, asset_manifest


MODELS_ROOT = ROOT / "data/models"
ACTIVE_PATH = MODELS_ROOT / "active.json"
SCHEMA_VERSION = "model-candidates/1"
CANDIDATE_ID_RE = re.compile(r"cand_[0-9a-f]{24}\Z")
REGISTRY_ID_RE = re.compile(r"registry-[0-9a-f]{24}\Z")
PUBLIC_ROOT_RE = re.compile(r"public-project-models-v1-[0-9a-f]{16}\Z")
MODEL_FILE_RE = re.compile(r"model_[0-9a-f]{64}(?:\.tileset\.json|\.glb)\Z")
HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
RUN_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{1,79}\Z")
DECISIONS = frozenset({"ready", "review", "rejected", "unavailable"})
ASSET_READINESS = frozenset({"ready_to_render", "review", "unavailable"})
LINK_STATUSES = frozenset({"verified", "review", "unlinked"})
ENTITY_GROUPS = {
    "registry_project": "registry",
    "registry_review_record": "registry",
    "mapped_construction": "mapped_construction",
    "external_event": "external_event",
}
MODEL_KINDS = frozenset({"mapped_construction", "project_model"})
DEPICTIONS = frozenset({"construction_outline", "project_footprint",
                       "schematic_massing", "licensed_exterior", "none"})
MAX_ASSESSMENTS_BYTES = 50_000_000
MAX_PUBLIC_MANIFEST_BYTES = 5_000_000
MAX_PUBLIC_FILE_BYTES = 50_000_000
MAX_PREVIEW_VERTICES = 128
MAX_PREVIEW_COORDINATES = 512

# Safe public explanation and next step are generated here, never copied from
# source text or from an unreviewed pipeline assertion.
REASONS: dict[str, tuple[str, str]] = {
    "no_corpus_link": ("Не подтверждена связь геометрии с конкретным корпусом.",
                       "Нужен план или ведомость с номером корпуса и его контуром."),
    "no_measured_geometry": ("Для объекта нет подтверждённых размеров индивидуальной модели.",
                             "Нужен масштабный план или проверенная геометрия объекта."),
    "unverified_location": ("Местоположение объекта недостаточно проверено для размещения модели.",
                            "Нужна точная геопривязка, не только адресная точка."),
    "outdated_version": ("Доступная проектная версия может быть устаревшей.",
                         "Нужна датированная действующая редакция проекта."),
    "rights_unconfirmed": ("Не подтверждены права на публичный показ этой модели.",
                           "Нужно разрешение на хранение, переработку и показ модели."),
    "crs_or_datum_unknown": ("Не подтверждены система координат или высотная основа.",
                             "Нужны CRS, вертикальный датум и контрольные точки."),
    "conflicting_evidence": ("Источники противоречат друг другу; оценка требует проверки.",
                             "Нужно сверить документы, даты и идентичность корпуса."),
    "not_moscow_residential": ("Запись не подтверждает московскую жилую новостройку.",
                               "Проверьте регион и назначение объекта."),
    "non_building_infrastructure": ("Запись относится к инфраструктуре, а не к зданию.",
                                    "Нужен источник о конкретном жилом корпусе."),
    "source_unavailable": ("Индивидуальный источник модели или геометрии недоступен.",
                           "Нужен доступный подтверждённый источник."),
    "no_project_link": ("Контур не связан с подтверждённым проектом или ЖК.",
                        "Нужна независимая привязка к проектному документу."),
    "not_currently_verified": ("Текущая стадия строительства не подтверждена.",
                               "Нужен новый датированный факт о состоянии объекта."),
    "invalid_geometry": ("Геометрия не прошла проверку формы или координат.",
                         "Нужен исправленный контур с проверяемой геопривязкой."),
    "ambiguous_match": ("Есть несколько возможных соответствий; выбор корпуса не доказан.",
                        "Нужен уникальный документный ключ или ручная сверка."),
    "mapped_status_not_current_verified": ("Статус строительства на карте не подтверждает текущее состояние.",
                                           "Нужна свежая проверка на месте или датированный документ."),
    "source_snapshot_historical": ("Исходный снимок описывает прошлую дату.",
                                   "Сверьте объект с более поздним источником."),
    "height_unknown_flat_only": ("Высота неизвестна; показан только плоский контур.",
                                 "Нужна измеренная высота или подтверждённая этажность."),
    "height_from_floors_estimate": ("Объём построен по оценке из числа этажей.",
                                    "Нужна измеренная высота для точной модели."),
    "height_tag_conflict_flat_only": ("Данные о высоте противоречат друг другу; показан плоский контур.",
                                      "Нужно разрешить противоречие независимым измерением."),
    "invalid_height_tag_flat_only": ("Значение высоты не прошло проверку; показан плоский контур.",
                                      "Нужна корректная измеренная высота."),
    "geometry_invalid": ("Контур не прошёл проверку геометрии.",
                         "Нужен исправленный проверяемый контур."),
    "geometry_missing": ("Для записи не найден проверяемый контур.",
                         "Нужен геопривязанный план или проверенный картографический контур."),
    "geometry_outside_moscow": ("Контур расположен вне Москвы.",
                                "Проверьте регион и связь с объектом."),
    "scope_review_required": ("Назначение или регион записи требуют проверки.",
                              "Нужен документ, который однозначно подтверждает московский жилой объект."),
    "source_duplicate_review": ("Возможен дубль записи источника.",
                                "Нужно сверить идентификаторы и даты исходных записей."),
}

ATTEMPT_KEYS: dict[str, tuple[str, ...]] = {
    "registry_project": ("estate_project_geometry", "strict_address_key",
                         "exact_osm_location_object_matches", "strict_osm_address_matches",
                         "verified_point_in_polygon_matches", "validated_parts_parent_matches",
                         "donor_geometry"),
    "registry_review_record": ("source_scope", "estate_project_geometry", "donor_geometry"),
    "mapped_construction": ("source_polygon", "typed_osm_identity", "display_rights",
                            "height_tag", "above_ground_levels", "validated_part_source_id_matches",
                            "validated_parts_parent_match", "donor_geometry"),
    "external_event": ("moscow_residential_scope", "event_identity",
                       "individual_polygon", "donor_geometry"),
}
ATTEMPT_LABELS = {
    "estate_project_geometry": "Контур в исходной карточке",
    "strict_address_key": "Разбор точного адреса",
    "exact_osm_location_object_matches": "Совпадения по проверенному картографическому объекту",
    "strict_osm_address_matches": "Совпадения по строгому адресу",
    "verified_point_in_polygon_matches": "Проверенная точка внутри контура",
    "validated_parts_parent_matches": "Части здания с подтверждённым родителем",
    "donor_geometry": "Заимствованный контур",
    "source_scope": "Допуск к поиску проектной модели",
    "source_polygon": "Картографический контур",
    "typed_osm_identity": "Идентичность картографического объекта",
    "display_rights": "Право публичного показа",
    "height_tag": "Высота по карте",
    "above_ground_levels": "Этажность по карте",
    "validated_part_source_id_matches": "Части здания по точному ключу",
    "validated_parts_parent_match": "Связь частей с родительским зданием",
    "moscow_residential_scope": "Московский жилой объект",
    "event_identity": "Учёт исходного события",
    "individual_polygon": "Контур конкретного здания",
}
ATTEMPT_VALUES: dict[str, dict[str, str]] = {
    "estate_project_geometry": {"absent_in_source": "Контур отсутствует в исходной записи."},
    "strict_address_key": {
        "parsed": "Адрес разобран однозначно.",
        "address_has_holding_plot_demolition_or_nearby_context":
            "Адрес содержит владение, участок, снос или ориентир; точное совпадение не доказано.",
        "address_contains_unmapped_building_component":
            "Есть компонент корпуса без точного картографического соответствия.",
        "address_has_parenthetical_address_component":
            "Адрес содержит уточнение в скобках; точное совпадение не доказано.",
        "address_not_unambiguously_parseable": "Адрес не удалось однозначно разобрать.",
        "address_contains_unmapped_trailing_components":
            "Есть непроверенные дополнительные компоненты адреса.",
    },
    "donor_geometry": {
        "not_admitted_without_verified_project_corpus_identity":
            "Не использован без подтверждённой связи с конкретным корпусом.",
        "not_admitted_without_residential_and_corpus_identity":
            "Не использован до проверки жилого назначения и идентичности корпуса.",
        "not_admitted_for_non_moscow_or_non_building_event":
            "Не использован для события вне московского жилого строительства.",
        "not_used": "Не потребовался для проверенного картографического контура.",
    },
    "source_scope": {"review_required_before_project_geometry_search":
                     "Назначение объекта требует проверки до поиска проектной модели."},
    "source_polygon": {"valid": "Контур прошёл геометрическую проверку."},
    "typed_osm_identity": {"verified_against_audit_jsonl":
                           "Идентичность подтверждена по проверенному снимку."},
    "display_rights": {"ODbL-1.0_cleared": "Показ контура разрешён с обязательной атрибуцией."},
    "height_tag": {"mapped": "Значение высоты найдено.",
                   "absent_or_invalid": "Корректной высоты нет."},
    "above_ground_levels": {"mapped": "Число этажей найдено.",
                            "absent_or_invalid": "Корректной этажности нет."},
    "moscow_residential_scope": {
        "sochi_event": "Событие относится к Сочи, не к Москве.",
        "incidental_gas_pipeline_mention": "Упоминание относится к газопроводу, не к жилому корпусу.",
    },
    "event_identity": {"source_record_id_preserved": "Событие учтено отдельно без слияния с ЖК."},
    "individual_polygon": {"absent_in_source": "Контур конкретного здания отсутствует."},
}
ATTEMPT_COUNTS = frozenset({"exact_osm_location_object_matches",
                            "strict_osm_address_matches", "verified_point_in_polygon_matches",
                            "validated_parts_parent_matches", "validated_part_source_id_matches"})


def _public_attempts(row: dict[str, Any]) -> list[dict[str, Any]]:
    entity_type = row["entity_type"]
    raw = row.get("attempt_log")
    ordered = ATTEMPT_KEYS[entity_type]
    if not isinstance(raw, dict) or set(raw) != set(ordered):
        raise ValueError("Model assessment has incomplete attempt log")
    attempts: list[dict[str, Any]] = []
    for code in ordered:
        value = raw[code]
        count: int | None = None
        if code in ATTEMPT_COUNTS:
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10_000:
                raise ValueError("Invalid model attempt count")
            count = value
            result = "Совпадений нет." if value == 0 else f"Найдено совпадений: {value}."
        elif code == "validated_parts_parent_match":
            if not isinstance(value, bool):
                raise ValueError("Invalid model parts match result")
            result = "Связь подтверждена." if value else "Связь не подтверждена."
        else:
            if not isinstance(value, str) or value not in ATTEMPT_VALUES[code]:
                raise ValueError("Unknown model attempt outcome")
            result = ATTEMPT_VALUES[code][value]
        attempts.append({"code": code, "label_ru": ATTEMPT_LABELS[code],
                         "result_ru": result, "count": count})
    return attempts


def _require_sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or HASH_RE.fullmatch(value) is None:
        raise ValueError(f"Invalid model {field}")
    return value


def _require_utc_datetime(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Invalid model {field}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Invalid model {field}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"Model {field} needs a time zone")
    return value


def _optional_date(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Invalid model {field}")
    try:
        date.fromisoformat(value)
    except ValueError:
        _require_utc_datetime(value, field)
    return value


def _hash_file(path: Path, *, max_bytes: int | None = None) -> tuple[str, int]:
    if not path.is_file():
        raise ValueError("Model run file is missing")
    size = path.stat().st_size
    if max_bytes is not None and size > max_bytes:
        raise ValueError("Model run file exceeds its size limit")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest(), size


def _positive_count(value: Any, field: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < (0 if allow_zero else 1):
        raise ValueError(f"Invalid model {field}")
    return value


def _count_map(value: Any, field: str) -> dict[str, int]:
    if not isinstance(value, dict) or not value:
        raise ValueError(f"Invalid model {field}")
    result: dict[str, int] = {}
    for key, count in value.items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key):
            raise ValueError(f"Invalid model {field} key")
        result[key] = _positive_count(count, field, allow_zero=True)
    return result


def _lonlat(value: Any, field: str) -> list[float] | None:
    if value is None:
        return None
    if (not isinstance(value, list) or len(value) != 2
            or any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in value)):
        raise ValueError(f"Invalid model {field}")
    lon, lat = float(value[0]), float(value[1])
    if not (36.6 <= lon <= 38.2 and 54.9 <= lat <= 56.3):
        raise ValueError(f"Model {field} is outside the Moscow gate")
    return [lon, lat]


def _bbox(value: Any) -> list[float] | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError("Invalid model bbox")
    west_south = _lonlat(value[:2], "bbox")
    east_north = _lonlat(value[2:], "bbox")
    assert west_south is not None and east_north is not None
    if west_south[0] > east_north[0] or west_south[1] > east_north[1]:
        raise ValueError("Invalid model bbox ordering")
    return [*west_south, *east_north]


def _preview_footprint(value: Any, bbox: list[float] | None) -> list[list[float]] | None:
    if value is None:
        return None
    if (bbox is None or not isinstance(value, list)
            or not 3 <= len(value) <= MAX_PREVIEW_VERTICES):
        raise ValueError("Invalid model preview footprint")
    points = [_lonlat(point, "preview footprint") for point in value]
    assert all(point is not None for point in points)
    result = [point for point in points if point is not None]
    for lon, lat in result:
        if not (bbox[0] - 1e-5 <= lon <= bbox[2] + 1e-5
                and bbox[1] - 1e-5 <= lat <= bbox[3] + 1e-5):
            raise ValueError("Model preview footprint leaves its bbox")
    return result


def _preview_geometry(value: Any, bbox: list[float] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if bbox is None or not isinstance(value, dict) or set(value) != {"type", "coordinates"}:
        raise ValueError("Invalid model preview geometry")
    kind = value["type"]
    coordinates = value["coordinates"]
    if kind not in {"Polygon", "MultiPolygon"} or not isinstance(coordinates, list):
        raise ValueError("Invalid model preview geometry type")
    polygons = [coordinates] if kind == "Polygon" else coordinates
    if not 1 <= len(polygons) <= 32:
        raise ValueError("Invalid model preview polygon count")
    total = 0
    validated: list[list[list[list[float]]]] = []
    for polygon in polygons:
        if not isinstance(polygon, list) or not 1 <= len(polygon) <= 32:
            raise ValueError("Invalid model preview rings")
        rings: list[list[list[float]]] = []
        for ring in polygon:
            if not isinstance(ring, list) or len(ring) < 4:
                raise ValueError("Invalid model preview ring")
            total += len(ring)
            if total > MAX_PREVIEW_COORDINATES:
                raise ValueError("Model preview exceeds the coordinate limit")
            points = [_lonlat(point, "preview geometry") for point in ring]
            if any(point is None for point in points):
                raise ValueError("Invalid model preview coordinate")
            typed_points = [point for point in points if point is not None]
            if typed_points[0] != typed_points[-1] or len({tuple(p) for p in typed_points[:-1]}) < 3:
                raise ValueError("Model preview ring is not closed")
            area = sum(typed_points[i][0] * typed_points[i + 1][1]
                       - typed_points[i + 1][0] * typed_points[i][1]
                       for i in range(len(typed_points) - 1))
            if abs(area) < 1e-14:
                raise ValueError("Model preview ring has no area")
            for lon, lat in typed_points:
                if not (bbox[0] - 1e-5 <= lon <= bbox[2] + 1e-5
                        and bbox[1] - 1e-5 <= lat <= bbox[3] + 1e-5):
                    raise ValueError("Model preview geometry leaves its bbox")
            rings.append(typed_points)
        validated.append(rings)
    return {"type": kind, "coordinates": validated[0] if kind == "Polygon" else validated}


def _safe_name(value: Any, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    value = " ".join(value.split())
    if (not 1 <= len(value) <= 180 or "://" in value or "\\" in value
            or "/" in value or "<" in value or ">" in value
            or any(term in value.casefold() for term in
                   ("estateline", "openstreetmap", "минстрой"))):
        return fallback
    return value


def _safe_height_evidence(value: Any, ready: bool) -> dict[str, Any]:
    if not ready:
        return {"mapped_height_m": None, "mapped_levels": None,
                "estimated_render_height_m": None, "render_height_m": None,
                "render_height_basis": None}
    if not isinstance(value, dict):
        raise ValueError("Ready model lacks height evidence")
    result: dict[str, Any] = {}
    for field in ("mapped_height_m", "estimated_render_height_m", "render_height_m"):
        number = value.get(field)
        if number is not None:
            if (isinstance(number, bool) or not isinstance(number, (int, float))
                    or not math.isfinite(number) or not 0 < number <= 400):
                raise ValueError(f"Invalid model {field}")
            number = float(number)
        result[field] = number
    levels = value.get("mapped_levels")
    if levels is not None and (isinstance(levels, bool) or not isinstance(levels, int)
                               or not 1 <= levels <= 100):
        raise ValueError("Invalid model mapped_levels")
    result["mapped_levels"] = levels
    basis = value.get("render_height_basis")
    if basis not in {None, "mapped_height_tag", "mapped_floors_times_3m"}:
        raise ValueError("Invalid model render_height_basis")
    result["render_height_basis"] = basis
    if (result["render_height_m"] is None) != (basis is None):
        raise ValueError("Model height and basis disagree")
    if basis == "mapped_height_tag" and result["render_height_m"] != result["mapped_height_m"]:
        raise ValueError("Measured model height differs from its source")
    if (basis == "mapped_floors_times_3m"
            and result["render_height_m"] != result["estimated_render_height_m"]):
        raise ValueError("Estimated model height differs from its source")
    return result


def _public_explanation(row: dict[str, Any], reasons: list[str]) -> tuple[str, str]:
    if reasons:
        return (" ".join(REASONS[code][0] for code in reasons[:3]),
                REASONS[reasons[0]][1])
    if row["entity_type"] == "mapped_construction":
        return ("Контур отмечен как строящийся; текущая стадия и связь с ЖК не подтверждены.",
                "Проверьте дату картографического источника и проектную идентичность.")
    return ("Индивидуальная модель прошла проверку для указанного вида отображения.",
            "Проверьте дату и качество модели перед использованием.")


def _validate_public_manifest(public_root: str, expected_sha: str,
                              run_id: str) -> tuple[set[str], int]:
    path = ASSET_ROOT / public_root / "public_manifest.json"
    actual_sha, _ = _hash_file(path, max_bytes=MAX_PUBLIC_MANIFEST_BYTES)
    if actual_sha != expected_sha:
        raise ValueError("Public model manifest hash mismatch")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(manifest, dict) or manifest.get("public_redacted_source_ids") is not True
            or manifest.get("run_id") != run_id):
        raise ValueError("Public model root is not sanitized")
    if set(manifest) - {"schema_version", "run_id", "public_redacted_source_ids",
                         "attribution", "license", "model_count", "files"}:
        raise ValueError("Public model manifest contains unreviewed metadata")
    model_count = _positive_count(manifest.get("model_count"), "public model_count",
                                  allow_zero=True)
    files = manifest.get("files")
    if not isinstance(files, list) or len(files) > 20_000:
        raise ValueError("Invalid public model file list")
    names: set[str] = set()
    for record in files:
        if not isinstance(record, dict):
            raise ValueError("Invalid public model file record")
        name = record.get("path")
        if not isinstance(name, str) or MODEL_FILE_RE.fullmatch(name) is None or name in names:
            raise ValueError("Invalid or duplicate public model filename")
        expected_file_sha = _require_sha(record.get("sha256"), "public file sha256")
        expected_size = _positive_count(record.get("bytes"), "public file bytes")
        if expected_size > MAX_PUBLIC_FILE_BYTES:
            raise ValueError("Public model asset exceeds size limit")
        actual_file_sha, actual_size = _hash_file(ASSET_ROOT / public_root / name,
                                                  max_bytes=MAX_PUBLIC_FILE_BYTES)
        if (actual_file_sha, actual_size) != (expected_file_sha, expected_size):
            raise ValueError("Public model asset hash or size mismatch")
        names.add(name)
    if len(list((ASSET_ROOT / public_root).iterdir())) != len(names) + 1:
        raise ValueError("Public model root contains an unlisted file")
    for name in names:
        if not name.endswith(".tileset.json"):
            continue
        document = json.loads((ASSET_ROOT / public_root / name).read_text(encoding="utf-8"))
        pending = [document]
        while pending:
            node = pending.pop()
            if isinstance(node, dict):
                for key, value in node.items():
                    if not isinstance(key, str):
                        raise ValueError("Public model tileset has an invalid key")
                    lowered = key.casefold()
                    if (lowered in {"source_id", "source_record_id", "source_url", "provider_url",
                                    "raw_tags", "provenance", "osm_id", "osm_type", "osm_version"}
                            or lowered.startswith("source_path")):
                        raise ValueError("Public model tileset contains source metadata")
                    if isinstance(value, str) and any(token in value.casefold() for token in
                                                      ("https://", "http://", ".xlsx",
                                                       "1_xls_estateline", "/data/models/")):
                        raise ValueError("Public model tileset contains a private reference")
                uri = node.get("uri")
                if uri is not None and (not isinstance(uri, str) or uri not in names
                                        or not uri.endswith(".glb")):
                    raise ValueError("Public model tileset has an unsafe URI")
                pending.extend(node.values())
            elif isinstance(node, list):
                pending.extend(node)
    if sum(name.endswith(".tileset.json") for name in names) != model_count:
        raise ValueError("Public model count does not match tileset files")
    return names, model_count


@lru_cache(maxsize=8)
def _public_model_filenames(public_root: str, mtime_ns: int, size: int) -> frozenset[str]:
    """Validate a versioned root once, including compatibility roots."""
    if size > MAX_PUBLIC_MANIFEST_BYTES:
        raise ValueError("Oversized public model manifest")
    run_id = "models-v1-" + public_root.removeprefix("public-project-models-v1-")
    manifest_path = MODELS_ROOT / run_id / "manifest.json"
    _hash_file(manifest_path, max_bytes=1_000_000)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (not isinstance(manifest, dict) or manifest.get("run_id") != run_id
            or manifest.get("public_root") != public_root
            or manifest.get("schema_version") != SCHEMA_VERSION):
        raise ValueError("Invalid public model run metadata")
    names, _ = _validate_public_manifest(
        public_root, _require_sha(manifest.get("public_manifest_sha256"),
                                  "public_manifest_sha256"), run_id)
    return frozenset(names)


def public_model_filenames(public_root: str) -> frozenset[str]:
    if PUBLIC_ROOT_RE.fullmatch(public_root) is None:
        raise ValueError("Invalid public model root")
    path = ASSET_ROOT / public_root / "public_manifest.json"
    stat = path.stat()
    return _public_model_filenames(public_root, stat.st_mtime_ns, stat.st_size)


def _project_row(row: Any, registry_names: dict[str, str], public_root: str,
                 public_files: set[str]) -> tuple[dict[str, Any], dict[str, Any] | None,
                                                  list[dict[str, Any]]]:
    if not isinstance(row, dict):
        raise ValueError("Model assessment must be an object")
    candidate_id = row.get("candidate_id")
    if not isinstance(candidate_id, str) or CANDIDATE_ID_RE.fullmatch(candidate_id) is None:
        raise ValueError("Invalid model candidate ID")
    registry_id = row.get("registry_id")
    if registry_id is not None and (
        not isinstance(registry_id, str) or REGISTRY_ID_RE.fullmatch(registry_id) is None
        or registry_id not in registry_names
    ):
        raise ValueError("Invalid linked registry ID")
    entity_type = row.get("entity_type")
    if entity_type not in ENTITY_GROUPS:
        raise ValueError("Unknown model entity type")
    attempts = _public_attempts(row)
    if (entity_type == "registry_project") != (registry_id is not None):
        raise ValueError("Model registry association conflicts with entity type")
    decision = row.get("decision")
    readiness = row.get("asset_readiness")
    link_status = row.get("project_link_status")
    if decision not in DECISIONS or readiness not in ASSET_READINESS or link_status not in LINK_STATUSES:
        raise ValueError("Invalid model decision state")
    reasons = row.get("reason_codes")
    if (not isinstance(reasons, list) or len(reasons) > 8
            or any(not isinstance(code, str) or code not in REASONS for code in reasons)
            or len(set(reasons)) != len(reasons)):
        raise ValueError("Invalid model reason codes")
    if decision != "ready" and not reasons:
        raise ValueError("Non-ready assessment needs a reason")
    selected = row.get("selected_model")
    if decision == "ready":
        if readiness != "ready_to_render" or not isinstance(selected, dict):
            raise ValueError("Ready assessment has no selected asset")
    elif selected is not None or readiness == "ready_to_render":
        raise ValueError("Non-ready assessment exposes a selected asset")
    if entity_type == "mapped_construction" and decision == "ready" and link_status != "unlinked":
        raise ValueError("Mapped construction cannot claim a project link")
    if entity_type == "external_event" and decision == "ready":
        raise ValueError("External event cannot become a building model")
    location = _lonlat(row.get("location"), "location")
    bbox = _bbox(row.get("bbox"))
    preview = _preview_footprint(row.get("preview_footprint"), bbox)
    preview_geometry = _preview_geometry(row.get("preview_geometry"), bbox)
    known_at = _optional_date(row.get("geometry_as_of"), "geometry_as_of")
    name = registry_names[registry_id] if registry_id else _safe_name(
        row.get("name"), "Кандидат модели " + candidate_id[-6:])
    height_evidence = _safe_height_evidence(row.get("height_evidence"), decision == "ready")
    render_height = height_evidence["render_height_m"]
    explanation, next_step = _public_explanation(row, reasons)
    public = {
        "candidate_id": candidate_id, "registry_id": registry_id,
        "name": name, "source_group": ENTITY_GROUPS[entity_type],
        "entity_type": entity_type, "decision": decision,
        "asset_readiness": readiness, "project_link_status": link_status,
        "ready_to_render": decision == "ready", "depiction": row.get("depiction"),
        "model_quality": row.get("model_quality"),
        "geometry_quality": row.get("geometry_quality"),
        "geometry_as_of": known_at, "reason_codes": reasons,
        "render_height_m": render_height, "height_evidence": height_evidence,
        "explanation_ru": explanation, "next_step_ru": next_step,
    }
    for key in ("depiction", "model_quality", "geometry_quality"):
        value = public[key]
        if value is not None and (not isinstance(value, str)
                                  or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value) is None):
            raise ValueError(f"Invalid model {key}")
    if public["depiction"] not in DEPICTIONS:
        raise ValueError("Invalid model depiction")
    if decision == "ready" and (public["model_quality"] is None
                                or public["geometry_quality"] is None
                                or public["depiction"] == "none"):
        raise ValueError("Ready model lacks a specific depiction or quality")
    if (public["depiction"] == "schematic_massing") != (render_height is not None):
        raise ValueError("Model depiction and usable render height disagree")
    descriptor: dict[str, Any] | None = None
    if selected is not None:
        url = selected.get("tileset_url")
        kind = selected.get("kind")
        content_hash = _require_sha(selected.get("content_hash"), "content_hash")
        expected_url = f"/assets/{public_root}/model_{content_hash}.tileset.json"
        if url != expected_url or f"model_{content_hash}.tileset.json" not in public_files:
            raise ValueError("Selected model URL is not a checked public asset")
        if kind not in MODEL_KINDS:
            raise ValueError("Invalid selected model kind")
        if kind == "project_model" and (registry_id is None or link_status != "verified"):
            raise ValueError("Project model lacks a verified registry link")
        if kind == "mapped_construction" and entity_type != "mapped_construction":
            raise ValueError("Mapped model has an incompatible entity type")
        rights = selected.get("rights_evidence")
        if (not isinstance(rights, dict) or rights.get("display_cleared") is not True
                or not isinstance(rights.get("license"), str)
                or not rights["license"]):
            raise ValueError("Selected model lacks public display rights evidence")
        if kind == "mapped_construction" and rights["license"] != "ODbL-1.0":
            raise ValueError("Mapped model has incorrect source rights")
        attribution = ("© OpenStreetMap contributors; ODbL 1.0" if kind == "mapped_construction"
                       else selected.get("public_attribution"))
        if not isinstance(attribution, str) or not 1 <= len(attribution) <= 240:
            raise ValueError("Selected model lacks a safe attribution")
        descriptor = {"url": url, "kind": kind,
                      "model_quality": public["model_quality"], "attribution": attribution}
        if bbox is None:
            raise ValueError("Selected model lacks checked bounds")
    geometry = {"center": location, "bbox": bbox, "footprint": preview,
                "preview_geometry": preview_geometry,
                "render_height_m": render_height, "height_evidence": height_evidence,
                "model_tileset": descriptor}
    return public, geometry if descriptor is not None else None, attempts


@lru_cache(maxsize=4)
def _load_run(run_id: str, manifest_sha256: str, source_snapshot_id: str,
              registry_name_items: tuple[tuple[str, str], ...],
              pinned_public_root: str | None, manifest_mtime_ns: int,
              manifest_size: int, assessments_mtime_ns: int = 0,
              assessments_size: int = 0, public_mtime_ns: int = 0,
              public_size: int = 0) -> dict[str, Any]:
    run_dir = MODELS_ROOT / run_id
    manifest_path = run_dir / "manifest.json"
    actual_manifest_sha, _ = _hash_file(manifest_path, max_bytes=1_000_000)
    if actual_manifest_sha != manifest_sha256:
        raise ValueError("Active model manifest hash mismatch")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION
            or manifest.get("run_id") != run_id):
        raise ValueError("Unsupported model run schema")
    checked_at = _require_utc_datetime(manifest.get("checked_at"), "checked_at")
    total = _positive_count(manifest.get("records_total"), "records_total")
    decision_counts = _count_map(manifest.get("decision_counts"), "decision_counts")
    readiness_counts = _count_map(manifest.get("asset_readiness_counts"), "asset_readiness_counts")
    source_counts = _count_map(manifest.get("source_counts"), "source_counts")
    expected_sources = {"estateline_accepted", "estateline_review", "osm_construction",
                        "minstroy_residential_event", "minstroy_text_audit"}
    if set(source_counts) != expected_sources:
        raise ValueError("Model run source groups are incomplete")
    if set(decision_counts) != DECISIONS or sum(decision_counts.values()) != total:
        raise ValueError("Model decision counts do not match total")
    if sum(readiness_counts.values()) != total or sum(source_counts.values()) != total:
        raise ValueError("Model readiness/source counts do not match total")
    if manifest.get("registry_snapshot_id") != source_snapshot_id:
        raise ValueError("Model run does not match active registry snapshot")
    public_root = manifest.get("public_root")
    if not isinstance(public_root, str) or PUBLIC_ROOT_RE.fullmatch(public_root) is None:
        raise ValueError("Invalid public model root")
    asset_roots = asset_manifest()
    if pinned_public_root is None:
        if asset_roots.get("project_models_root") != public_root:
            raise ValueError("Public model root is not the active sanitized pointer")
    elif (pinned_public_root != public_root or public_root not in {
            asset_roots.get("project_models_root"),
            *asset_roots.get("previous_public_roots", [])}):
        raise ValueError("Pinned model root is not a sanitized published asset")
    public_files, public_model_count = _validate_public_manifest(
        public_root, _require_sha(manifest.get("public_manifest_sha256"), "public_manifest_sha256"),
        run_id)
    for basename, hash_key in (("candidates.jsonl", "candidates_sha256"),
                               ("source_evaluations.jsonl", "source_evaluations_sha256")):
        actual_sha, _ = _hash_file(run_dir / basename)
        if actual_sha != _require_sha(manifest.get(hash_key), hash_key):
            raise ValueError("Model run audit hash mismatch")
    assessments_path = run_dir / "assessments.jsonl"
    assessments_sha, _ = _hash_file(assessments_path, max_bytes=MAX_ASSESSMENTS_BYTES)
    if assessments_sha != _require_sha(manifest.get("assessments_sha256"), "assessments_sha256"):
        raise ValueError("Model assessments hash mismatch")
    registry_names = dict(registry_name_items)
    rows: list[dict[str, Any]] = []
    geometries: dict[str, dict[str, Any]] = {}
    attempts_by_id: dict[str, list[dict[str, Any]]] = {}
    seen_ids: set[str] = set()
    linked_ids: set[str] = set()
    with assessments_path.open("r", encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            public, geometry, attempts = _project_row(json.loads(line), registry_names,
                                                      public_root, public_files)
            candidate_id = public["candidate_id"]
            if candidate_id in seen_ids:
                raise ValueError("Duplicate model candidate ID")
            seen_ids.add(candidate_id)
            registry_id = public["registry_id"]
            if registry_id is not None:
                if registry_id in linked_ids:
                    raise ValueError("Duplicate registry assessment")
                linked_ids.add(registry_id)
            rows.append(public)
            attempts_by_id[candidate_id] = attempts
            if geometry is not None:
                geometries[candidate_id] = geometry
    if len(rows) != total or linked_ids != set(registry_names):
        raise ValueError("Model run does not cover every registry row exactly once")
    actual_decisions = Counter(row["decision"] for row in rows)
    actual_readiness = Counter(row["asset_readiness"] for row in rows)
    actual_entities = Counter(row["entity_type"] for row in rows)
    if (actual_entities["registry_project"] != source_counts["estateline_accepted"]
            or actual_entities["registry_review_record"] != source_counts["estateline_review"]
            or actual_entities["mapped_construction"] != source_counts["osm_construction"]
            or actual_entities["external_event"] != source_counts["minstroy_residential_event"]
               + source_counts["minstroy_text_audit"]):
        raise ValueError("Model source group counts differ from assessments")
    if any(actual_decisions[key] != value for key, value in decision_counts.items()):
        raise ValueError("Model decision counts differ from assessments")
    if any(actual_readiness[key] != value for key, value in readiness_counts.items()):
        raise ValueError("Model readiness counts differ from assessments")
    if len(geometries) != decision_counts["ready"] or public_model_count != len(geometries):
        raise ValueError("Ready models and checked assets disagree")
    rows.sort(key=lambda row: (row["name"].casefold(), row["candidate_id"]))
    return {"dataset_id": "model_candidates", "registry_snapshot_id": source_snapshot_id,
            "model_run_id": run_id, "checked_at": checked_at, "run_status": "ready",
            "counts": {"scanned": total, **decision_counts},
            "items": rows, "by_id": {row["candidate_id"]: row for row in rows},
            "geometries": geometries, "attempts_by_id": attempts_by_id,
            "public_root": public_root}


def load_model_run(registry: dict[str, Any] | None = None) -> dict[str, Any]:
    generation = current_generation()
    if generation is None and not ACTIVE_PATH.is_file():
        raise RuntimeError("Model evaluation has not been published")
    if generation is None and ACTIVE_PATH.stat().st_size > 4096:
        raise ValueError("Invalid model active pointer")
    pointer = generation["model"] if generation else json.loads(ACTIVE_PATH.read_text(encoding="utf-8"))
    if not isinstance(pointer, dict):
        raise ValueError("Invalid model active pointer")
    run_id = pointer.get("run_id")
    if not isinstance(run_id, str) or RUN_ID_RE.fullmatch(run_id) is None:
        raise ValueError("Invalid model run ID")
    expected_sha = _require_sha(pointer.get("manifest_sha256"), "manifest_sha256")
    manifest_path = MODELS_ROOT / run_id / "manifest.json"
    stat = manifest_path.stat()
    assessment_stat = (MODELS_ROOT / run_id / "assessments.jsonl").stat()
    public_root = pointer.get("public_root") if generation else asset_manifest().get("project_models_root")
    if not isinstance(public_root, str) or PUBLIC_ROOT_RE.fullmatch(public_root) is None:
        raise ValueError("No active public model root")
    public_stat = (ASSET_ROOT / public_root / "public_manifest.json").stat()
    registry = registry if registry is not None else load_registry()
    names = tuple((item["registry_id"], item["name"]) for item in registry["items"])
    run = _load_run(run_id, expected_sha, registry["source_snapshot_id"], names,
                    public_root if generation else None, stat.st_mtime_ns, stat.st_size,
                    assessment_stat.st_mtime_ns, assessment_stat.st_size,
                    public_stat.st_mtime_ns, public_stat.st_size)
    if run["public_root"] != public_root or run["registry_snapshot_id"] != registry["source_snapshot_id"]:
        raise ValueError("Model run no longer matches its public or registry pointer")
    return run


def select_candidates(run: dict[str, Any], *, query: str | None = None,
                      decision: str | None = None, reason_code: str | None = None,
                      source_group: str | None = None,
                      registry_id: str | None = None) -> list[dict[str, Any]]:
    if decision is not None and decision not in DECISIONS:
        raise ValueError("Unknown model decision")
    if reason_code is not None and reason_code not in REASONS:
        raise ValueError("Unknown model reason code")
    if source_group is not None and source_group not in set(ENTITY_GROUPS.values()):
        raise ValueError("Unknown model source group")
    if registry_id is not None and REGISTRY_ID_RE.fullmatch(registry_id) is None:
        raise ValueError("Invalid registry ID filter")
    needle = query.strip().casefold() if query else None
    return [row for row in run["items"]
            if (not needle or needle in row["name"].casefold()
                or needle in row["candidate_id"].casefold())
            and (decision is None or row["decision"] == decision)
            and (reason_code is None or reason_code in row["reason_codes"])
            and (source_group is None or row["source_group"] == source_group)
            and (registry_id is None or row["registry_id"] == registry_id)]
