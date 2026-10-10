"""Validate private, source-backed complex memberships before public projection.

The active model run may contain ``map_point_groups.private.json``. Absence means
there are no verified multi-point groups. This reader never derives membership
from names, coordinates, or labels and never returns private evidence fields.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from backend.store import ROOT


MODELS_ROOT = ROOT / "data/models"
SIDECAR_NAME = "map_point_groups.private.json"
SIDECAR_SCHEMA = "map-point-groups-private/1"
PUBLIC_SCHEMA_VERSION = 1
MAX_SIDECAR_BYTES = 2_000_000
MAX_GROUPS = 2_000
MAX_MEMBERS_PER_GROUP = 1_000

RUN_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{1,79}\Z")
SNAPSHOT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}\Z")
POINT_ID_RE = re.compile(r"mappt_[0-9a-f]{24}\Z")
OPAQUE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}\Z")
DATASET_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,119}\Z")
IDENTITY_BASES = frozenset({
    "source_complex_id", "explicit_relation_membership", "verified_crosswalk",
})


class GroupEvidenceError(ValueError):
    """Private grouping evidence is malformed, stale, or unsafe to publish."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GroupEvidenceError("Duplicate group sidecar field")
        result[key] = value
    return result


def _exact_keys(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise GroupEvidenceError(f"Invalid {label} fields")
    return value


def _identifier(value: Any, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise GroupEvidenceError(f"Invalid {label}")
    return value


def _text(value: Any, *, label: str, maximum: int) -> str:
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > maximum or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise GroupEvidenceError(f"Invalid {label}")
    return value


def _read_sidecar(path_root: Path, run_id: str) -> dict[str, Any] | None:
    """Open a bounded run-local JSON file without following a symlink."""
    directory_fd: int | None = None
    file_fd: int | None = None
    try:
        root_fd = os.open(path_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                          | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            directory_fd = os.open(run_id, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                                   | getattr(os, "O_CLOEXEC", 0)
                                   | getattr(os, "O_NOFOLLOW", 0), dir_fd=root_fd)
        finally:
            os.close(root_fd)
        try:
            file_fd = os.open(SIDECAR_NAME, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                              | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
        except FileNotFoundError:
            return None
        info = os.fstat(file_fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SIDECAR_BYTES
                or info.st_mode & 0o022):
            raise GroupEvidenceError("Invalid group sidecar file")
        chunks = bytearray()
        while len(chunks) <= MAX_SIDECAR_BYTES:
            block = os.read(file_fd, min(65_536, MAX_SIDECAR_BYTES + 1 - len(chunks)))
            if not block:
                break
            chunks.extend(block)
        if len(chunks) > MAX_SIDECAR_BYTES:
            raise GroupEvidenceError("Group sidecar exceeds size limit")
        try:
            value = json.loads(chunks.decode("utf-8"), object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GroupEvidenceError("Invalid group sidecar JSON") from exc
        if not isinstance(value, dict):
            raise GroupEvidenceError("Invalid group sidecar document")
        return value
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            raise GroupEvidenceError("Active model run directory is missing") from exc
        raise GroupEvidenceError("Unable to read group sidecar") from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def _validate_member_rows(value: Any, expected_ids: list[str]) -> None:
    if not isinstance(value, list) or len(value) != len(expected_ids):
        raise GroupEvidenceError("Group membership evidence does not cover its points")
    seen: set[str] = set()
    for row in value:
        member = _exact_keys(row, {
            "point_id", "source_dataset_id", "source_snapshot_id", "source_record_id",
        }, "group member evidence")
        point_id = _identifier(member["point_id"], POINT_ID_RE, "member point ID")
        if point_id not in expected_ids or point_id in seen:
            raise GroupEvidenceError("Duplicate or mismatched member evidence")
        seen.add(point_id)
        _identifier(member["source_dataset_id"], DATASET_ID_RE, "member dataset ID")
        _identifier(member["source_snapshot_id"], SNAPSHOT_RE, "member source snapshot")
        _identifier(member["source_record_id"], OPAQUE_ID_RE, "member source record ID")
    if seen != set(expected_ids):
        raise GroupEvidenceError("Group member evidence is incomplete")


def _validate_proof(value: Any, *, basis: str, identity_key: str,
                    members: list[dict[str, Any]]) -> str:
    proof = _exact_keys(value, {
        "proof_kind", "source_dataset_id", "source_snapshot_id", "source_record_id",
    }, "group proof")
    if proof["proof_kind"] != basis:
        raise GroupEvidenceError("Group proof basis mismatch")
    dataset_id = _identifier(proof["source_dataset_id"], DATASET_ID_RE, "proof dataset ID")
    _identifier(proof["source_snapshot_id"], SNAPSHOT_RE, "proof source snapshot")
    proof_record_id = _identifier(proof["source_record_id"], OPAQUE_ID_RE,
                                  "group proof record ID")
    expected_identity_key = f"{dataset_id}:{proof_record_id}"
    if identity_key != expected_identity_key:
        raise GroupEvidenceError("Group identity key is not the source-scoped proof identity")
    member_datasets = {row["source_dataset_id"] for row in members}
    if basis in {"source_complex_id", "explicit_relation_membership"}:
        if member_datasets != {dataset_id}:
            raise GroupEvidenceError("Source-owned group crosses source datasets")
        if any(row["source_snapshot_id"] != proof["source_snapshot_id"]
               for row in members):
            raise GroupEvidenceError("Source-owned group mixes snapshots")
    elif len(member_datasets) < 2:
        raise GroupEvidenceError("Crosswalk group lacks two source identities")
    digest = hashlib.sha256(f"{basis}\0{identity_key}".encode("utf-8")).hexdigest()[:24]
    return f"group_{digest}"


def _validate_sidecar(document: dict[str, Any], *, registry_snapshot_id: str,
                      model_run_id: str, point_ids: set[str]) -> list[dict[str, Any]]:
    sidecar = _exact_keys(document, {
        "schema_version", "registry_snapshot_id", "model_run_id", "groups",
    }, "group sidecar")
    if (sidecar["schema_version"] != SIDECAR_SCHEMA
            or sidecar["registry_snapshot_id"] != registry_snapshot_id
            or sidecar["model_run_id"] != model_run_id):
        raise GroupEvidenceError("Group sidecar snapshot or run is stale")
    groups = sidecar["groups"]
    if not isinstance(groups, list) or len(groups) > MAX_GROUPS:
        raise GroupEvidenceError("Invalid group collection")
    seen_group_ids: set[str] = set()
    seen_point_ids: set[str] = set()
    projected: list[dict[str, Any]] = []
    for raw_group in groups:
        group = _exact_keys(raw_group, {
            "identity_key", "group_scope", "identity_basis", "group_label", "point_ids",
            "public_display", "members", "proof",
        }, "group")
        identity_key = _identifier(group["identity_key"], OPAQUE_ID_RE,
                                   "private group identity key")
        if group["group_scope"] != "residential_complex":
            raise GroupEvidenceError("Unsupported group scope")
        basis = group["identity_basis"]
        if not isinstance(basis, str) or basis not in IDENTITY_BASES:
            raise GroupEvidenceError("Unsupported group identity basis")
        label = _text(group["group_label"], label="group label", maximum=160)
        if group["public_display"] is not True:
            raise GroupEvidenceError("Group display permission is not explicit")
        member_ids = group["point_ids"]
        if (not isinstance(member_ids, list) or not 2 <= len(member_ids) <= MAX_MEMBERS_PER_GROUP):
            raise GroupEvidenceError("Invalid group point membership")
        member_ids = [_identifier(value, POINT_ID_RE, "group point ID") for value in member_ids]
        if len(set(member_ids)) != len(member_ids):
            raise GroupEvidenceError("Invalid group point membership")
        member_evidence = group["members"]
        _validate_member_rows(member_evidence, member_ids)
        group_id = _validate_proof(group["proof"], basis=basis,
                                   identity_key=identity_key, members=member_evidence)
        if group_id in seen_group_ids:
            raise GroupEvidenceError("Duplicate group ID")
        seen_group_ids.add(group_id)
        if seen_point_ids.intersection(member_ids):
            raise GroupEvidenceError("A map point belongs to multiple groups")
        seen_point_ids.update(member_ids)
        # Public visibility can change while the immutable source/model run stays
        # the same. Never publish a partial/stale group; keep its points separate.
        if not set(member_ids) <= point_ids:
            continue
        projected.append({
            "group_id": group_id,
            "group_scope": "residential_complex",
            "identity_basis": basis,
            "group_label": label,
            "point_ids": member_ids,
        })
    return projected


def load_group_evidence(registry_snapshot_id: str, model_run_id: str,
                        point_ids: set[str] | list[str] | tuple[str, ...]) -> dict[str, Any]:
    """Return a public-safe evidence bundle, empty when no sidecar is published.

    ``point_ids`` must be the full unfiltered set emitted by the active map run.
    A stale or malformed sidecar is an error; it is never partially projected.
    """
    registry_snapshot_id = _identifier(registry_snapshot_id, SNAPSHOT_RE,
                                       "registry snapshot ID")
    model_run_id = _identifier(model_run_id, RUN_ID_RE, "model run ID")
    if not isinstance(point_ids, (set, list, tuple)):
        raise GroupEvidenceError("Invalid active map point ID collection")
    known_ids = {_identifier(value, POINT_ID_RE, "active map point ID") for value in point_ids}
    if len(known_ids) != len(point_ids):
        raise GroupEvidenceError("Duplicate active map point ID")
    document = _read_sidecar(MODELS_ROOT, model_run_id)
    groups = [] if document is None else _validate_sidecar(
        document, registry_snapshot_id=registry_snapshot_id,
        model_run_id=model_run_id, point_ids=known_ids)
    return {
        "schema_version": PUBLIC_SCHEMA_VERSION,
        "visibility": "public",
        "registry_snapshot_id": registry_snapshot_id,
        "model_run_id": model_run_id,
        "groups": groups,
    }
