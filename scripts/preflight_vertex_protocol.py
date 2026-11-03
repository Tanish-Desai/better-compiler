#!/usr/bin/env python3
"""Validate a Vertex repair cohort before spending any model generations.

The preflight builds each not-yet-certified bug at its base commit, checks the
reproducer, and requires a clean Alive2 semantic violation. It never creates a
model client or sends a generation request. A failed primary is replaced only
by the next verifier-eligible reserve in its predeclared complexity stratum.

Run with the native LLVM environment loaded and no other process using the LLVM
checkout. The optional completed-results source lets already completed cells
certify their own baseline without rebuilding those bugs.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ce.benchmark import load_runs, record_is_complete, run_record_path  # noqa: E402
from ce.protocol import load_protocol  # noqa: E402
from scripts.vertex_amendment import (  # noqa: E402
    assert_config_matches_protocol, assert_treatment_unchanged,
)


def normalized_json_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


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


def completed_baseline_cache(results_dir: Path, parent_protocol: dict,
                             parent_sha: str, dataset_dir: Path) -> tuple[dict, dict]:
    """Return bugs with a complete, clean baseline-certified matched row."""
    manifest_path = results_dir / ".experiment.json"
    if not results_dir.is_dir() or not manifest_path.is_file():
        raise ValueError(f"Cannot reuse baseline evidence; missing {manifest_path}")
    source_config = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert_config_matches_protocol(source_config, parent_protocol, parent_sha)

    identities = set()
    bugs = defaultdict(list)
    for record in load_runs(str(results_dir)):
        if record.get("notes", {}).get("experiment_config") != source_config:
            raise ValueError("A result record has a different config from its experiment manifest")
        try:
            trial = int(record.get("trial", 1))
        except (TypeError, ValueError):
            continue
        identity = (str(record.get("bug_id")), record.get("condition"), trial)
        if identity in identities:
            raise ValueError(f"Duplicate result identity in reuse directory: {identity}")
        identities.add(identity)
        path = Path(run_record_path(
            str(results_dir), str(record["bug_id"]), record["condition"],
            allow_promotion=record.get("notes", {}).get("allow_promotion", True),
            trial=trial,
        ))
        if not record_is_complete(str(path)):
            continue
        if not record.get("notes", {}).get("baseline_validation", {}).get(
                "semantic_failure_confirmed"):
            continue
        bugs[str(record["bug_id"])].append((identity, record))

    source_records = (source_config.get("dataset_identity") or {}).get("records") or {}
    cache = {}
    for bug_id in parent_protocol["bugs"]:
        expected = {(bug_id, condition, trial)
                    for condition in parent_protocol["conditions"]
                    for trial in range(1, parent_protocol["repeat"] + 1)}
        rows = bugs.get(bug_id, [])
        found = {identity for identity, _ in rows}
        if found != expected:
            continue
        path = dataset_dir / f"{bug_id}.json"
        if not path.is_file():
            continue
        current_hash = normalized_json_hash(path)
        if source_records.get(bug_id) != current_hash:
            raise ValueError(f"Dataset record changed since completed runs for bug {bug_id}")
        cache[bug_id] = {
            "source": str(results_dir),
            "cells": len(rows),
            "dataset_sha256": current_hash,
        }
    return cache, source_config


def validate_one(rx, bug_id: str, build_jobs: int) -> dict:
    env = None
    outcome = {"bug_id": bug_id, "eligible": False, "method": "fresh_baseline_check"}
    try:
        env = rx.Env(bug_id, "2000-01-01Z", max_build_jobs=build_jobs)
        if env.get_bug_type() != "miscompilation":
            outcome["reason"] = f"unsupported bug_type: {env.get_bug_type()}"
            return outcome
        if not env.is_single_func_fix():
            outcome["reason"] = "not a single-function fix"
            return outcome
        env.use_knowledge("alive2", env.knowledge_cutoff)
        env.reset()
        ok, log = env.check_fast()
        outcome["build_count"] = env.build_count
        outcome["build_failure_count"] = env.build_failure_count
        outcome["baseline_validation"] = dict(env.last_validation)
        if env.build_failure_count:
            outcome["reason"] = "unpatched LLVM build failed"
            outcome["log_tail"] = str(log)[-4000:]
            return outcome
        if ok:
            outcome["reason"] = "base commit did not reproduce the bug"
            return outcome
        try:
            outcome["semantic_baseline"] = rx.semantic_baseline(log)
        except rx.BaselineBuildError as error:
            outcome["reason"] = str(error)
            return outcome
        outcome["eligible"] = True
        outcome["reason"] = "clean unpatched Alive2 semantic violation"
        return outcome
    except Exception as error:  # an infrastructure fault is an ineligible candidate
        outcome["reason"] = f"{type(error).__name__}: {error}"
        return outcome
    finally:
        if env is not None:
            try:
                env.reset()
            except Exception as error:  # preserve the original result and expose cleanup failure
                outcome["cleanup_error"] = f"{type(error).__name__}: {error}"
                outcome["eligible"] = False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--protocol", default="data/repair_v3c_vertex_protocol.json")
    parser.add_argument("--source-protocol", default="data/repair_v3b_vertex_protocol.json")
    parser.add_argument("--reuse-completed", default="results-vertex-v3b")
    parser.add_argument("--out-protocol", default="run-provenance/repair_v3c_vertex_resolved.json")
    parser.add_argument("--report", default="run-provenance/repair_v3c_preflight.json")
    parser.add_argument("--build-jobs", type=int, default=8)
    args = parser.parse_args(argv)
    if args.build_jobs < 1:
        parser.error("--build-jobs must be positive")

    base_path = Path(args.protocol)
    parent_path = Path(args.source_protocol)
    out_protocol = Path(args.out_protocol)
    report_path = Path(args.report)
    if out_protocol.exists() or report_path.exists():
        parser.error("Preflight output already exists; preserve it and choose fresh output paths")

    try:
        base_protocol, base_sha = load_protocol(str(base_path))
        parent_protocol, parent_sha = load_protocol(str(parent_path))
        assert_treatment_unchanged(parent_protocol, base_protocol)
        if base_protocol.get("amends_protocol_id") != parent_protocol["id"]:
            raise ValueError("The revised protocol does not name the supplied parent protocol")
        if "cohort_plan" not in base_protocol:
            raise ValueError("The protocol has no predeclared cohort_plan")
        candidate_lists = []
        primary_order = []
        cohort_names = set()
        for cohort in base_protocol["cohort_plan"].get("cohorts", []):
            if cohort.get("name") in cohort_names:
                raise ValueError("Duplicate cohort name in predeclared plan")
            cohort_names.add(cohort.get("name"))
            primary_order.extend(cohort.get("primary", []))
            candidate_lists.extend(cohort.get("primary", []) + cohort.get("reserves", []))
        if primary_order != base_protocol["bugs"]:
            raise ValueError("Protocol bugs must match the cohort primary order")
        if len(candidate_lists) != len(set(candidate_lists)):
            raise ValueError("Predeclared candidate IDs must be unique across cohorts")
        dataset_dir = Path(os.environ["LAB_DATASET_DIR"]).resolve()
        for key in ("LAB_LLVM_DIR", "LAB_LLVM_BUILD_DIR", "LAB_LLVM_ALIVE_TV"):
            if not os.environ.get(key):
                raise ValueError(f"{key} is not set; source the native runtime first")
        import examples.repair_experiment as rx
        rx._import_benchmark()
        cached, source_config = completed_baseline_cache(
            Path(args.reuse_completed), parent_protocol, parent_sha, dataset_dir)
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))

    report = {
        "schema": 1,
        "action": "baseline eligibility only; zero model generations",
        "started_on": date.today().isoformat(),
        "base_protocol": {"path": str(base_path), "id": base_protocol["id"], "sha256": base_sha},
        "parent_protocol": {"path": str(parent_path), "id": parent_protocol["id"], "sha256": parent_sha},
        "reused_completed_baselines_from": str(Path(args.reuse_completed)),
        "reused_baseline_bugs": sorted(cached),
        "candidate_results": [],
        "selected_by_cohort": {},
        "eligible_bugs": [],
        "status": "running",
    }
    available = dict(cached)
    try:
        for cohort in base_protocol["cohort_plan"]["cohorts"]:
            name = cohort["name"]
            target_size = cohort["target_size"]
            primary = list(cohort["primary"])
            reserves = list(cohort["reserves"])
            if len(primary) != target_size or len(set(primary + reserves)) != len(primary + reserves):
                raise ValueError(f"Malformed candidate order for cohort {name}")
            if any(bug not in base_protocol["bugs"] for bug in primary):
                raise ValueError(f"Primary cohort {name} differs from the protocol bug list")

            positions: list[str | None] = [None] * target_size
            for index, bug_id in enumerate(primary):
                if bug_id in available:
                    positions[index] = bug_id
                    report["candidate_results"].append({
                        "bug_id": bug_id, "eligible": True, "method": "reused_completed_baseline",
                        "evidence": available[bug_id],
                    })
                    continue
                outcome = validate_one(rx, bug_id, args.build_jobs)
                report["candidate_results"].append(outcome)
                if outcome["eligible"]:
                    positions[index] = bug_id
                    available[bug_id] = {"method": "fresh_baseline_check"}
                print(f"[{name}] {bug_id}: {'ELIGIBLE' if outcome['eligible'] else 'EXCLUDE'} — {outcome['reason']}", flush=True)

            reserve_iter = iter(reserves)
            for index, selected in enumerate(positions):
                if selected is not None:
                    continue
                while True:
                    try:
                        candidate = next(reserve_iter)
                    except StopIteration:
                        raise ValueError(f"Cohort {name} exhausted its predeclared reserves; "
                                         "do not start generation with fewer than the target size")
                    outcome = validate_one(rx, candidate, args.build_jobs)
                    outcome["cohort"] = name
                    outcome["reserve_for_primary"] = primary[index]
                    report["candidate_results"].append(outcome)
                    print(f"[{name} reserve for {primary[index]}] {candidate}: "
                          f"{'ELIGIBLE' if outcome['eligible'] else 'EXCLUDE'} — {outcome['reason']}",
                          flush=True)
                    if outcome["eligible"]:
                        positions[index] = candidate
                        break

            report["selected_by_cohort"][name] = positions

        selected_bugs = [bug for cohort in base_protocol["cohort_plan"]["cohorts"]
                         for bug in report["selected_by_cohort"][cohort["name"]]]
        if len(selected_bugs) != len(base_protocol["bugs"]) or len(set(selected_bugs)) != len(selected_bugs):
            raise ValueError("Resolved cohort is not exactly the requested unique 16-bug sample")
        resolved = dict(base_protocol)
        resolved["bugs"] = selected_bugs
        selection_digest = hashlib.sha256("\n".join(selected_bugs).encode()).hexdigest()[:10]
        resolved["id"] = f"{base_protocol['id']}-resolved-{selection_digest}"
        resolved["written"] = date.today().isoformat()
        resolved["status"] = "Frozen after a no-model verifier and reproducer preflight; see paired preflight report."
        resolved["rationale"] += " The resolved cohort is the first 8 clean-baseline candidates in each predeclared stratum."
        resolved_sha = load_protocol_json_hash(resolved)
        report["resolved_protocol"] = {
            "path": str(out_protocol), "id": resolved["id"], "sha256": resolved_sha,
        }
        report["eligible_bugs"] = selected_bugs
        report["status"] = "passed"
        atomic_new_json(out_protocol, resolved)
        print(f"Wrote frozen eligible protocol: {out_protocol}")
        print(f"Eligible cohort: {len(selected_bugs)} bugs, "
              f"{len(selected_bugs) * len(resolved['conditions']) * resolved['repeat']} cells")
    except Exception as error:  # noqa: BLE001 - report failures before returning
        report["status"] = "failed"
        report["error"] = f"{type(error).__name__}: {error}"
        print(report["error"], file=sys.stderr)
        atomic_new_json(report_path, report)
        return 1

    atomic_new_json(report_path, report)
    print(f"Preflight report: {report_path}")
    return 0


def load_protocol_json_hash(value: dict) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
