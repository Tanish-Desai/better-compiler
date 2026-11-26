#!/usr/bin/env python3
"""Measure the effect of trace guidance on archived LLVM witness pairs."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ce.alive import AliveRun, FunctionResult, run_alive_tv
from ce.irmodel import measure_pair
from ce.oracle import Oracle, Violation
from ce.reduce_iraware import reduce_iraware
from ce.reduce_llvmreduce import reduce_llvmreduce
from scripts import compare_saved_witnesses as replay

CAP = 32
PREFIX = 8
TIMEOUT = 120
METHODS = ("trace-guided", "no-trace-seeds-or-path", "relax-if-stalled", "llvmreduce", "llvmreduce-ir-passes")
IR_PASSES = ("instructions", "basic-blocks", "simplify-cfg", "arguments", "operands-to-args", "instruction-flags")


def read_witnesses(report: dict) -> list[replay.Witness]:
    if not report["complete"]:
        raise ValueError("use a complete fresh witness report")
    witnesses = []
    for tier in report["protocol"]["tiers"]:
        count = 0
        for candidate in tier["candidates"]:
            bug = candidate["bug_id"]
            entry = report["entries"].get(bug, {})
            if "comparison" not in entry:
                continue
            witness = replay.Witness(**entry["witness"])
            if witness.bug_id != bug or witness.identity != entry["comparison"]["witness_sha256"]:
                raise ValueError(f"witness identity mismatch for {bug}")
            witnesses.append(witness)
            count += 1
        if count != tier["target"]:
            raise ValueError(f"incomplete {tier['name']} tier")
    if len({w.identity for w in witnesses}) != report["protocol"]["eligible_total"]:
        raise ValueError("the archive must contain the full set of distinct witnesses")
    return witnesses


def clean_violation(run: AliveRun, target: Violation | None = None) -> FunctionResult | None:
    if run.tool_error or run.num_incorrect != 1 or run.num_errors or run.num_failed_to_prove:
        return None
    violation = run.first_violation()
    if violation is None or not violation.name or not violation.error_class:
        return None
    if target is not None and (violation.name, violation.error_class) != (target.function, target.error_class):
        return None
    return violation


def replay_case(witness: replay.Witness, archived_target: Violation, alive_tv: str, llvm_reduce: str) -> dict:
    pair = witness.src, witness.tgt
    original = run_alive_tv(*pair, witness.extra_args, alive_tv=alive_tv, timeout=TIMEOUT)
    violation = clean_violation(original, archived_target)
    if violation is None:
        raise ValueError(f"{witness.bug_id}: archived witness could not be re-established: {original.raw[-1500:]}")
    target = Violation.from_result(violation)
    original_size = measure_pair(*pair)["instructions"]
    if not original_size:
        raise ValueError(f"{witness.bug_id}: empty witness")
    rows = {}
    for method in METHODS:
        oracle = Oracle(target, witness.extra_args, alive_tv=alive_tv, timeout=TIMEOUT,
                        max_calls=PREFIX if method == "relax-if-stalled" else CAP)
        start = time.perf_counter()
        retried = False
        prefix_calls = None
        if method.startswith("llvmreduce"):
            reduction = reduce_llvmreduce(*pair, oracle, llvm_reduce=llvm_reduce,
                                          delta_passes=IR_PASSES if method == "llvmreduce-ir-passes" else ())
        else:
            reduction = reduce_iraware(*pair, oracle, violation,
                                       use_trace_guidance=method != "no-trace-seeds-or-path")
            if method == "relax-if-stalled":
                prefix_calls = oracle.calls
                if reduction.error or reduction.size_after["instructions"] >= original_size:
                    retried = True
                    oracle.max_calls = CAP
                    second = reduce_iraware(*pair, oracle, violation, use_trace_guidance=False)
                    if not second.error and (reduction.error or
                            second.size_after["instructions"] < reduction.size_after["instructions"]):
                        reduction = second
        checked = run_alive_tv(reduction.src, reduction.tgt, witness.extra_args,
                               alive_tv=alive_tv, timeout=TIMEOUT)
        verified = clean_violation(checked, target) is not None
        if oracle.calls > CAP:
            raise ValueError(f"{witness.bug_id}/{method}: search exceeded its call ceiling")
        rows[method] = {"verified": verified, "error": reduction.error,
                        "instructions": reduction.size_after["instructions"],
                        "removed_fraction": (original_size - reduction.size_after["instructions"]) / original_size
                        if verified and not reduction.error else 0.0,
                        "search_calls": oracle.calls, "postcheck_calls": 1,
                        "wall_seconds": round(time.perf_counter() - start, 6), "prefix_calls": prefix_calls,
                        "retried_without_guidance": retried, "passes": reduction.passes_applied,
                        "src": reduction.src, "tgt": reduction.tgt, "postcheck": replay.run_details(checked)}
    return {"bug_id": witness.bug_id, "witness_sha256": witness.identity,
            "original_instructions": original_size, "methods": rows}


def summarize(report: dict) -> None:
    cases = list(report["cases"].values())
    print(f"{len(cases)}/{report['planned_witnesses']} witnesses complete")
    if not cases:
        return
    for method in METHODS:
        rows = [case["methods"][method] for case in cases]
        print(f"{method}: {sum(r['verified'] and not r['error'] for r in rows)}/{len(rows)} verified outputs, "
              f"mean removed {statistics.mean(r['removed_fraction'] for r in rows):.1%}, "
              f"median search + final check calls {statistics.median(r['search_calls'] + 1 for r in rows):g}")
    for first, second in (("trace-guided", "no-trace-seeds-or-path"), ("relax-if-stalled", "trace-guided"),
                          ("relax-if-stalled", "llvmreduce"), ("relax-if-stalled", "llvmreduce-ir-passes")):
        differences = [c["methods"][first]["removed_fraction"] - c["methods"][second]["removed_fraction"] for c in cases]
        wins = sum(d > 0 for d in differences)
        losses = sum(d < 0 for d in differences)
        print(f"{first} vs {second}: {wins} smaller, {len(cases) - wins - losses} tied, {losses} larger. "
              f"Mean gain {100 * statistics.mean(differences):+.2f} percentage points.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("witness-comparison-fresh-b32/report.json"))
    parser.add_argument("--out", type=Path, default=Path("witness-comparison-guidance-b32/report.json"))
    parser.add_argument("--alive-tv")
    parser.add_argument("--llvm-reduce")
    parser.add_argument("--jobs", type=int, default=1, help="independent witness replays to run concurrently")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    try:
        if args.status:
            summarize(json.loads(args.out.read_bytes()))
            return 0
        raw = args.input.read_bytes()
        archive = json.loads(raw)
        witnesses = read_witnesses(archive)
        if args.plan:
            print(f"{len(witnesses)} archived witnesses, {len(METHODS)} methods, {CAP} search calls per method")
            print(", ".join(witness.bug_id for witness in witnesses))
            return 0
        alive_tv = replay.binary_path(args.alive_tv, "alive-tv")
        llvm_reduce = replay.binary_path(args.llvm_reduce, "llvm-reduce")
        passes = subprocess.check_output([llvm_reduce, "--print-delta-passes"], text=True, timeout=15)
        if set(IR_PASSES) - set(passes.split()):
            raise ValueError("llvm-reduce lacks one or more required IR passes")
        binaries = {"alive_tv": replay.binary_identity(alive_tv), "llvm_reduce": replay.binary_identity(llvm_reduce)}
        source = replay.source_identity()
        source[Path(__file__).name] = replay.digest(Path(__file__).read_bytes())
        identity = {"input_sha256": replay.digest(raw), "source": source,
                    "binaries": {name: detail["sha256"] for name, detail in binaries.items()},
                    "cap": CAP, "prefix": PREFIX, "timeout": TIMEOUT, "ir_passes": list(IR_PASSES),
                    "jobs": args.jobs}
        if args.out.exists():
            report = json.loads(args.out.read_bytes())
            if report["identity"] != identity:
                raise ValueError("checkpoint inputs, source, or binaries differ")
        else:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            report = {"identity": identity, "binaries": binaries, "planned_witnesses": len(witnesses), "cases": {}}
            replay.write_report(args.out, report)
        pending = [witness for witness in witnesses if witness.bug_id not in report["cases"]]
        errors = report.setdefault("errors", {})
        with ThreadPoolExecutor(max_workers=args.jobs) as executor:
            futures = {}
            for witness in pending:
                bug = witness.bug_id
                saved = archive["entries"][bug]["comparison"]["establishment"]["violation"]
                future = executor.submit(replay_case, witness, Violation(**saved), alive_tv, llvm_reduce)
                futures[future] = bug
            for future in as_completed(futures):
                bug = futures[future]
                try:
                    report["cases"][bug] = future.result()
                    errors.pop(bug, None)
                    print(f"{len(report['cases'])}/{len(witnesses)}. Saved {bug}", flush=True)
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    errors[bug] = str(error)
                    print(f"Could not replay {bug}: {error}", file=sys.stderr, flush=True)
                replay.write_report(args.out, report)
        summarize(report)
        return int(bool(errors))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
