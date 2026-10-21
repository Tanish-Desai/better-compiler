#!/usr/bin/env python3
"""The preregistered significance test for the condition sweep.

    python3 examples/analyze_significance.py results/ [--json] [--all-pairs]

WHAT THIS ANSWERS
-----------------
``summarize_results.py`` tells you that, say, ``iraware-plain`` fixed 9 of 24
bugs and ``generic-plain`` fixed 5. This script answers the next question: is
that gap bigger than what shuffling a few coin flips would produce?

WHY McNEMAR AND NOT FISHER'S EXACT
----------------------------------
Every condition is run on **the same 24 bugs**. That pairing is the whole
design -- it is why bug difficulty cancels out instead of being noise. Fisher's
exact test is for two *independent* groups; feeding it paired data throws the
pairing away and, with 24 bugs, throws away most of the power along with it.

McNemar's test is the paired counterpart. It ignores the bugs both conditions
fixed and the bugs neither fixed -- those carry no information about which
condition is better -- and asks only about the **discordant** bugs:

           b = fixed by A, not by B
           c = fixed by B, not by A

Under the null "the two feedback formats are equally good", each discordant bug
is a fair coin, so ``b ~ Binomial(b + c, 1/2)``. That is the exact test used
here. The chi-squared approximation McNemar is usually quoted as is *not* used:
with b + c likely under 15 it is not trustworthy, and the exact version costs
nothing.

This is also what the closest prior work does. `Agentic Harness for Real-World
Compilers` (llvm-harness, the same LLVM middle-end bug family) reports exactly
this -- a one-sided McNemar at alpha = 0.05 with the ``#01 #10 #11 #00`` matrix
printed alongside -- and `RepairLLaMA` uses McNemar for "the binary outcomes of
two representations evaluated on the same set of benchmark examples", which is
this design with the nouns changed. See docs/SLM_SELECTION.md section 6.

WHAT COUNTS AS ONE OBSERVATION
------------------------------
One bug, not one run. With ``--repeat k`` a bug is attempted k times per
condition and its outcome is **pass@k**: fixed on at least one trial. That
keeps n at 24 paired observations however large k is -- k buys a more reliable
outcome per bug, not more bugs (docs/ANALYSIS_PLAN.md).

THE FAMILY OF TESTS IS FIXED IN ADVANCE
---------------------------------------
There are 36 pairs among 9 conditions. Testing all of them and reporting the
smallest p-value is how a null result gets laundered into a finding. So four
comparisons are **primary** -- fixed before any data existed, each one the
direct operationalisation of a stated research question -- and everything else
is exploratory and reported without inferential claims.

The primary four are ``PRIMARY`` below. Their p-values are corrected together
with Benjamini-Hochberg (the correction `Improving Code Generation via Small
Language Model-as-a-judge` applies to its own McNemar family); ``--all-pairs``
prints the rest, uncorrected and labelled exploratory.

THE PROMOTION ABLATION
----------------------
``--ablation`` asks a different question with the same machinery: for each
``iraware`` condition, does the same condition with ``promote-operands``
disabled fix the same bugs? That is a paired comparison of a condition against
itself across the promotion axis, so the two arms are relabelled and run
through McNemar exactly as above. It answers METHODOLOGY.md section 4's worry
that promotion generalises the program -- if disabling it costs nothing, the
generalisation was not carrying the result.

READ THE EFFECT SIZE, NOT JUST THE STAR
---------------------------------------
With 24 bugs this test can only detect a large effect: it takes roughly five
discordant bugs all pointing one way to clear alpha = 0.05 one-sided. A p above
0.05 here means "this sample cannot resolve it", not "there is no effect". The
b/c counts and the rate difference are printed for every comparison for that
reason, and should be reported whatever the p-value says.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))

from ce.benchmark import per_bug_outcomes  # noqa: E402
from ce.analysis import select_complete_runs  # noqa: E402
from ce.experiment import ExperimentMismatch, load_experiment_runs  # noqa: E402
from ce.feedback import CONDITIONS  # noqa: E402

#: The preregistered primary family: ``(better, worse, what it asks)``.
#: "better" is the direction the hypothesis predicts, which is what makes the
#: one-sided test legitimate -- the direction was not read off the results.
PRIMARY: Tuple[Tuple[str, str, str], ...] = (
    ("iraware-plain", "generic-plain",
     "RQ: does IR-aware reduction beat generic text reduction?"),
    ("iraware-plain", "llvmreduce-plain",
     "Blocker 5: is it counterexample-awareness, or just IR-validity?"),
    ("iraware-structured", "iraware-plain",
     "RQ: does structured layout add anything on top of reduction?"),
    ("raw-plain", "baseline",
     "sanity anchor: does the counterexample help at all?"),
)

ALPHA = 0.05


# --------------------------------------------------------------------------
# Statistics, in stdlib only -- no scipy in the container image
# --------------------------------------------------------------------------

def binom_sf_inclusive(k: int, n: int) -> float:
    """``P(X >= k)`` for ``X ~ Binomial(n, 1/2)``."""
    if n == 0:
        return 1.0
    return sum(math.comb(n, i) for i in range(k, n + 1)) / (2 ** n)


def mcnemar_exact(b: int, c: int) -> Tuple[float, float]:
    """Exact McNemar on discordant counts. Returns ``(one_sided, two_sided)``.

    ``b`` is the count favouring the hypothesised-better condition. The
    one-sided p is ``P(X >= b)`` under ``X ~ Binomial(b + c, 1/2)``; the
    two-sided is the usual doubling, clamped at 1.
    """
    n = b + c
    if n == 0:
        return 1.0, 1.0
    one = binom_sf_inclusive(b, n)
    return one, min(1.0, 2.0 * min(one, binom_sf_inclusive(c, n)))


def benjamini_hochberg(pvalues: Sequence[float]) -> List[float]:
    """BH-adjusted p-values, in the input order."""
    m = len(pvalues)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvalues[i])
    adjusted = [1.0] * m
    running = 1.0
    for rank, i in enumerate(reversed(order), start=1):
        running = min(running, pvalues[i] * m / (m - rank + 1))
        adjusted[i] = min(1.0, running)
    return adjusted


# --------------------------------------------------------------------------
# The comparison
# --------------------------------------------------------------------------

def compare(outcomes: Dict[str, Dict[str, bool]], better: str,
            worse: str) -> Optional[dict]:
    """One paired comparison, or None if a condition has no runs."""
    if better not in outcomes or worse not in outcomes:
        return None
    shared = sorted(set(outcomes[better]) & set(outcomes[worse]))
    if not shared:
        return None

    b = c = both = neither = 0
    for bug in shared:
        x, y = outcomes[better][bug], outcomes[worse][bug]
        if x and y:
            both += 1
        elif x:
            b += 1
        elif y:
            c += 1
        else:
            neither += 1

    one_sided, two_sided = mcnemar_exact(b, c)
    n = len(shared)
    return {
        "better": better,
        "worse": worse,
        "n_bugs": n,
        "fixed_better": both + b,
        "fixed_worse": both + c,
        "rate_better": round((both + b) / n, 4),
        "rate_worse": round((both + c) / n, 4),
        "rate_difference": round((b - c) / n, 4),
        "only_better": b,
        "only_worse": c,
        "both": both,
        "neither": neither,
        "discordant": b + c,
        "odds_ratio": (round(b / c, 3) if c else (float("inf") if b else None)),
        "p_one_sided": round(one_sided, 6),
        "p_two_sided": round(two_sided, 6),
    }


def _allow_promotion(run: dict) -> bool:
    return run.get("notes", {}).get("allow_promotion", True)


ROW = ("{better:<34} {worse:<34} {n_bugs:>3} {fixed_better:>4}/{fixed_worse:<4} "
       "{only_better:>3} {only_worse:>3} {rate_difference:>+7.3f} "
       "{p_one_sided:>9.4f}")


def print_rows(rows: Sequence[dict], adjusted: Optional[Sequence[float]] = None,
               *, final: bool = False) -> None:
    header = ("{:<34} {:<34} {:>3} {:>9} {:>3} {:>3} {:>7} {:>9}").format(
        "better (hypothesised)", "worse", "n", "fixed", "b", "c", "diff", "p(1-sided)")
    if adjusted is not None:
        header += "  {:>9}  {}".format("p(BH)", "sig")
    print(header)
    print("-" * len(header))
    for i, row in enumerate(rows):
        line = ROW.format(**row)
        if adjusted is not None:
            p = adjusted[i]
            line += "  {:>9.4f}  {}".format(p, "*" if final and p < ALPHA else "")
        print(line)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", nargs="?", default="results")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--model", help="select one model from historical mixed results")
    parser.add_argument("--repeat", type=int, default=3,
                        help="required completed trials per condition (default: 3)")
    parser.add_argument("--condition", nargs="+", choices=sorted(CONDITIONS),
                        default=list(CONDITIONS), help="expected main conditions; default: all nine")
    parser.add_argument("--final", action="store_true",
                        help="final analysis; requires every planned bug to have complete matched cells")
    parser.add_argument("--sample", default=os.path.join(os.path.dirname(__file__),
                        os.pardir, "data", "experiment_sample.json"),
                        help="planned sample checked by --final (default: committed 24-bug sample)")
    parser.add_argument("--all-pairs", action="store_true",
                        help="also print the 36 exploratory pairs, uncorrected")
    parser.add_argument("--ablation", action="store_true",
                        help="test promotion-on against promotion-off, per iraware condition")
    args = parser.parse_args(argv)
    if args.repeat < 1:
        parser.error("--repeat must be positive")

    try:
        runs = load_experiment_runs(args.directory, args.model)
    except ExperimentMismatch as e:
        parser.error(str(e))
    if not runs:
        print(f"no run records in {args.directory}", file=sys.stderr)
        return 2

    if args.ablation:
        # The ablation question is not "which condition wins" but "does turning
        # promote-operands off change this condition's outcome" -- a comparison
        # of the same condition against itself across the promotion axis. The
        # two arms live in different files, so relabel one arm before pairing
        # (METHODOLOGY.md section 4; docs/IMPLEMENTATION.md Blocker 7).
        ablation_conditions = tuple(c for c in args.condition if c.startswith("iraware"))
        if not ablation_conditions:
            parser.error("--ablation requires an iraware condition")
        on_runs, on_coverage = select_complete_runs(
            runs, repeats=args.repeat, conditions=ablation_conditions)
        off_runs, off_coverage = select_complete_runs(
            runs, repeats=args.repeat, conditions=ablation_conditions, promotion=False)
        matched_bugs = set(on_coverage["complete_bugs"]) & set(off_coverage["complete_bugs"])
        outcomes = per_bug_outcomes([r for r in on_runs if str(r["bug_id"]) in matched_bugs])
        off = per_bug_outcomes([r for r in off_runs if str(r["bug_id"]) in matched_bugs])
        coverage = {"promotion_on": on_coverage, "promotion_off": off_coverage,
                    "complete_bugs": sorted(matched_bugs)}
        if not off:
            print(f"no --no-promotion run records in {args.directory}", file=sys.stderr)
            return 2
        family = []
        for condition in sorted(off):
            key = f"{condition} [no-promotion]"
            outcomes[key] = off[condition]
            family.append((condition, key,
                           "ablation: does promote-operands change this condition?"))
    else:
        matched, coverage = select_complete_runs(
            runs, repeats=args.repeat, conditions=args.condition)
        outcomes = per_bug_outcomes(matched)
        family = list(PRIMARY)

    if args.final:
        from examples.repair_experiment import sample_bug_ids
        planned = set(sample_bug_ids(args.sample))
        missing = planned - set(coverage["complete_bugs"])
        if missing:
            parser.error("Final analysis is incomplete; missing matched bugs: " + ", ".join(sorted(missing)))
        if args.repeat != 3:
            parser.error("The registered final analysis requires --repeat 3")
        if not args.ablation and set(args.condition) != set(CONDITIONS):
            parser.error("The registered final main analysis requires all nine conditions")
        # Final rates concern the planned sample, not extra diagnostic bugs.
        outcomes = {condition: {bug: fixed for bug, fixed in values.items() if bug in planned}
                    for condition, values in outcomes.items()}
        coverage["excluded_extra_bugs"] = sorted(set(coverage["complete_bugs"]) - planned)
        coverage["complete_bugs"] = sorted(planned)

    primary = []
    for better, worse, question in family:
        row = compare(outcomes, better, worse)
        if row is None:
            print(f"skipped {better} vs {worse}: no overlapping runs", file=sys.stderr)
            continue
        row["question"] = question
        primary.append(row)

    # Missing comparisons do not shrink the registered multiple-testing family.
    padded = [r["p_one_sided"] for r in primary] + [1.0] * (len(family) - len(primary))
    adjusted = benjamini_hochberg(padded)[:len(primary)]
    for row, p in zip(primary, adjusted):
        row["p_bh"] = round(p, 6)

    exploratory: List[dict] = []
    if args.all_pairs:
        named = {(r["better"], r["worse"]) for r in primary}
        named |= {(r["worse"], r["better"]) for r in primary}
        conditions = sorted(outcomes)
        for i, a in enumerate(conditions):
            for b_name in conditions[i + 1:]:
                if (a, b_name) in named or (b_name, a) in named:
                    continue
                row = compare(outcomes, a, b_name)
                if row is not None:
                    exploratory.append(row)

    if args.json:
        print(json.dumps({
            "alpha": ALPHA,
            "test": "McNemar exact (binomial), one-sided in the preregistered direction",
            "correction": "Benjamini-Hochberg over the primary family",
            "unit": "one bug, outcome = pass@k",
            "repeat_count": args.repeat,
            "analysis_status": "final" if args.final else "interim_descriptive",
            "coverage": coverage,
            "registered_family_size": len(family),
            "primary": primary,
            "exploratory": exploratory,
        }, indent=2))
        return 0

    label = "promotion ablation" if args.ablation else "main sweep"
    print(f"McNemar exact, one-sided, alpha = {ALPHA} ({label})")
    if not args.final:
        print("INTERIM DESCRIPTIVE CHECK: p-values are diagnostic, not a stopping rule or final inference.")
    print(f"Matched bugs: {len(coverage['complete_bugs'])}; completed trials per condition: {args.repeat}")
    print("b = fixed only by the hypothesised-better condition; "
          "c = fixed only by the other\n")
    print("PRIMARY (preregistered, Benjamini-Hochberg corrected together)")
    print_rows(primary, adjusted, final=args.final)
    for row in primary:
        print(f"  {row['better']} vs {row['worse']}: {row['question']}")

    if exploratory:
        print("\nEXPLORATORY (not preregistered, uncorrected -- descriptive only, "
              "do not read p-values here as evidence)")
        print_rows(exploratory)

    if primary and all(r["discordant"] < 5 for r in primary):
        print("\nNOTE: every primary comparison has fewer than 5 discordant bugs. "
              "No result here can reach p < 0.05 one-sided regardless of "
              "direction; report the counts, not significance.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
