#!/usr/bin/env python3
"""Compare paired and focused LLVM reduction on a frozen, unused witness cohort."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ce.alive import run_alive_tv  # noqa: E402
from ce.irmodel import measure_pair  # noqa: E402
from ce.oracle import Oracle, Violation  # noqa: E402
from ce.reduce_iraware import reduce_iraware  # noqa: E402
from ce.reduce_llvmreduce import reduce_llvmreduce  # noqa: E402
from scripts import compare_saved_witnesses as replay  # noqa: E402
from scripts import replay_reducer_guidance as guidance  # noqa: E402
from scripts import run_fresh_witnesses as fresh  # noqa: E402
from scripts.preflight_vertex_protocol import normalized_json_hash  # noqa: E402

PROTOCOL = ROOT / "data/fresh_witness_frontier_protocol.json"
METHODS = ("paired", "focused-llvmreduce")


def run_case(witness, captured_violation, settings, alive_tv, llvm_reduce):
    pair = witness.src, witness.tgt
    timeout = settings["timeout"]
    original = run_alive_tv(*pair, witness.extra_args, alive_tv=alive_tv, timeout=timeout)
    violation = guidance.clean_violation(original, captured_violation)
    if violation is None:
        raise RuntimeError(f"{witness.bug_id}: could not re-establish the captured violation\n{original.raw[-1500:]}")
    original_instructions = measure_pair(*pair)["instructions"]
    if not original_instructions:
        raise ValueError(f"{witness.bug_id}: empty original witness")

    target = Violation.from_result(violation)
    budgets = {}
    for budget in settings["budgets"]:
        methods = {}
        for method in METHODS:
            oracle = Oracle(target, witness.extra_args, strictness=settings["oracle_strictness"],
                            alive_tv=alive_tv, timeout=timeout, max_calls=budget)
            started = time.perf_counter()
            if method == "paired":
                reduction = reduce_iraware(*pair, oracle, violation, use_trace_guidance=False,
                                           allow_promotion=settings["operand_promotion"])
            else:
                reduction = reduce_llvmreduce(*pair, oracle, llvm_reduce=llvm_reduce,
                                              delta_passes=tuple(settings["llvm_delta_passes"]))
            if oracle.calls > budget:
                raise RuntimeError(f"{witness.bug_id}/{method}/{budget}: reducer exceeded its call ceiling")
            checked = run_alive_tv(reduction.src, reduction.tgt, witness.extra_args,
                                   alive_tv=alive_tv, timeout=timeout)
            verified = guidance.clean_violation(checked, target) is not None
            instructions = reduction.size_after["instructions"]
            methods[method] = {
                "verified": verified,
                "error": reduction.error,
                "instructions": instructions,
                "removed_fraction": (original_instructions - instructions) / original_instructions
                if verified and not reduction.error else 0.0,
                "reducer_calls": oracle.calls,
                "postcheck_calls": 1,
                "seconds": round(time.perf_counter() - started, 6),
                "oracle": oracle.stats(),
                "src": reduction.src,
                "tgt": reduction.tgt,
                "postcheck": replay.run_details(checked),
            }
        budgets[str(budget)] = methods
    return {"witness_sha256": witness.identity, "original_instructions": original_instructions,
            "establishment": replay.run_details(original), "budgets": budgets}


def analyze(cases, settings):
    analysis = {}
    for budget in settings["budgets"]:
        rows = [case["budgets"][str(budget)] for case in cases]
        differences = [row[METHODS[0]]["removed_fraction"] - row[METHODS[1]]["removed_fraction"] for row in rows]
        wins = sum(delta > 0 for delta in differences)
        losses = sum(delta < 0 for delta in differences)
        result = {"methods": {}, "paired_mean_gain": statistics.mean(differences),
                  "smaller_tied_larger": [wins, len(cases) - wins - losses, losses]}
        for method in METHODS:
            results = [row[method] for row in rows]
            result["methods"][method] = {
                "verified_outputs": sum(row["verified"] and not row["error"] for row in results),
                "mean_removed_fraction": statistics.mean(row["removed_fraction"] for row in results),
                "median_reducer_and_postcheck_calls": statistics.median(
                    row["reducer_calls"] + row["postcheck_calls"] for row in results
                ),
            }
        if budget == settings["primary_budget"]:
            discordant = wins + losses
            p = min(1.0, 2 * sum(math.comb(discordant, k) for k in range(min(wins, losses) + 1)) / 2**discordant)
            result.update(p_two_sided=p, improvement=(wins > losses and p < settings["alpha"]
                          and result["paired_mean_gain"] >= settings["minimum_mean_gain"]))
        analysis[str(budget)] = result
    return analysis


def progress(report):
    print(f"{len(report['cases'])}/{report['protocol']['eligible_total']} witnesses complete")
    if report["complete"]:
        print(json.dumps(report["analysis"], indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-jobs", type=int, default=8)
    parser.add_argument("--llvm-reduce", default=replay.default_llvm_reduce())
    parser.add_argument("--out", type=Path, default=Path("witness-comparison-frontier/report.json"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="list the frozen cohort and budgets without running tools")
    mode.add_argument("--status", action="store_true", help="read checkpoint coverage and final analysis")
    args = parser.parse_args()
    if args.build_jobs < 1:
        parser.error("--build-jobs must be positive")

    try:
        if args.status:
            progress(json.loads(args.out.read_bytes()))
            return 0
        plan = json.loads(PROTOCOL.read_bytes())
        settings = plan["comparison"]
        archive_bytes = (ROOT / plan["original_archive"]).read_bytes()
        if replay.digest(archive_bytes) != plan["original_archive_sha256"]:
            raise ValueError("original witness archive differs from the protocol")
        archive = json.loads(archive_bytes)
        original_witnesses = guidance.read_witnesses(archive)
        candidates = [candidate for tier in plan["tiers"] for candidate in tier["candidates"]]
        candidate_ids = {candidate["bug_id"] for candidate in candidates}
        excluded_ids = set(archive["entries"]) | set(archive["protocol"]["excluded_bug_ids"])
        if (candidate_ids & excluded_ids or len(candidate_ids) != len(candidates)
                or plan["eligible_total"] != sum(tier["target"] for tier in plan["tiers"])):
            raise ValueError("invalid cohort or overlap with the original evaluation")
        if args.plan:
            print(f"{plan['id']}: target {plan['eligible_total']} eligible witnesses from {len(candidates)} candidates")
            print(f"Reducer ceilings: {settings['budgets']}; primary: {settings['primary_budget']}")
            for tier in plan["tiers"]:
                print(tier["name"], tier["target"], "of", ", ".join(c["bug_id"] for c in tier["candidates"]))
            return 0

        fresh.repair._import_benchmark()
        dataset = Path(os.environ["LAB_DATASET_DIR"])
        for candidate in candidates:
            if normalized_json_hash(dataset / f"{candidate['bug_id']}.json") != candidate["dataset_sha256"]:
                raise ValueError(f"dataset hash mismatch for {candidate['bug_id']}")
        alive_tv = replay.binary_path(os.environ.get("LAB_LLVM_ALIVE_TV"), "alive-tv")
        llvm_reduce = replay.binary_path(args.llvm_reduce, "llvm-reduce")
        passes = subprocess.check_output([llvm_reduce, "--print-delta-passes"], text=True, timeout=15)
        if set(settings["llvm_delta_passes"]) - set(passes.split()):
            raise ValueError("llvm-reduce lacks a focused pass from the protocol")
        binaries = {"alive_tv": replay.binary_identity(alive_tv), "llvm_reduce": replay.binary_identity(llvm_reduce)}
        source = replay.source_identity()
        for path in (Path(__file__), Path(fresh.__file__), Path(guidance.__file__),
                     ROOT / "examples/repair_experiment.py", dataset.parent / "scripts/lab_env.py",
                     dataset.parent / "scripts/llvm_helper.py"):
            source[path.name] = normalized_json_hash(path)
        identity = {"protocol_sha256": normalized_json_hash(PROTOCOL), "source": source,
                    "binaries": {name: detail["sha256"] for name, detail in binaries.items()},
                    "build_jobs": args.build_jobs}
        if args.out.exists():
            report = json.loads(args.out.read_bytes())
            if identity != report["identity"]:
                raise ValueError("checkpoint protocol, source, binaries, or build concurrency differ")
        else:
            report = {"identity": identity, "protocol": plan, "binaries": binaries,
                      "entries": {}, "cases": {}, "complete": False}
            args.out.parent.mkdir(parents=True, exist_ok=True)
            replay.write_report(args.out, report)

        if report["complete"]:
            progress(report)
            return 0
        seen_pairs = {witness.identity for witness in original_witnesses}
        entries, cases = report["entries"], report["cases"]
        for tier in plan["tiers"]:
            count = 0
            for candidate in tier["candidates"]:
                bug = candidate["bug_id"]
                if bug not in entries:
                    print(f"[{tier['name']} {count}/{tier['target']}] {bug}: building unpatched opt and screening", flush=True)
                    entry, witness = fresh.capture(candidate, args.build_jobs)
                    if witness is not None:
                        if witness.identity in seen_pairs:
                            entry["reason"] = "duplicate of a previously observed witness"
                        else:
                            entry["witness"] = asdict(witness)
                    entries[bug] = entry
                    replay.write_report(args.out, report)
                entry = entries[bug]
                if "witness" not in entry:
                    print(f"{bug}: {entry['reason']}", flush=True)
                    continue
                witness = replay.Witness(**entry["witness"])
                seen_pairs.add(witness.identity)
                if bug not in cases:
                    print(f"{bug}: comparing reducers at {settings['budgets']} calls", flush=True)
                    captured = Violation(**entry["initial_verifier"]["violation"])
                    cases[bug] = run_case(witness, captured, settings, alive_tv, llvm_reduce)
                    replay.write_report(args.out, report)
                    print(f"{len(cases)}/{plan['eligible_total']}. Saved {bug}", flush=True)
                count += 1
                if count == tier["target"]:
                    break
            if count != tier["target"]:
                raise RuntimeError(f"{tier['name']}: fixed reserves exhausted ({count}/{tier['target']}); incomplete cohort")

        report["analysis"] = analyze(list(cases.values()), settings)
        report["complete"] = True
        replay.write_report(args.out, report)
        progress(report)
        return 0
    except (KeyError, OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
