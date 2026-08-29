#!/usr/bin/env python3
"""Pick a good first bug for the Blocker 1 bootstrap (docs/IMPLEMENTATION.md, §9).

We have never run the repair loop end-to-end: `/workspace/llvm-build` is empty
and nobody has built `opt` for any `base_commit` yet. Before scaling to the
full dataset, we want ONE bug that exercises the whole pipeline (build, Alive2
check, repair loop) with as little incidental complexity as possible.

A bug qualifies for `examples/repair_experiment.py` at all only if:
  * bug_type == "miscompilation" (crash/hang bugs skip the counterexample
    conditions entirely - see repair_experiment.py's `repair()`)
  * properties.is_single_func_fix is true (multi-function fixes are skipped)
  * it is checked by Alive2, not just `lli` (lli-only bugs have
    `lli_expected_out` set on every subtest instead of going through
    alive2_check - see llvm_helper.verify_dispatch)

Among qualifying bugs we rank by simplicity: fewer hint lines, fewer/smaller
reproducers, a single lit test directory, and a single hinted component. None
of this is about picking an "easy" bug to fix - the point of Blocker 1 is to
prove the build+verify+repair machinery works, not to get a high score.

Usage:
    python3 scripts/select_bootstrap_bug.py            # top 10 candidates
    python3 scripts/select_bootstrap_bug.py --top 1     # just the pick
"""

from __future__ import annotations

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
DATASET_DIR = os.path.join(_HERE, os.pardir, "llvm-apr-benchmark", "dataset")


def load(bug_id: str) -> dict:
    with open(os.path.join(DATASET_DIR, f"{bug_id}.json"), encoding="utf-8") as f:
        return json.load(f)


def is_alive2_checked(data: dict) -> bool:
    for test in data.get("tests", []):
        for subtest in test.get("tests", []):
            if subtest.get("lli_expected_out") is not None:
                return False
    return True


def hint_line_count(data: dict) -> int:
    lineno = data.get("hints", {}).get("bug_location_lineno") or {}
    total = 0
    for ranges in lineno.values():
        for lo, hi in ranges:
            total += hi - lo + 1
    return total


def reproducer_size(data: dict) -> int:
    total = 0
    for test in data.get("tests", []):
        for subtest in test.get("tests", []):
            total += len(subtest.get("test_body", "").splitlines())
    return total


def score(bug_id: str, data: dict) -> tuple:
    """Lower is simpler. Sort key, not a displayed metric."""
    lit_dirs = len(data.get("lit_test_dir") or [])
    components = len(data.get("hints", {}).get("components") or [])
    return (
        hint_line_count(data),
        reproducer_size(data),
        lit_dirs,
        components,
        bug_id,
    )


def find_candidates() -> list[tuple[str, dict]]:
    candidates = []
    for name in sorted(os.listdir(DATASET_DIR)):
        if not name.endswith(".json"):
            continue
        bug_id = name.removesuffix(".json")
        data = load(bug_id)
        if data.get("bug_type") != "miscompilation":
            continue
        if not data.get("properties", {}).get("is_single_func_fix"):
            continue
        if not is_alive2_checked(data):
            continue
        candidates.append((bug_id, data))
    return candidates


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--top", type=int, default=10)
    args = parser.parse_args(argv)

    candidates = find_candidates()
    if not candidates:
        print("no qualifying bug found", file=sys.stderr)
        return 1
    candidates.sort(key=lambda kv: score(*kv))

    print(f"{len(candidates)} qualifying bugs "
          f"(miscompilation, single-func fix, Alive2-checked)")
    print(f"{'bug_id':>8}  {'hint_lines':>10}  {'repro_lines':>11}  "
          f"{'lit_dirs':>8}  base_commit")
    for bug_id, data in candidates[: args.top]:
        print(f"{bug_id:>8}  {hint_line_count(data):>10}  "
              f"{reproducer_size(data):>11}  "
              f"{len(data.get('lit_test_dir') or []):>8}  "
              f"{data['base_commit'][:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
