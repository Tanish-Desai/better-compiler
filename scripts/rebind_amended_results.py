#!/usr/bin/env python3
"""Carry complete, treatment-identical cells into a baseline amendment.

This creates a NEW results directory. It never edits or deletes the parent
results. Only complete cells whose bug remains in the resolved cohort and
whose source, dataset record, model, prompt, harness, budgets and treatment are
identical are copied. Each copied record retains its original config in an
amendment provenance field.
"""

from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ce.benchmark import load_runs, record_is_complete, run_record_path  # noqa: E402
from ce.experiment import record_config  # noqa: E402
from ce.protocol import load_protocol  # noqa: E402
from scripts.vertex_amendment import (  # noqa: E402
    assert_config_matches_protocol, assert_treatment_unchanged,
)


def atomic_new_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"Refusing to replace existing artifact: {path}")
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def dataset_record_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from", dest="source", default="results-vertex-v3b")
    parser.add_argument("--to", dest="target", default="results-vertex-v3c")
    parser.add_argument("--source-protocol", default="data/repair_v3b_vertex_protocol.json")
    parser.add_argument("--protocol", default="run-provenance/repair_v3c_vertex_resolved.json")
    parser.add_argument("--preflight-report", default="run-provenance/repair_v3c_preflight.json")
    parser.add_argument("--report", default="run-provenance/repair_v3c-amendment.json")
    args = parser.parse_args(argv)

    source_dir, target_dir = Path(args.source), Path(args.target)
    source_protocol_path, target_protocol_path = Path(args.source_protocol), Path(args.protocol)
    preflight_report_path = Path(args.preflight_report)
    report_path = Path(args.report)
    if not source_dir.is_dir():
        parser.error(f"Missing source results directory: {source_dir}")
    if target_dir.exists():
        parser.error(f"Target exists; preserve it and choose a new --to path: {target_dir}")
    if target_dir.resolve().is_relative_to(source_dir.resolve()):
        parser.error("Target results directory must not be inside the source directory")
    if report_path.exists():
        parser.error(f"Amendment report exists; preserve it and choose a new --report path")

    try:
        parent, parent_sha = load_protocol(str(source_protocol_path))
        target_protocol, target_sha = load_protocol(str(target_protocol_path))
        if target_protocol.get("amends_protocol_id") != parent["id"]:
            raise ValueError("Target protocol does not name this parent protocol")
        assert_treatment_unchanged(parent, target_protocol)
        preflight_report = json.loads(preflight_report_path.read_text(encoding="utf-8"))
        resolved_ref = preflight_report.get("resolved_protocol") or {}
        if (preflight_report.get("status") != "passed"
                or preflight_report.get("eligible_bugs") != target_protocol["bugs"]
                or resolved_ref.get("id") != target_protocol["id"]
                or resolved_ref.get("sha256") != target_sha):
            raise ValueError("Target protocol is not the exact cohort frozen by a passing preflight report")

        source_manifest = source_dir / ".experiment.json"
        if not source_manifest.is_file():
            raise ValueError(f"Missing source manifest: {source_manifest}")
        source_config = json.loads(source_manifest.read_text(encoding="utf-8"))
        assert_config_matches_protocol(source_config, parent, parent_sha)

        import examples.repair_experiment as rx
        dataset_args = SimpleNamespace(dataset_bug_ids=target_protocol["bugs"])
        target_dataset_identity = rx.dataset_identity(dataset_args)
        if target_dataset_identity is None:
            raise ValueError("LAB_DATASET_DIR is required to verify target dataset identity")
        target_config = dict(source_config)
        target_config["protocol_id"] = target_protocol["id"]
        target_config["protocol_sha256"] = target_sha
        target_config["dataset_identity"] = target_dataset_identity
        assert_config_matches_protocol(target_config, target_protocol, target_sha)

        allowed_config_changes = {"protocol_id", "protocol_sha256", "dataset_identity"}
        differences = [key for key in sorted(set(source_config) | set(target_config))
                       if key not in allowed_config_changes
                       and source_config.get(key) != target_config.get(key)]
        if differences:
            raise ValueError("Amendment changes run configuration beyond protocol/sample identity: "
                             + ", ".join(differences))
        if source_config.get("harness_code_hash") != target_config.get("harness_code_hash"):
            raise ValueError("Harness code hash changed; old cells cannot be rebound")

        source_records = (source_config.get("dataset_identity") or {}).get("records") or {}
        target_records = target_dataset_identity.get("records") or {}
        target_bugs = set(target_protocol["bugs"])
        parent_bugs = set(parent["bugs"])
        for bug in target_bugs & parent_bugs:
            current_hash = dataset_record_hash(Path(os.environ["LAB_DATASET_DIR"]) / f"{bug}.json")
            if source_records.get(bug) != current_hash or target_records.get(bug) != current_hash:
                raise ValueError(f"Dataset record changed across amendment for bug {bug}")

        expected_conditions = set(target_protocol["conditions"])
        expected_trials = set(range(1, target_protocol["repeat"] + 1))
        copied: list[tuple[str, dict]] = []
        seen = set()
        for record in load_runs(str(source_dir)):
            if record_config(record) != source_config:
                raise ValueError("A source run has a config different from its manifest")
            bug = str(record.get("bug_id"))
            condition = record.get("condition")
            try:
                trial = int(record.get("trial", 1))
            except (TypeError, ValueError):
                continue
            identity = (bug, condition, trial)
            if bug not in target_bugs or bug not in parent_bugs or condition not in expected_conditions \
                    or trial not in expected_trials or identity in seen \
                    or record.get("notes", {}).get("allow_promotion", True) is not True:
                continue
            seen.add(identity)
            source_path = Path(run_record_path(
                str(source_dir), bug, condition,
                allow_promotion=record.get("notes", {}).get("allow_promotion", True),
                trial=trial,
            ))
            if not record_is_complete(str(source_path)):
                continue
            if not record.get("notes", {}).get("baseline_validation", {}).get(
                    "semantic_failure_confirmed"):
                continue
            migrated = json.loads(json.dumps(record))
            notes = migrated.setdefault("notes", {})
            notes["amendment_provenance"] = {
                "source_protocol_id": parent["id"],
                "source_protocol_sha256": parent_sha,
                "source_results_directory": str(source_dir),
                "source_config_sha256": hashlib.sha256(
                    json.dumps(source_config, sort_keys=True).encode("utf-8")).hexdigest(),
                "reason": "Completed treatment-identical cell retained after pre-model verifier eligibility amendment.",
            }
            notes["experiment_config"] = target_config
            destination = run_record_path(
                str(target_dir), bug, condition,
                allow_promotion=notes.get("allow_promotion", True), trial=trial,
            )
            copied.append((destination, migrated))

        report = {
            "schema": 1,
            "created_on": date.today().isoformat(),
            "source_results": str(source_dir),
            "target_results": str(target_dir),
            "source_protocol": {"id": parent["id"], "sha256": parent_sha},
            "target_protocol": {"id": target_protocol["id"], "sha256": target_sha},
            "target_dataset_sha256": target_dataset_identity["sha256"],
            "carried_cells": len(copied),
            "carried_records": [Path(path).name for path, _ in copied],
            "source_records_unchanged": True,
            "budget_ledger_action": "None; preserve and continue using the existing v3b main reservation ledger.",
        }
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{target_dir.name}.staging-",
                                        dir=target_dir.parent))
        try:
            atomic_new_json(staging / ".experiment.json", target_config)
            for destination, record in copied:
                atomic_new_json(staging / Path(destination).name, record)
            os.rename(staging, target_dir)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        atomic_new_json(report_path, report)
        print(f"Carried {len(copied)} completed, baseline-certified cells into {target_dir}.")
        print(f"Amendment report: {report_path}")
        print("Source records and the existing Vertex reservation ledger were not modified.")
        return 0
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
