#!/usr/bin/env python3
"""Turn a folder of experiment runs into the results table.

    python3 examples/summarize_results.py results/ [--json]

Reads every run record written by ``repair_experiment.py`` and prints, per
condition: how many bugs were attempted, how many were fixed, and what it cost
(turns, tokens, Alive2 calls, wall-clock time).

**Repair rate is the headline number**, but always read it next to the costs.
A condition that fixes more bugs while spending twice as much is a different
finding from one that fixes more for less.

When comparing cost, **`mean_iterations` is the efficiency claim** — not
`mean_prompt_tokens_est` or `mean_wall_seconds`. One iteration is one LLVM
rebuild (minutes); a few hundred prompt tokens or a few seconds of difference
is noise against that (`context.md` RQ4, decided 2026-08-27 — see
`docs/IMPLEMENTATION.md` Blocker 2). The token/time columns are still printed
because they're useful descriptive context, just don't lead with them.

WHY THERE ARE TWO TABLES
------------------------
The second table is the one to trust.

Conditions can end up attempting different sets of bugs -- a run crashes, a
build times out, you interrupt a sweep halfway.  If condition A happened to
attempt an easier subset than condition B, comparing their raw percentages is
meaningless.

The "paired" table fixes this by only counting bugs that **every** condition
attempted, so all conditions are being scored on identical work.

THE PROMOTION ABLATION IS KEPT SEPARATE
----------------------------------------
``--no-promotion`` (docs/IMPLEMENTATION.md Blocker 7) is a separate ablation
axis, not one of the six/eight main conditions -- a run under
``iraware-structured`` with promotion off is still "the ``iraware-structured``
condition", just with one pass disabled. Mixing it into the main tables would
silently conflate two different runs of the same condition name. The "All
runs"/"Paired" tables above therefore only ever include the default
(promotion-on) records; a separate "No-promotion ablation" table is printed
underneath when any ``--no-promotion`` records exist, so the ablation is
visible without corrupting the primary comparison.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))

from ce.benchmark import load_runs, summarize  # noqa: E402

COLUMNS = (
    ("condition", 24),
    ("bugs_attempted", 9),
    ("bugs_fixed", 7),
    ("repair_rate", 11),
    ("mean_iterations", 10),
    ("mean_prompt_tokens_est", 12),
    ("mean_oracle_calls", 11),
    ("mean_wall_seconds", 11),
)


def print_table(table: dict) -> None:
    header = "  ".join(name[:w].ljust(w) for name, w in COLUMNS)
    print(header)
    print("-" * len(header))
    for condition, row in table.items():
        cells = [condition[:COLUMNS[0][1]].ljust(COLUMNS[0][1])]
        for name, w in COLUMNS[1:]:
            value = row.get(name, "-")
            cells.append((f"{value:.3f}" if isinstance(value, float) else str(value))[:w].ljust(w))
        print("  ".join(cells))


def _allow_promotion(run: dict) -> bool:
    return run.get("notes", {}).get("allow_promotion", True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", nargs="?", default="results")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    all_runs = load_runs(args.directory)
    if not all_runs:
        print(f"no run records in {args.directory}", file=sys.stderr)
        return 2

    # Keep the promotion ablation out of the main tables (see module
    # docstring) -- it's a separate axis, not another condition.
    runs = [r for r in all_runs if _allow_promotion(r)]
    ablation_runs = [r for r in all_runs if not _allow_promotion(r)]

    table = summarize(runs)

    # Restrict to bugs attempted under every condition, so that a condition is
    # not credited for an easier subset than the others faced.
    by_condition: dict = {}
    for run in runs:
        by_condition.setdefault(run["condition"], set()).add(run["bug_id"])
    common = set.intersection(*by_condition.values()) if by_condition else set()
    paired = summarize([r for r in runs if r["bug_id"] in common]) if common else {}

    ablation_table = summarize(ablation_runs) if ablation_runs else {}

    if args.json:
        print(json.dumps({
            "all": table,
            "paired": paired,
            "paired_bug_count": len(common),
            "no_promotion_ablation": ablation_table,
        }, indent=2))
        return 0

    print(f"All runs ({len(runs)} records)")
    print_table(table)
    if paired and len(by_condition) > 1:
        print(f"\nPaired over the {len(common)} bugs attempted under all "
              f"{len(by_condition)} conditions")
        print_table(paired)
    if ablation_table:
        print(f"\nNo-promotion ablation ({len(ablation_runs)} records) -- "
              f"compare each row against its counterpart above")
        print_table(ablation_table)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
