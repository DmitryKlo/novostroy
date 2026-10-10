from pathlib import Path
import hashlib
import io
import json
import shutil
import subprocess
import tarfile
import pytest

from scripts.package_deploy_data import apply_plan, make_plan
from scripts.runtime_delta import export_delta, import_delta, TransportError
from scripts.tests.test_package_deploy_data import _fixture
from deploy.entrypoint import ROUTING_INPUT_FILES
from deploy.apply_release import verify_release


def source_bundle(tmp_path: Path):
    repo = tmp_path / "source-git"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "branch1", str(repo)], check=True)
    (repo / "source.py").write_text("value = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "source.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture",
                    "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture"], check=True)
    commit = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                            check=True, text=True, stdout=subprocess.PIPE).stdout.strip()
    bundle = tmp_path / "source.bundle"
    subprocess.run(["git", "-C", str(repo), "bundle", "create", str(bundle), "branch1"], check=True)
    return commit, bundle


def prepared(tmp_path: Path):
    source = tmp_path / "source"
    _fixture(source)
    plan, pointer = make_plan(source, "full-compat")
    base = apply_plan(source, tmp_path / "base", "full-compat", plan, pointer)
    base_sha = hashlib.sha256((base / "release-manifest.json").read_bytes()).hexdigest()
    changed = "data/property/estateline/projects.jsonl"
    (source / changed).write_bytes(b'{"updated":true}\n')
    plan, pointer = make_plan(source, "full-compat")
    new = apply_plan(source, tmp_path / "new", "full-compat", plan, pointer,
                     base_release=base, base_manifest_sha256=base_sha)
    commit, bundle = source_bundle(tmp_path)
    delta = export_delta(new, base, base_sha, tmp_path / "delta",
                         source_commit=commit, source_bundle=bundle)
    remote = tmp_path / "remote"
    remote.mkdir()
    shutil.copytree(base, remote / base.name)
    sidecar = delta / "transport.json"
    doc = json.loads(sidecar.read_text())
    return base, new, delta / doc["archive"]["name"], sidecar, remote, changed


def snapshot(root: Path):
    return {str(p.relative_to(root)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mode & 0o777)
            for p in root.rglob("*") if p.is_file()}


def repack(archive: Path, sidecar: Path, mutate):
    raw = subprocess.run(["zstd", "-q", "-d", "-c", str(archive)], check=True, stdout=subprocess.PIPE).stdout
    out = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r") as original, tarfile.open(fileobj=out, mode="w") as changed:
        for member in original:
            body = original.extractfile(member).read() if member.isfile() else None
            mutate(changed, member, body)
    with archive.open("wb") as stream:
        subprocess.run(["zstd", "-q", "-3", "-c"], input=out.getvalue(), check=True, stdout=stream)
    doc = json.loads(sidecar.read_text())
    doc["archive"].update(sha256=hashlib.sha256(archive.read_bytes()).hexdigest(), bytes=archive.stat().st_size,
                          uncompressed_bytes=len(out.getvalue()))
    sidecar.write_text(json.dumps(doc))


def test_delta_reuses_unchanged_inodes_and_replaces_changed_files_without_touching_base(tmp_path: Path):
    base, new, archive, sidecar, remote, changed = prepared(tmp_path)
    doc = json.loads(sidecar.read_text())
    assert doc["delta"]["file_count"] == 1
    assert doc["delta"]["bytes_total"] == len(b'{"updated":true}\n')
    old = snapshot(remote / "base")
    unchanged = "data/social/osm_moscow_roads_261008.parquet"
    assert (base / unchanged).stat().st_ino == (new / unchanged).stat().st_ino
    assert (base / changed).stat().st_ino != (new / changed).stat().st_ino
    imported = import_delta(archive, sidecar, remote)
    assert snapshot(remote / "base") == old
    assert snapshot(imported) == snapshot(new)
    assert (imported / unchanged).stat().st_ino == (remote / "base" / unchanged).stat().st_ino
    assert (imported / changed).stat().st_ino != (remote / "base" / changed).stat().st_ino
    with pytest.raises(FileExistsError):
        import_delta(archive, sidecar, remote)


@pytest.mark.parametrize("failure", ["base_pin", "base_bytes", "archive_bytes", "new_manifest_pin"])
def test_wrong_base_and_corruption_never_publish_or_mutate_base(tmp_path: Path, failure: str):
    _, _, archive, sidecar, remote, changed = prepared(tmp_path)
    doc = json.loads(sidecar.read_text())
    if failure == "base_pin":
        doc["base"]["manifest_sha256"] = "0"*64
    elif failure == "new_manifest_pin":
        doc["release"]["manifest_sha256"] = "0"*64
    elif failure == "archive_bytes":
        archive.write_bytes(archive.read_bytes()+b"tampered")
    else:
        (remote / "base" / changed).write_bytes(b"corrupt")
    sidecar.write_text(json.dumps(doc))
    old = snapshot(remote / "base")
    with pytest.raises(ValueError):
        import_delta(archive, sidecar, remote)
    assert snapshot(remote / "base") == old
    assert sorted(p.name for p in remote.iterdir()) == ["base"]


@pytest.mark.parametrize("malicious", ["traversal", "symlink", "changed_bytes", "extra_unchanged"])
def test_malicious_or_inconsistent_delta_payload_is_rejected(tmp_path: Path, malicious: str):
    _, _, archive, sidecar, remote, changed = prepared(tmp_path)
    old = snapshot(remote / "base")
    def mutate(tar, member, body):
        if member.name.endswith(changed):
            if malicious == "traversal":
                member.name = "new/../outside"
            elif malicious == "symlink":
                member.type, member.linkname, member.size = tarfile.SYMTYPE, "../../base/"+changed, 0
                body = None
            elif malicious == "changed_bytes":
                body = b"x"*len(body)
            else:
                member.name = "new/data/social/osm_moscow_roads_261008.parquet"
        tar.addfile(member, io.BytesIO(body) if body is not None else None)
    repack(archive, sidecar, mutate)
    with pytest.raises(ValueError):
        import_delta(archive, sidecar, remote)
    assert snapshot(remote / "base") == old
    assert sorted(p.name for p in remote.iterdir()) == ["base"]
    assert not (remote / "outside").exists()


def test_symlinked_base_and_wrong_base_reuse_pin_fail_before_linking(tmp_path: Path):
    base, _, archive, sidecar, remote, _ = prepared(tmp_path)
    shutil.rmtree(remote / "base")
    (remote / "base").symlink_to(base, target_is_directory=True)
    with pytest.raises(TransportError, match="symlink"):
        import_delta(archive, sidecar, remote)
    source = tmp_path / "source"
    plan, pointer = make_plan(source, "full-compat")
    with pytest.raises(TransportError, match="expected SHA"):
        apply_plan(source, tmp_path / "bad", "full-compat", plan, pointer,
                   base_release=base, base_manifest_sha256="0"*64)
    assert not (tmp_path / "bad").exists()


def test_historical_base_without_connectors_can_only_produce_a_complete_new_release(tmp_path: Path):
    source = tmp_path / "source"
    _fixture(source)
    plan, pointer = make_plan(source, "full-compat")
    base = apply_plan(source, tmp_path / "base", "full-compat", plan, pointer)
    manifest_path = base / "release-manifest.json"
    doc = json.loads(manifest_path.read_text())
    for name in ROUTING_INPUT_FILES:
        (base / name).unlink()
    doc["files"] = [row for row in doc["files"] if row["path"] not in ROUTING_INPUT_FILES]
    doc["file_count"] = len(doc["files"])
    doc["bytes_total"] = sum(row["bytes"] for row in doc["files"])
    manifest_path.write_text(json.dumps(doc))
    base_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    before = snapshot(base)
    with pytest.raises(ValueError, match="routing input"):
        verify_release(base)
    new = apply_plan(source, tmp_path / "new", "full-compat", plan, pointer,
                     base_release=base, base_manifest_sha256=base_sha)
    commit, bundle = source_bundle(tmp_path)
    delta = export_delta(new, base, base_sha, tmp_path / "delta",
                         source_commit=commit, source_bundle=bundle)
    sidecar = delta / "transport.json"
    transport = json.loads(sidecar.read_text())
    assert transport["delta"]["file_count"] == len(ROUTING_INPUT_FILES)
    remote = tmp_path / "remote"
    remote.mkdir()
    shutil.copytree(base, remote / "base")
    imported = import_delta(delta / transport["archive"]["name"], sidecar, remote)
    assert snapshot(imported) == snapshot(new)
    assert snapshot(base) == before == snapshot(remote / "base")
    assert all((imported / name).is_file() for name in ROUTING_INPUT_FILES)


def test_partial_legacy_connector_inputs_are_not_accepted_as_a_delta_base(tmp_path: Path):
    base, _, _, _, _, _ = prepared(tmp_path)
    manifest_path = base / "release-manifest.json"
    doc = json.loads(manifest_path.read_text())
    missing = ROUTING_INPUT_FILES[0]
    (base / missing).unlink()
    doc["files"] = [row for row in doc["files"] if row["path"] != missing]
    doc["file_count"] = len(doc["files"])
    doc["bytes_total"] = sum(row["bytes"] for row in doc["files"])
    manifest_path.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="routing input"):
        verify_release(base, allow_legacy_missing_route_inputs=True)


def test_delta_export_rejects_a_source_bundle_from_a_different_commit(tmp_path: Path):
    base, new, _, _, _, _ = prepared(tmp_path)
    base_sha = hashlib.sha256((base / "release-manifest.json").read_bytes()).hexdigest()
    with pytest.raises(TransportError, match="Source bundle branch1"):
        export_delta(new, base, base_sha, tmp_path / "bad-delta",
                     source_commit="0"*40, source_bundle=tmp_path / "source.bundle")
    assert not (tmp_path / "bad-delta").exists()
