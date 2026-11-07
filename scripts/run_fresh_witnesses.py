#!/usr/bin/env python3
"""Collect real unpatched LLVM witnesses and run the frozen CPU comparison."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
import json
import math
import os
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ce.alive import parse_alive_output, parse_extra_args  # noqa: E402
from ce.benchmark import first_failed_test, is_alive2_log  # noqa: E402
from examples import repair_experiment as repair  # noqa: E402
from scripts import compare_saved_witnesses as replay  # noqa: E402
from scripts.preflight_vertex_protocol import normalized_json_hash  # noqa: E402

PROTOCOL = ROOT / "data/fresh_witness_b32_protocol.json"


def capture(candidate: dict, jobs: int) -> tuple[dict, replay.Witness | None]:
    bug = candidate["bug_id"]
    env = repair.Env(bug, "2000-01-01Z", max_build_jobs=jobs)
    if env.base_commit != candidate["base_commit"]:
        raise ValueError(f"{bug}: dataset base commit differs from protocol")
    env.reset()
    started = time.perf_counter()
    env.verify_head()
    built, log = repair.llvm_helper.build(jobs, env.additional_cmake_args, targets=("opt",))
    if not built:
        raise RuntimeError(f"{bug}: unpatched build failed. Fix the build and resume.\n{str(log)[-4000:]}")
    passed, log = repair.llvm_helper.verify_test_group(False, env.data["tests"], env.bug_type)
    details = {"baseline_seconds": round(time.perf_counter() - started, 3),
               "validation": {"stage": "reproducer", "passed": passed}}
    if passed:
        return {**details, "reason": "base commit passed the reproducer"}, None
    failed = first_failed_test(log)
    inner = failed.get("log") if failed else None
    if not is_alive2_log(inner):
        return {**details, "reason": "first failing reproducer did not return an Alive2 pair",
                "diagnostic_tail": str(failed or log)[-4000:]}, None

    parsed = parse_alive_output(inner["log"])
    violation = parsed.first_violation()
    details["initial_verifier"] = replay.run_details(parsed)
    if (parsed.tool_error or parsed.num_errors or parsed.num_failed_to_prove
            or parsed.num_incorrect != 1 or violation is None
            or not violation.name or not violation.error_class):
        return {**details, "reason": "first failing pair did not establish one clean named violation"}, None
    args = tuple(parse_extra_args(inner.get("additional_args") or ""))
    witness = replay.Witness(bug, inner["src"], inner["tgt"], args, failed["name"],
                             [candidate["base_commit"]], [failed["file"]], candidate["dataset_sha256"])
    return details, witness


def score(case: dict, method: str) -> tuple[int, bool]:
    original = case["original_size"]["instructions"]
    row = next(row for row in case["methods"] if row["method"] == method)
    post = row.get("postcheck", {})
    clean = (row.get("same_violation_fresh") is True and post.get("num_incorrect", 0) == 1
             and post.get("num_errors") == 0 and post.get("num_failed_to_prove") == 0)
    if clean and row["status"] in ("ok", "fallback"):
        return original - row["displayed_size"]["instructions"], True
    return 0, False


def paired_summary(cases: list[dict], methods: list[str], *, minimum_mean_gain: float | None = None) -> dict:
    hybrid, baseline = methods
    wins = ties = losses = 0
    fractions = []
    valid = Counter()
    for case in cases:
        better, better_valid = score(case, hybrid)
        worse, worse_valid = score(case, baseline)
        valid[hybrid] += better_valid
        valid[baseline] += worse_valid
        wins += better > worse
        ties += better == worse
        losses += better < worse
        fractions.append((better - worse) / case["original_size"]["instructions"])
    summary = {"methods": methods, "n": len(cases), "wins": wins, "ties": ties, "losses": losses,
               "verified_outputs": dict(valid), "mean_paired_difference": statistics.mean(fractions),
               "median_paired_difference": statistics.median(fractions)}
    if minimum_mean_gain is not None:
        n = wins + losses
        p = min(1.0, 2 * sum(math.comb(n, k) for k in range(min(wins, losses) + 1)) / 2**n)
        summary.update(p_two_sided=p, alpha=0.05, minimum_mean_gain=minimum_mean_gain,
                       improvement=(wins > losses and p < 0.05
                                    and summary["mean_paired_difference"] >= minimum_mean_gain))
    return summary


def progress(report: dict) -> None:
    for tier in report["protocol"]["tiers"]:
        entries = [report["entries"].get(candidate["bug_id"], {}) for candidate in tier["candidates"]]
        compared = sum("comparison" in entry for entry in entries)
        print(f"{tier['name']}: {compared}/{tier['target']} compared, {sum(bool(e) for e in entries)} screened")
    if report["complete"]:
        print(json.dumps(report["analysis"], indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("witness-comparison-fresh-b32"))
    parser.add_argument("--build-jobs", type=int, default=8)
    parser.add_argument("--llvm-reduce", default=replay.default_llvm_reduce())
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="list the frozen cohort without running tools")
    mode.add_argument("--status", action="store_true", help="read checkpoint coverage and final analysis")
    args = parser.parse_args()
    if args.build_jobs < 1:
        parser.error("--build-jobs must be positive")
    report_path = args.out / "report.json"
    plan = json.loads(PROTOCOL.read_bytes())
    protocol_sha = normalized_json_hash(PROTOCOL)
    settings = plan["comparison"]
    try:
        if args.plan:
            print(f"{plan['id']}: target {plan['eligible_total']} eligible witnesses; protocol {protocol_sha}")
            for tier in plan["tiers"]:
                print(tier["name"], tier["target"], "of", ", ".join(c["bug_id"] for c in tier["candidates"]))
            return 0
        if args.status:
            progress(json.loads(report_path.read_bytes()))
            return 0

        repair._import_benchmark()
        dataset = Path(os.environ["LAB_DATASET_DIR"])
        for tier in plan["tiers"]:
            for candidate in tier["candidates"]:
                if normalized_json_hash(dataset / f"{candidate['bug_id']}.json") != candidate["dataset_sha256"]:
                    raise ValueError(f"dataset hash mismatch for {candidate['bug_id']}")
        alive_tv = replay.binary_path(os.environ.get("LAB_LLVM_ALIVE_TV"), "alive-tv")
        llvm_reduce = replay.binary_path(args.llvm_reduce, "llvm-reduce")
        binaries = {"alive_tv": replay.binary_identity(alive_tv), "llvm_reduce": replay.binary_identity(llvm_reduce)}
        source = replay.source_identity()
        for path in (Path(__file__), ROOT / "examples/repair_experiment.py",
                     dataset.parent / "scripts/lab_env.py", dataset.parent / "scripts/llvm_helper.py"):
            source[path.name] = normalized_json_hash(path)
        identity = {"protocol_sha256": protocol_sha, "implementation_sha256": source,
                    "binaries_sha256": {key: value["sha256"] for key, value in binaries.items()}}
        if report_path.exists():
            report = json.loads(report_path.read_bytes())
            if report["identity"] != identity:
                raise ValueError("checkpoint protocol, code, or verifier binaries differ")
        else:
            report = {"identity": identity, "protocol": plan, "binaries": binaries, "entries": {}, "complete": False}
            args.out.mkdir(parents=True, exist_ok=True)
            replay.write_report(report_path, report)

        if report["complete"]:
            progress(report)
            return 0
        entries = report["entries"]
        cases = []
        seen_pairs = set()
        with replay.llvm_reduce_override(llvm_reduce):
            for tier in plan["tiers"]:
                count = 0
                for candidate in tier["candidates"]:
                    bug = candidate["bug_id"]
                    if bug not in entries:
                        print(f"[{tier['name']} {count}/{tier['target']}] {bug}: building and checking unpatched LLVM", flush=True)
                        entry, witness = capture(candidate, args.build_jobs)
                        if witness is not None and witness.identity in seen_pairs:
                            entry["reason"] = "duplicate of an earlier eligible witness"
                        elif witness is not None:
                            entry["witness"] = asdict(witness)
                        entries[bug] = entry
                        replay.write_report(report_path, report)
                    entry = entries[bug]
                    if "witness" not in entry:
                        print(f"{bug}: excluded during eligibility screening ({entry['reason']})", flush=True)
                        continue
                    witness = replay.Witness(**entry["witness"])
                    seen_pairs.add(witness.identity)
                    if "comparison" not in entry:
                        print(f"{bug}: replaying reducers", flush=True)
                        case = replay.compare_witness(
                            witness, alive_tv=alive_tv, budget=settings["budget"], timeout=settings["timeout"],
                            hybrid_prefix_calls=settings["hybrid_prefix_calls"], balanced_llvmreduce=True,
                            instruction_seed_only=settings["instruction_seed_only"],
                        )
                        if case["status"] != "completed" or any(
                            row["status"] == "error" or row.get("candidate_calls", 0) > settings["budget"]
                            for row in case["methods"]
                        ):
                            entry["interrupted_comparison"] = case
                            replay.write_report(report_path, report)
                            raise RuntimeError(f"{bug}: incomplete replay. Inspect the checkpoint and resume.")
                        entry["comparison"] = case
                        replay.write_report(report_path, report)
                    cases.append(entry["comparison"])
                    count += 1
                    print(f"[{tier['name']} {count}/{tier['target']}] {bug}: saved", flush=True)
                    if count == tier["target"]:
                        break
                if count != tier["target"]:
                    progress(report)
                    raise RuntimeError(f"{tier['name']}: fixed reserves exhausted. Report coverage as incomplete.")

        report["analysis"] = {"primary": paired_summary(
            cases, settings["primary"], minimum_mean_gain=settings["minimum_mean_gain"]),
            "balanced_secondary": paired_summary(cases, settings["balanced_secondary"])}
        report["complete"] = True
        replay.write_report(report_path, report)
        progress(report)
        return 0
    except (KeyError, OSError, RuntimeError, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
