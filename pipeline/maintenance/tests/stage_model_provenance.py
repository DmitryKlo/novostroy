"""One bounded real-input fixture for a changed EstateLine staging path.

Runs in a temporary output root. No production pointer or source file changes.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from sources.models import build as model_build
from sources.models import audit as model_audit


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="novostroy-model-stage-") as folder:
        temporary = Path(folder)
        projects = temporary / "projects.jsonl"
        review = temporary / "review_required.jsonl"
        source = model_build.INPUTS["estateline_projects"]
        rows = source.read_text(encoding="utf-8").splitlines()
        first = json.loads(rows[0])
        first["name"] += " [local staging fixture]"
        rows[0] = json.dumps(first, ensure_ascii=False)
        projects.write_text("\n".join(rows) + "\n", encoding="utf-8")
        review.write_bytes(model_build.INPUTS["estateline_review"].read_bytes())
        original_build, original_audit = model_build.INPUTS, model_audit.INPUTS
        selected_inputs = {**original_build, "estateline_projects": projects,
                           "estateline_review": review}
        model_build.INPUTS = selected_inputs
        model_audit.INPUTS = selected_inputs
        try:
            result = model_build.build(temporary / "data/models", temporary / "assets")
            run = temporary / "data/models" / result["run_id"]
            model_audit.audit(run, temporary / "assets")
            public_manifest = temporary / "assets" / result["public_root"] / "public_manifest.json"
            public_digest = model_build.file_sha(public_manifest)
            held = run.with_name(".interrupted-run")
            run.rename(held)
            retried = model_build.build(temporary / "data/models", temporary / "assets")
            if model_build.file_sha(public_manifest) != public_digest:
                raise AssertionError("orphan retry changed existing public assets")
            model_audit.audit(run, temporary / "assets")
        finally:
            model_build.INPUTS, model_audit.INPUTS = original_build, original_audit
        raw_manifest = (run / "manifest.json").read_text(encoding="utf-8")
        if folder in raw_manifest:
            raise AssertionError("temporary staging path leaked into immutable manifest")
        if result["input_hashes"]["estateline_projects"] != model_build.file_sha(projects):
            raise AssertionError("staged source hash missing from model manifest")
        print(json.dumps({"run_id": result["run_id"], "assessments": result["records_total"],
                          "ready": result["decision_counts"].get("ready", 0),
                          "interrupted_public_retry": retried["run_id"] == result["run_id"],
                          "staging_path_in_manifest": False,
                          "temporary_outputs_removed_on_exit": True}, sort_keys=True))


if __name__ == "__main__":
    main()
