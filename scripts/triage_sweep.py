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
    No usable source edit was applied. Depending on the harness version, this
    can mean unmatched source, malformed SEARCH/REPLACE blocks, or a reply
    stopped by its output limit. These causes are counted separately below.
    ``llvm_bench`` found patch invalidity to be a dominant failure mode, so
    this is expected to be non-zero -- the question is how large.

    Recorded patch statuses take precedence. Historical build counts alone
    cannot distinguish these failures from a no-op that was not built.

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
from typing import Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))

from ce.experiment import ExperimentMismatch, load_experiment_runs  # noqa: E402
from ce.patching import PatchFormatError  # noqa: E402
from scripts.inspect_patches import conversation, normalise, extract_code  # noqa: E402

#: Blocker 15's preregistered thresholds, over roughly the first ten bugs.
NOOP_RUN_TO_COMPLETION = 2
NOOP_STOP_AND_FIX = 4
UNAPPLIED_PATCH_STATUSES = frozenset({"mismatch", "invalid_format", "truncated"})


def patch_statuses(run: dict) -> List[Optional[str]]:
    """Per-turn application, no-op, format and truncation statuses, where recorded.

    For records predating the guard, recover unchanged replies from stored
    conversations. Other missing statuses remain unknown: build counts alone
    cannot distinguish a formatting failure from a historical no-op rebuild.
    """
    statuses = [i.get("patch") for i in run.get("iterations", [])]
    if any(s is None for s in statuses):
        hunk, replies = conversation(run)
        if hunk is not None and len(replies) == len(statuses):
            for index, reply in enumerate(replies):
                if statuses[index] is None:
                    try:
                        if normalise(extract_code(reply)) == normalise(hunk):
                            statuses[index] = "unchanged"
                    except PatchFormatError:
                        # A malformed historical response does not establish
                        # either a no-op or an application failure by itself.
                        pass
    return statuses


def classify(run: dict) -> str:
    """Which bucket this run's failure falls in. See the module docstring."""
    if run.get("totals", {}).get("fixed"):
        return "fixed"

    notes = run.get("notes", {})
    if notes.get("llm_error"):
        return "llm_error"

    statuses = patch_statuses(run)
    if statuses and all(s == "unchanged" for s in statuses):
        return "returned the code unchanged"
    if statuses and all(s == "truncated" for s in statuses):
        return "all replies hit output limit"
    if statuses and all(s == "invalid_format" for s in statuses):
        return "invalid patch format"

    validations = [i.get("validation") for i in run.get("iterations", [])]
    regressions = [v for v in validations if v and v.get("stage") == "regression"]
    if any(v.get("failure_kind") == "test_failure" for v in regressions):
        return "broke regression tests"
    if any(v.get("failure_kind") == "timeout" for v in regressions):
        return "regression validation timed out"
    if regressions:
        return "regression validation error"

    iterations = run.get("iterations", [])
    if iterations and all(
        not int(i.get("llm", {}).get("completion_tokens", 0) or 0) for i in iterations
    ):
        return "no reply"

    cert = run.get("certificate") or {}
    builds = int(cert.get("build_count", 0) or 0)
    # check_fast builds once before the loop; each applied patch builds once
    # more. Anything short of that never reached a build.
    unapplied = (sum(s == "unchanged" or s in UNAPPLIED_PATCH_STATUSES for s in statuses)
                 if statuses and all(s is not None for s in statuses)
                 else len(iterations) - max(0, builds - 1))
    if iterations and unapplied >= len(iterations):
        return "patch never applied"
    if int(cert.get("build_failure_count", 0) or 0) >= max(1, builds - 1):
        return "build failed"
    if cert.get("fast_check_pass") and not cert.get("full_check_pass"):
        # Historical sticky flags only prove that some turn reached lit. Look
        # at the returned diagnostics before calling that a real regression.
        messages = (cert.get("log") or {}).get("messages", [])
        lit_logs = [m.get("content", "") for m in messages
                    if m.get("role") == "user" and m.get("content", "").startswith("Feedback:")
                    and "llvm-lit" in m.get("content", "")]
        if any("FAIL:" in log for log in lit_logs):
            return "broke regression tests"
        if any("timed out" in log.lower() for log in lit_logs):
            return "regression validation timed out"
        return "regression validation error"
    if unapplied > 0:
        if "unchanged" in statuses and not any(s in UNAPPLIED_PATCH_STATUSES for s in statuses):
            return "no fix found (some unchanged turns)"
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
        # "reached a build", not "built successfully" -- the distinction matters,
        # and calling this "turns that built" once made a 17% compile-failure
        # rate read as a 100% compile-success rate.
        out["turns that reached a build"] += min(builds, len(iterations))
        statuses = [s for s in patch_statuses(run) if s]
        if statuses:
            out["turns that returned the code unchanged"] += sum(
                1 for s in statuses if s == "unchanged")
            out["turns the patch did not apply"] += sum(
                1 for s in statuses if s in UNAPPLIED_PATCH_STATUSES)
            out["turns with invalid patch format"] += statuses.count("invalid_format")
            out["turns with truncated replies"] += statuses.count("truncated")
            out["turns with unmatched source"] += statuses.count("mismatch")
        else:
            out["turns the patch did not apply"] += max(0, len(iterations) - builds)
        out["turns with an empty reply"] += sum(
            1 for i in iterations
            if not int(i.get("llm", {}).get("completion_tokens", 0) or 0)
        )
        out["build failures"] += int(cert.get("build_failure_count", 0) or 0)
    return out


def iraware_noop(runs: List[dict]) -> Dict[str, Dict[str, int]]:
    """Blocker 15: bugs where every ``iraware`` reduction removed nothing.

    This preserves the registered accepted-pass proxy. A tiny already reduced
    witness may legitimately have no accepted pass. Inspect shown/original
    sizes and fallback errors before interpreting this as a reducer defect;
    structured feedback can still differ from raw-plain without shrinking IR.
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
    parser.add_argument("--model", help="select one model from historical mixed results")
    args = parser.parse_args(argv)

    try:
        runs = load_experiment_runs(args.directory, args.model)
    except ExperimentMismatch as e:
        parser.error(str(e))
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
            "models": sorted({r.get("model", "unknown") for r in runs}),
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

    compiled = turns["turns that reached a build"] - turns["build failures"]
    print("\nWhere the turns went")
    width = max(len(k) for k in turns) + 2
    for name in ("turns", "turns that reached a build",
                 "turns that returned the code unchanged",
                 "turns the patch did not apply", "turns with invalid patch format",
                 "turns with truncated replies", "turns with unmatched source", "build failures",
                 "turns with an empty reply"):
        print(f"  {name.ljust(width)}  {turns[name]:>4}")
    print(f"  {'turns that compiled'.ljust(width)}  {compiled:>4}")
    if turns["turns that reached a build"]:
        share = 100 * turns["build failures"] / turns["turns that reached a build"]
        print(f"  Build failure share of attempted builds: {share:.1f}%")
    print("  Stage 'error' alone does not distinguish compiler diagnostics, an Alive2 violation, or infrastructure.")

    # The number the buckets bury: of every patch that did compile, how many
    # got as far as making the bug's own reproducer pass -- before lit is even
    # consulted. Zero here means the model is not near a fix, which is a
    # different and much worse position than failing on regressions.
    reached = sum(1 for r in runs if (r.get("certificate") or {}).get("fast_check_pass"))
    print(f"  {'runs where the reproducer passed'.ljust(width)}  {reached:>4}"
          f"  (of {len(runs)})")

    print(f"\nBlocker 15: iraware reduced nothing on "
          f"{len(noop_bugs)} of {len(noop['total'])} bugs seen"
          + (f" ({', '.join(noop_bugs)})" if noop_bugs else ""))
    if len(noop["total"]) >= 8:
        if len(noop_bugs) >= NOOP_STOP_AND_FIX:
            print("  -> registered STOP AND FIX checkpoint: pause at a safe cell boundary and inspect "
                  "actual shown sizes and fallback errors before changing the reducer.")
        elif len(noop_bugs) <= NOOP_RUN_TO_COMPLETION:
            print("  -> preregistered decision: run to completion, record as a "
                  "limitation in METHODOLOGY.md.")
        else:
            print("  -> between the preregistered thresholds (<=2 continue, "
                  ">=4 stop). Judgement call; say which way and why.")
    else:
        print("  -> fewer than ~8 bugs seen; the decision point is ~10.")
    print("  This counts accepted-pass lists, not actual IR shrinkage; use audit_sweep.py for displayed sizes.")

    # The verdict this whole script exists to support.
    #
    # Thresholds, not any-nonzero. A handful of failed cells in a long sweep is
    # housekeeping -- re-run them. Blocker 14 was every condition failing, and
    # only that shape means the sweep is not measuring anything. Reporting 3
    # bad cells out of 216 in the same words as 9 out of 9 buries the finding
    # under the footnote.
    print("\nRead")
    broken = buckets.get("llm_error", 0) + buckets.get("no reply", 0)
    if broken > len(runs) * 0.1:
        print(f"  {broken} of {len(runs)} runs never got a usable reply. That "
              "is Blocker 14's shape -- those runs are not data. Check "
              "notes.llm_error before reading anything else, and re-run those "
              "cells with --overwrite.")
    elif broken:
        print(f"  {broken} runs failed to get a reply -- housekeeping, not "
              "Blocker 14's shape. Re-run those cells with --overwrite.")
    elif turns["turns that returned the code unchanged"] > turns["turns"] * 0.25:
        print("  Over a quarter of turns returned unchanged code. Inspect the "
              "stored replies and prompt before spending another full sweep.")
    elif turns["turns the patch did not apply"] > turns["turns"] * 0.5:
        print("  Over half of all turns never reached a build: the model is "
              "returning unapplied edits. Inspect malformed blocks, unmatched "
              "source and output-limit counters before spending more compute. "
              "Run check_llm_endpoint.py --repair-format for a v3 preflight.")
    elif buckets.get("broke regression tests"):
        print(f"  {buckets['broke regression tests']} runs fixed the "
              "reproducer in at least one turn and then failed lit. Inspect "
              "the exact test failures before changing the model. The final "
              "saved patch may come from a later turn that did not pass.")
    elif buckets.get("regression validation timed out") or buckets.get("regression validation error"):
        print("  Some patches reached lit but validation timed out or failed "
              "without a test assertion. Check the toolchain and validation logs.")
    elif not fixed_runs:
        print(f"  No full fixes in {len(runs)} runs over {len(bugs)} bugs. This "
              "does not establish a model floor or predict unattempted bugs. "
              "Verify a known human patch and screen the held-out pilot before "
              "committing to the rest of the sweep. All-zero outcomes have no "
              "discordant pairs to inform the condition comparison.")
    else:
        print(f"  {len(fixed_runs)} runs passed full validation. Compare only "
              "matched bugs under this experiment configuration; partial "
              "coverage is not evidence about unattempted bugs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
