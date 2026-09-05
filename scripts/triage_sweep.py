#!/usr/bin/env python3
"""Why is the sweep not fixing anything? Read the run records and say.

    python3 scripts/triage_sweep.py results/

WHY THIS EXISTS
---------------
A run record that says ``fixed: false`` says nothing about *why*, and the
reasons are not equivalent. Some mean the experiment is working and the task
is simply hard, which is what ``docs/SLM_SELECTION.md`` section 2.1 predicts.
Others mean the harness is broken and the whole sweep is worthless.

Blocker 14 is the cautionary case: every one of nine pilot conditions wrote a
normal-looking ``fixed: false`` record while the model was actually being
handed a 400 at 4097 tokens and never answering. Nothing surfaced it until
someone grepped ``notes.llm_error``. A silent failure mode does not announce
itself; it looks exactly like a hard task.

So this classifies each run into one of a few buckets, all of them derivable
from what the records already store:

``llm_error``
    The model call failed. Blocker 14's failure, still the first thing to
    rule out. Anything here means those runs are not data.

``no reply``
    The call succeeded but came back empty. A reasoning model whose output
    lands in a channel the client does not read does this.

``patch never applied``
    The model did not echo the code window back verbatim, so ``apply_patch``
    refused and the turn was spent on formatting rather than repair.
    ``llvm_bench`` found patch invalidity to be a dominant failure mode, so
    this is expected to be non-zero -- the question is how large.

    Detected exactly, not guessed: ``check_fast`` builds once before the loop
    and every applied patch builds once more, so a run's builds should number
    ``1 + iterations``. Whatever is missing never reached a build.

``build failed``
    The patch applied but did not compile. Real model output, wasted turn.

``broke regression tests``
    **The interesting one.** ``fast_check_pass`` true with
    ``full_check_pass`` false means the patch *fixed the reproducer* and then
    failed LLVM's existing lit tests. The model solved the stated problem and
    overreached. A sweep full of these is a completely different finding from
    a sweep full of no-ops, and it is invisible in the repair rate.

``no fix found``
    Everything worked and the patch just did not fix the bug. This is the
    bucket a healthy, honestly-hard sweep should mostly land in.

It also runs Blocker 15's preregistered ``iraware`` no-op check, since that
decision is due after roughly the first ten bugs and needs the same records.

Nothing here is a hypothesis test. It is a triage instrument for deciding
whether the sweep is worth finishing.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from typing import Dict, List

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))

from ce.benchmark import load_runs  # noqa: E402

#: Blocker 15's preregistered thresholds, over roughly the first ten bugs.
NOOP_RUN_TO_COMPLETION = 2
NOOP_STOP_AND_FIX = 4


def classify(run: dict) -> str:
    """Which bucket this run's failure falls in. See the module docstring."""
    if run.get("totals", {}).get("fixed"):
        return "fixed"

    notes = run.get("notes", {})
    if notes.get("llm_error"):
        return "llm_error"

    iterations = run.get("iterations", [])
    if iterations and all(
        not int(i.get("llm", {}).get("completion_tokens", 0) or 0) for i in iterations
    ):
        return "no reply"

    cert = run.get("certificate") or {}
    builds = int(cert.get("build_count", 0) or 0)
    # check_fast builds once before the loop; each applied patch builds once
    # more. Anything short of that never reached a build.
    unapplied = len(iterations) - max(0, builds - 1)
    if iterations and unapplied >= len(iterations):
        return "patch never applied"
    if int(cert.get("build_failure_count", 0) or 0) >= max(1, builds - 1):
        return "build failed"
    if cert.get("fast_check_pass") and not cert.get("full_check_pass"):
        return "broke regression tests"
    if unapplied > 0:
        return "no fix found (some turns unapplied)"
    return "no fix found"


def turn_level(runs: List[dict]) -> Dict[str, int]:
    """Per-turn tallies, which the per-run bucket can hide.

    A run whose first three turns failed to apply and whose fourth built
    cleanly is classified by its best turn, not its worst. These counts show
    what the four turns were actually spent on.
    """
    out = collections.Counter()
    for run in runs:
        iterations = run.get("iterations", [])
        cert = run.get("certificate") or {}
        builds = max(0, int(cert.get("build_count", 0) or 0) - 1)
        out["turns"] += len(iterations)
        out["turns that built"] += min(builds, len(iterations))
        out["turns the patch did not apply"] += max(0, len(iterations) - builds)
        out["turns with an empty reply"] += sum(
            1 for i in iterations
            if not int(i.get("llm", {}).get("completion_tokens", 0) or 0)
        )
        out["build failures"] += int(cert.get("build_failure_count", 0) or 0)
    return out


def iraware_noop(runs: List[dict]) -> Dict[str, Dict[str, int]]:
    """Blocker 15: bugs where every ``iraware`` reduction removed nothing.

    A no-op makes ``iraware``'s prompt byte-identical to ``raw``'s for that
    bug, so the row stops measuring what it is labelled as.
    """
    total: Dict[str, int] = collections.Counter()
    noop: Dict[str, int] = collections.Counter()
    for run in runs:
        if not str(run.get("condition", "")).startswith("iraware"):
            continue
        for it in run.get("iterations", []):
            fb = it.get("feedback", {})
            if not fb.get("counterexample"):
                continue
            bug = str(run.get("bug_id"))
            total[bug] += 1
            if not fb.get("passes_applied"):
                noop[bug] += 1
    return {"total": dict(total), "noop": dict(noop)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", nargs="?", default="results")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    runs = load_runs(args.directory)
    if not runs:
        print(f"no run records in {args.directory}", file=sys.stderr)
        return 2

    buckets = collections.Counter(classify(r) for r in runs)
    bugs = sorted({str(r.get("bug_id")) for r in runs})
    conditions = sorted({str(r.get("condition")) for r in runs})
    turns = turn_level(runs)
    noop = iraware_noop(runs)
    noop_bugs = sorted(b for b, n in noop["total"].items() if noop["noop"].get(b, 0) == n)

    fixed_runs = [r for r in runs if r.get("totals", {}).get("fixed")]
    fixed_bugs = sorted({str(r["bug_id"]) for r in fixed_runs})

    if args.json:
        print(json.dumps({
            "runs": len(runs), "bugs": bugs, "conditions": conditions,
            "buckets": dict(buckets), "turns": dict(turns),
            "fixed_runs": len(fixed_runs), "fixed_bugs": fixed_bugs,
            "iraware_noop_bugs": noop_bugs,
            "iraware_bugs_seen": sorted(noop["total"]),
        }, indent=2))
        return 0

    print(f"{len(runs)} runs over {len(bugs)} bugs x {len(conditions)} conditions")
    print(f"fixed: {len(fixed_runs)} runs, {len(fixed_bugs)} distinct bugs"
          + (f" ({', '.join(fixed_bugs)})" if fixed_bugs else ""))

    print("\nWhy each run ended")
    width = max(len(k) for k in buckets)
    for name, count in buckets.most_common():
        share = 100.0 * count / len(runs)
        print(f"  {name.ljust(width)}  {count:>4}  {share:5.1f}%")

    print("\nWhere the turns went")
    width = max(len(k) for k in turns)
    for name in ("turns", "turns that built", "turns the patch did not apply",
                 "build failures", "turns with an empty reply"):
        print(f"  {name.ljust(width)}  {turns[name]:>4}")

    print(f"\nBlocker 15: iraware reduced nothing on "
          f"{len(noop_bugs)} of {len(noop['total'])} bugs seen"
          + (f" ({', '.join(noop_bugs)})" if noop_bugs else ""))
    if len(noop["total"]) >= 8:
        if len(noop_bugs) >= NOOP_STOP_AND_FIX:
            print("  -> preregistered decision: STOP AND FIX. Enough rows are "
                  "compromised to bias the sweep toward a null result.")
        elif len(noop_bugs) <= NOOP_RUN_TO_COMPLETION:
            print("  -> preregistered decision: run to completion, record as a "
                  "limitation in METHODOLOGY.md.")
        else:
            print("  -> between the preregistered thresholds (<=2 continue, "
                  ">=4 stop). Judgement call; say which way and why.")
    else:
        print("  -> fewer than ~8 bugs seen; the decision point is ~10.")

    # The verdict this whole script exists to support.
    print("\nRead")
    if buckets.get("llm_error"):
        print("  The model call failed on "
              f"{buckets['llm_error']} runs. Those are NOT data -- this is "
              "Blocker 14's failure mode. Check notes.llm_error before "
              "anything else and re-run those cells with --overwrite.")
    elif buckets.get("no reply"):
        print(f"  {buckets['no reply']} runs got empty replies. The endpoint "
              "is answering but the client is not reading the output.")
    elif turns["turns the patch did not apply"] > turns["turns"] * 0.5:
        print("  Over half of all turns never reached a build: the model is "
              "not echoing the code window back verbatim. Turns are being "
              "spent on formatting, not repair -- fix the prompt before "
              "spending more compute.")
    elif buckets.get("broke regression tests"):
        print(f"  {buckets['broke regression tests']} runs fixed the "
              "reproducer and then failed lit. The model is solving the "
              "stated problem and overreaching -- a real finding, and "
              "invisible in the repair rate.")
    else:
        print("  No harness failure visible: turns are reaching the model, "
              "patches are applying, builds are running, and the patches "
              "simply do not fix the bugs. That is the expected shape of a "
              "hard task (SLM_SELECTION.md section 2.1) -- but a sweep that "
              "ends at 0% still cannot answer the research question, since "
              "identical all-zero columns give McNemar no discordant pairs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
