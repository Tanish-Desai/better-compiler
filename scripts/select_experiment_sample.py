#!/usr/bin/env python3
"""Pick the stratified bug sample for the real experiment (Blocker 3).

docs/IMPLEMENTATION.md, Blocker 3: the dataset has 135 miscompilations Alive2
can check, but `repair_experiment.py` also skips anything that isn't a
single-function fix (see `repair()`'s `is_single_func_fix()` check) — so the
bugs actually usable by this experiment number **100**, not 135. Running all
100 x 6 conditions x k repeats, at up to `--max-iterations` LLVM rebuilds per
attempt, is thousands of builds at hours each. The fix the doc calls for is a
stratified subsample, picked and justified in advance rather than however far
a sweep happens to get before someone stops it.

STRATIFICATION
---------------
Two dimensions, in priority order:

1. **Complexity tier** (primary). Bugs are scored by the same simplicity
   metric as `select_bootstrap_bug.py` (hint-region size + reproducer size)
   and split into three equal-sized tiers: easy / medium / hard. This is the
   dimension that plausibly interacts with the research question — if
   IR-aware reduction's advantage only shows up on complex bugs, or only on
   simple ones, a sample skewed toward one tier would hide that.
2. **Component diversity** (secondary, within each tier). 45 of the 100
   qualifying bugs are InstCombine; a sample built by picking the N
   "simplest" bugs overall would mostly just be InstCombine bugs at every
   tier. Within each complexity tier, bugs are selected to maximize distinct
   `hints.components` before repeating any component.

Both steps are fully deterministic (score, then component, then bug_id as a
final tiebreak) — no RNG, so the sample is reproducible from the dataset
alone and citable as "here is the exact list", not "some random sample".

SAMPLE SIZE
-----------
Default is 24 (8 per tier). This is a judgment call, not a statistical
derivation: it is small enough to be tractable at hours-per-build, and large
enough to say something about a 6-condition comparison. It is a starting
point for a pilot, not a claim that 24 is sufficient for the paper's final
numbers — re-run with a larger `--n` once Phase 2 timing from Blocker 1 gives
a real per-iteration wall-clock estimate to budget against.

`115575` (the Blocker 1 bootstrap bug) is excluded by default since it has
already been build-tested in isolation; include it with `--include-bootstrap`
if that's not a reason to exclude it for this particular run.

Usage:
    python3 scripts/select_experiment_sample.py                  # default N=24
    python3 scripts/select_experiment_sample.py --n 30 --tiers 3
    python3 scripts/select_experiment_sample.py --out data/experiment_sample.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from select_bootstrap_bug import (  # noqa: E402
    find_candidates,
    hint_line_count,
    reproducer_size,
)

DEFAULT_EXCLUDE = {"115575"}


def primary_component(data: dict) -> str:
    comps = data.get("hints", {}).get("components") or []
    return comps[0] if comps else "unknown"


def complexity_score(bug_id: str, data: dict) -> tuple:
    return (hint_line_count(data), reproducer_size(data), bug_id)


def split_tiers(candidates: list, n_tiers: int) -> list:
    """Split `candidates` (already sorted by score) into `n_tiers` equal-ish
    contiguous chunks — tier 0 is the simplest third, etc."""
    n = len(candidates)
    tiers = []
    for t in range(n_tiers):
        lo = n * t // n_tiers
        hi = n * (t + 1) // n_tiers
        tiers.append(candidates[lo:hi])
    return tiers


def pick_diverse(tier: list, count: int) -> list:
    """Greedily pick `count` bugs from `tier`, maximizing distinct
    `primary_component` before repeating any component. `tier` is assumed
    pre-sorted by complexity score; ties within a component are broken by
    that pre-existing order."""
    by_component: dict = {}
    for bug_id, data in tier:
        by_component.setdefault(primary_component(data), []).append((bug_id, data))

    picked = []
    round_robin = list(by_component.values())
    idx = 0
    while len(picked) < count and any(round_robin):
        bucket = round_robin[idx % len(round_robin)]
        if bucket:
            picked.append(bucket.pop(0))
        idx += 1
        if idx > 10_000:  # pragma: no cover - safety valve, unreachable in practice
            break
    return picked[:count]


def build_sample(n: int, n_tiers: int, exclude: set) -> dict:
    candidates = [(bug_id, data) for bug_id, data in find_candidates()
                  if bug_id not in exclude]
    candidates.sort(key=lambda kv: complexity_score(*kv))

    tiers = split_tiers(candidates, n_tiers)
    per_tier = [n // n_tiers + (1 if i < n % n_tiers else 0) for i in range(n_tiers)]

    tier_names = ["easy", "medium", "hard"] if n_tiers == 3 else [
        f"tier_{i}" for i in range(n_tiers)
    ]

    manifest = {
        "n_requested": n,
        "n_tiers": n_tiers,
        "excluded": sorted(exclude),
        "pool_size": len(candidates),
        "tiers": {},
    }
    all_bug_ids = []
    for name, tier, count in zip(tier_names, tiers, per_tier):
        picked = pick_diverse(tier, count)
        manifest["tiers"][name] = [
            {
                "bug_id": bug_id,
                "base_commit": data["base_commit"],
                "component": primary_component(data),
                "hint_lines": hint_line_count(data),
                "reproducer_lines": reproducer_size(data),
            }
            for bug_id, data in picked
        ]
        all_bug_ids.extend(bug_id for bug_id, _ in picked)
    manifest["bug_ids"] = all_bug_ids
    manifest["n_selected"] = len(all_bug_ids)
    return manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--n", type=int, default=24)
    parser.add_argument("--tiers", type=int, default=3)
    parser.add_argument("--include-bootstrap", action="store_true",
                        help="don't exclude 115575 (the Blocker 1 bootstrap bug)")
    parser.add_argument("--out", default=None,
                        help="write the manifest as JSON to this path")
    args = parser.parse_args(argv)

    exclude = set() if args.include_bootstrap else set(DEFAULT_EXCLUDE)
    manifest = build_sample(args.n, args.tiers, exclude)

    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
        print(f"wrote {manifest['n_selected']} bugs to {args.out}")

    print(f"pool={manifest['pool_size']} requested={manifest['n_requested']} "
          f"selected={manifest['n_selected']} excluded={manifest['excluded']}")
    for tier_name, entries in manifest["tiers"].items():
        print(f"\n{tier_name} ({len(entries)}):")
        for e in entries:
            print(f"  {e['bug_id']:>8}  {e['component']:<22} "
                  f"hint_lines={e['hint_lines']:<4} repro_lines={e['reproducer_lines']:<4} "
                  f"{e['base_commit'][:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
