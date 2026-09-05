#!/usr/bin/env python3
"""What did the model actually write, and did the feedback change it?

    python3 scripts/inspect_patches.py results/
    python3 scripts/inspect_patches.py results/ --show 89390    # print real diffs

WHY THIS EXISTS
---------------
A repair rate of zero is consistent with two very different situations, and
they call for opposite responses:

**A capability ceiling.** The model makes plausible, targeted edits to the
right code and they are simply wrong. Then the model is the lever, and a
larger one might clear the floor.

**A task-framing failure.** The model returns the hunk unchanged, or rewrites
it cosmetically, or produces the same answer every turn no matter what it is
told. Then a larger model walks into the same wall, and the prompt or the loop
is what needs fixing.

``scripts/triage_sweep.py`` cannot tell these apart -- both look like "no fix
found". This reads the conversations themselves.

THE QUESTION THAT DECIDES THE STUDY
-----------------------------------
This experiment varies exactly one thing: the feedback text. That only means
anything if the text changes what the model writes. So the central measurement
here is a comparison of two similarities:

    across conditions   same bug, same trial, nine different feedback texts
    across trials       same bug, same condition, identical feedback text

The second is a pure sampling-noise floor: at temperature 0.8, two runs of the
identical prompt still differ. If the first is no lower than the second, the
nine conditions are drawing from one distribution and the design is measuring
nothing -- **which no change of model can fix.** If the first is clearly
lower, the feedback is landing and the repair rate is the model's problem.

Everything is derived from ``certificate.log.messages``, which
``repair_experiment.py`` already stores for every run.
"""

from __future__ import annotations

import argparse
import collections
import difflib
import json
import os
import re
import statistics
import sys
from typing import Dict, List, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, os.pardir))
sys.path.insert(0, os.path.join(_HERE, os.pardir, "examples"))

from ce.benchmark import load_runs  # noqa: E402

try:  # keep one definition of "which part of the reply is code"
    from repair_experiment import extract_code  # noqa: E402
except Exception:  # pragma: no cover - only if examples/ moves
    def extract_code(reply: str) -> str:
        if reply.startswith("```"):
            return reply.strip().removeprefix("```cpp").removeprefix("```").removesuffix("```")
        for pattern in (r"```cpp([\s\S]+)```", r"```([\s\S]+)```"):
            matches = re.findall(pattern, reply)
            if matches:
                return matches[-1]
        return reply

#: The initial user message embeds the window the model must rewrite.
_HUNK = re.compile(
    r"Please modify the following code in (\S+) to fix the bug:\s*```cpp\n(.*?)\n```",
    re.DOTALL,
)


def normalise(code: str) -> str:
    """Collapse whitespace so reformatting does not read as a semantic edit."""
    return "\n".join(line.strip() for line in code.strip().splitlines() if line.strip())


def conversation(run: dict) -> Tuple[Optional[str], List[str]]:
    """``(original hunk, [reply per turn])`` from a run's stored messages."""
    log = ((run.get("certificate") or {}).get("log")) or {}
    messages = log.get("messages") or []
    hunk = None
    for message in messages:
        if message.get("role") == "user":
            found = _HUNK.search(message.get("content") or "")
            if found:
                hunk = found.group(2)
                break
    replies = [m.get("content") or "" for m in messages if m.get("role") == "assistant"]
    return hunk, replies


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", nargs="?", default="results")
    parser.add_argument("--show", metavar="BUG_ID",
                        help="print the real first-turn diff for each condition "
                             "on this bug")
    parser.add_argument("--context", type=int, default=3,
                        help="diff context lines with --show")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    runs = load_runs(args.directory)
    if not runs:
        print(f"no run records in {args.directory}", file=sys.stderr)
        return 2

    #: (bug, trial, condition) -> normalised first-turn code
    first: Dict[Tuple[str, int, str], str] = {}
    #: (bug, condition, trial) -> the original hunk, for --show
    hunks: Dict[str, str] = {}
    raw_first: Dict[Tuple[str, int, str], str] = {}

    unchanged = same_as_previous = no_fence = turns = missing = 0
    edit_sizes: List[int] = []
    #: normalised original hunk per bug, so a reply can be tested for "no edit"
    bases: Dict[str, str] = {}
    #: per condition: (turns, turns that returned the hunk unchanged)
    by_condition: Dict[str, List[int]] = collections.defaultdict(lambda: [0, 0])

    for run in runs:
        bug = str(run.get("bug_id"))
        cond = str(run.get("condition"))
        trial = int(run.get("trial", 1) or 1)
        hunk, replies = conversation(run)
        if hunk is None or not replies:
            missing += 1
            continue
        hunks.setdefault(bug, hunk)
        base = normalise(hunk)
        bases.setdefault(bug, base)

        previous = None
        for index, reply in enumerate(replies):
            turns += 1
            by_condition[cond][0] += 1
            if "```" not in reply:
                no_fence += 1
            code = normalise(extract_code(reply))
            if code == base:
                unchanged += 1
                by_condition[cond][1] += 1
            if previous is not None and code == previous:
                same_as_previous += 1
            previous = code
            edit_sizes.append(sum(
                1 for line in difflib.unified_diff(
                    base.splitlines(), code.splitlines(), n=0)
                if line[:1] in "+-" and not line.startswith(("+++", "---"))
            ))
            if index == 0:
                first[(bug, trial, cond)] = code
                raw_first[(bug, trial, cond)] = extract_code(reply)

    # --- the measurement that decides the study ---------------------------
    #
    # Restricted to replies that actually edited something. A reply that echoes
    # the hunk back verbatim carries no information about whether the feedback
    # landed, and once those dominate, both similarities collapse to "how alike
    # are two copies of one text" -- about 1.0, by construction, whatever the
    # feedback did. Including them does not measure feedback-sensitivity
    # weakly; it measures the no-op rate and mislabels it.
    across_conditions: List[float] = []
    by_bug_trial: Dict[Tuple[str, int], Dict[str, str]] = collections.defaultdict(dict)
    by_bug_cond: Dict[Tuple[str, str], Dict[int, str]] = collections.defaultdict(dict)
    for (bug, trial, cond), code in first.items():
        if code == bases.get(bug):
            continue
        by_bug_trial[(bug, trial)][cond] = code
        by_bug_cond[(bug, cond)][trial] = code

    for cells in by_bug_trial.values():
        names = sorted(cells)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                across_conditions.append(similarity(cells[a], cells[b]))

    across_trials: List[float] = []
    for cells in by_bug_cond.values():
        keys = sorted(cells)
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                across_trials.append(similarity(cells[a], cells[b]))

    def mean(values: List[float]) -> float:
        return round(statistics.fmean(values), 4) if values else float("nan")

    report = {
        "runs": len(runs),
        "runs_without_conversation": missing,
        "turns": turns,
        "returned_the_hunk_unchanged": unchanged,
        "identical_to_previous_turn": same_as_previous,
        "replies_without_a_code_fence": no_fence,
        "median_lines_changed": statistics.median(edit_sizes) if edit_sizes else 0,
        "similarity_across_conditions": mean(across_conditions),
        "similarity_across_trials": mean(across_trials),
        "pairs_across_conditions": len(across_conditions),
        "pairs_across_trials": len(across_trials),
    }

    if args.json and not args.show:
        print(json.dumps(report, indent=2))
        return 0

    print(f"{len(runs)} runs, {turns} model replies"
          + (f" ({missing} runs had no stored conversation)" if missing else ""))

    print("\nWhat the replies look like")
    rows = [
        ("returned the hunk unchanged", unchanged),
        ("identical to the previous turn", same_as_previous),
        ("no code fence in the reply", no_fence),
    ]
    width = max(len(name) for name, _ in rows)
    for name, count in rows:
        share = 100.0 * count / turns if turns else 0.0
        print(f"  {name.ljust(width)}  {count:>4}  {share:5.1f}%")
    print(f"  {'median lines changed vs the hunk'.ljust(width)}  "
          f"{report['median_lines_changed']:>4}")

    # Whether the model edits at all is itself a per-condition outcome. If the
    # richer feedback provokes an edit more often, the feedback is landing --
    # expressed as willingness to touch the code rather than as which edit is
    # chosen. That signal survives even when the similarity metric below has
    # gone degenerate.
    if by_condition:
        print("\nHow often each condition provoked no edit at all")
        width = max(len(c) for c in by_condition)
        rates = {}
        for cond in sorted(by_condition):
            total, noop = by_condition[cond]
            rate = noop / total if total else 0.0
            rates[cond] = rate
            bar = "#" * round(rate * 30)
            print(f"  {cond.ljust(width)}  {noop:>3}/{total:<3} {rate:5.1%}  {bar}")
        spread = max(rates.values()) - min(rates.values())
        print(f"  spread across conditions: {spread:.1%}")

    print("\nDoes the feedback change what the model writes?")
    print("  (only replies that actually edited something -- verbatim echoes "
          "carry no signal)")
    print(f"  same bug+trial, ACROSS the 9 conditions   "
          f"{report['similarity_across_conditions']:.3f}   "
          f"({len(across_conditions)} pairs, feedback differs)")
    trials_cell = (f"{report['similarity_across_trials']:.3f}"
                   if across_trials else "  --- ")
    print(f"  same bug+condition, ACROSS trials         "
          f"{trials_cell}   "
          f"({len(across_trials)} pairs, identical prompt)")

    gap = report["similarity_across_trials"] - report["similarity_across_conditions"]
    noop_rate = unchanged / turns if turns else 0.0
    if noop_rate >= 0.25:
        print(f"\n  -> {noop_rate:.0%} of replies returned the hunk unchanged. "
              "That is the finding, and it comes before the similarity numbers: "
              "the loop is mostly not attempting a repair at all. Every one of "
              "those turns still reset the tree, rebuilt LLVM and ran lit to "
              "confirm that unmodified source does not fix the bug.")
        print("     Feedback-sensitivity is UNTESTED here, not disproven -- "
              "there are too few real edits to measure it. Fix the no-op rate "
              "first, then re-run this to find out whether the conditions "
              "differ.")
    elif not across_trials:
        print("\n  -> No sampling-noise floor to compare against: every "
              "condition here was run once, so there is no pair of trials of "
              "an identical prompt. The across-conditions number alone means "
              "nothing -- two samples of the same prompt differ too. Re-run "
              "with --repeat 3 before reading it.")
    elif len(across_conditions) < 30 or len(across_trials) < 10:
        print("\n  -> Too few genuine edits to compare; treat the similarity "
              "numbers above as unmeasured rather than as a result.")
    elif gap <= 0.02:
        print("\n  -> Changing the feedback moves the model no more than "
              "resampling the same prompt does. The nine conditions are "
              "drawing from one distribution, so the design is measuring "
              "nothing -- and a bigger model does not fix that. The prompt "
              "or the loop is what has to change.")
    else:
        print(f"\n  -> The feedback lands: conditions differ by {gap:.3f} "
              "more than sampling noise alone. The design is sound and the "
              "zero repair rate is a capability problem, so the model is "
              "the lever worth pulling.")

    if args.show:
        bug = args.show
        hunk = hunks.get(bug)
        if hunk is None:
            print(f"\nno stored conversation for bug {bug}", file=sys.stderr)
            return 1
        print(f"\n\nFirst-turn edits on bug {bug}, by condition")
        print("=" * 72)
        cells = {c: code for (b, t, c), code in raw_first.items()
                 if b == bug and t == 1}
        for cond in sorted(cells):
            diff = list(difflib.unified_diff(
                hunk.splitlines(), cells[cond].splitlines(),
                fromfile="original", tofile=cond, n=args.context, lineterm=""))
            print(f"\n--- {cond} " + "-" * max(0, 66 - len(cond)))
            if not diff:
                print("  (no change: returned the hunk verbatim)")
            else:
                for line in diff[:80]:
                    print("  " + line)
                if len(diff) > 80:
                    print(f"  ... {len(diff) - 80} more diff lines")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
