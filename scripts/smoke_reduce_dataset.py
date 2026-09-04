#!/usr/bin/env python3
"""SMOKE TEST: does the smart shrinker survive real, messy LLVM code?

THE PROBLEM THIS WORKS AROUND
-----------------------------
To test the shrinker on a *real* bug we would need real ``opt`` output, and
that means building LLVM at each bug's specific commit -- hours of work per
commit (docs/IMPLEMENTATION.md Blocker 1). That's been done for exactly one
bug so far; this script exercises hundreds of dataset entries at once, each
of which would need its own build, so faking is still the right call here.

So this script fakes the "after" version instead.  It takes a genuine bug
reproducer from the dataset and attaches an ``nsw`` flag that the original code
does not justify, which creates a real (if artificial) miscompilation.

WHAT THIS DOES AND DOES NOT PROVE
---------------------------------
It is **not** the benchmark's actual bug, so this is not a research result.

What it does show is that the shrinker copes with real-world IR -- complicated
control flow, unusual types, hundreds of instructions -- rather than only the
tidy examples we wrote by hand.  The failure mode it is looking for is the
shrinker crashing, producing invalid code, or losing the bug.

Sample output::

    105988.json :: test
      instructions 48 -> 16 (66.7% removed)  oracle_calls=29  1.1s
      violation preserved: True

Usage::

    python3 scripts/smoke_reduce_dataset.py [--limit N] [--min N] [--max N]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))

from ce.alive import run_alive_tv  # noqa: E402
from ce.irmodel import parse_module  # noqa: E402
from ce.oracle import establish  # noqa: E402
from ce.reduce_iraware import reduce_iraware  # noqa: E402


#: Opcodes that accept `nsw`, so all four are usable perturbation sites.
_NSW_OPCODES = ("add", "sub", "mul", "shl")


def perturb(body: str, fn) -> str | None:
    """Attach an unjustified `nsw` to the first integer op that lacks one.

    Two things here are deliberate, both found while measuring why this script
    reports so few cases (docs/IMPLEMENTATION.md Blocker 15):

    **All four `nsw`-capable opcodes, not just `add`.** Restricting to `add`
    passed up most of the dataset: of 453 single-function miscompilation tests
    in the 4-80 instruction range, 71 have a plain `add` but 130 have one of
    add/sub/mul/shl.

    **The flag is inserted with a regex anchored on the `= <opcode>` that
    starts the instruction**, not by replacing the first occurrence of the
    opcode's text. LLVM names results after their operation constantly
    (`%add = add i64 %phi, 1`), and a plain `.replace(opcode + " ", ...)` hits
    the *name* first, emitting `%add nsw = add i64 %phi, 1` -- invalid IR that
    alive-tv rejects, so the case was silently dropped by the `tool_error`
    check in `main` rather than reported. 190 instructions in the dataset are
    shaped that way.
    """
    for inst in fn.instructions():
        if inst.opcode not in _NSW_OPCODES or not inst.result or "nsw" in inst.raw:
            continue
        patched = re.sub(rf"(=\s*){inst.opcode}(\s)",
                         rf"\g<1>{inst.opcode} nsw\g<2>", inst.raw, count=1)
        if patched != inst.raw:
            return body.replace(inst.raw, patched, 1)
    return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dataset", default=os.environ.get("LAB_DATASET_DIR"))
    parser.add_argument("--limit", type=int, default=8, help="cases to report")
    parser.add_argument("--min", type=int, default=10, help="min instructions")
    parser.add_argument("--max", type=int, default=80, help="max instructions")
    parser.add_argument("--budget", type=int, default=250)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)

    if not args.dataset or not os.path.isdir(args.dataset):
        print("dataset directory not found; set LAB_DATASET_DIR", file=sys.stderr)
        return 2

    reported = failures = 0
    for name in sorted(os.listdir(args.dataset)):
        if reported >= args.limit:
            break
        if not name.endswith(".json"):
            continue
        with open(os.path.join(args.dataset, name), encoding="utf-8") as f:
            issue = json.load(f)
        if issue.get("bug_type") != "miscompilation":
            continue

        for group in issue.get("tests", []):
            if reported >= args.limit:
                break
            for test in group.get("tests", []):
                body = test.get("test_body") or ""
                functions = parse_module(body).functions
                if len(functions) != 1:
                    continue
                count = len(list(functions[0].instructions()))
                if not args.min <= count <= args.max:
                    continue
                tgt = perturb(body, functions[0])
                if tgt is None:
                    continue

                run = run_alive_tv(body, tgt, timeout=args.timeout)
                if run.tool_error or run.verified or run.first_violation() is None:
                    continue

                _, violation, oracle = establish(
                    body, tgt, timeout=args.timeout, max_calls=args.budget
                )
                if oracle is None:
                    continue

                result = reduce_iraware(body, tgt, oracle, violation)
                recheck = run_alive_tv(result.src, result.tgt, timeout=args.timeout)
                again = recheck.first_violation()
                preserved = again is not None and again.error_class == violation.error_class

                print(f"{name} :: {test.get('test_name')}")
                print(f"  instructions {result.size_before['instructions']:>4} -> "
                      f"{result.size_after['instructions']:<4} "
                      f"({(result.ratio() or 0) * 100:5.1f}% removed)  "
                      f"oracle_calls={result.oracle_stats['oracle_calls']:<4} "
                      f"{result.seconds:.1f}s")
                print(f"  passes: {', '.join(result.passes_applied) or '(none)'}")
                print(f"  violation preserved: {preserved}"
                      + (f"   error: {result.error}" if result.error else ""))
                if not preserved:
                    failures += 1
                reported += 1
                break

    print(f"\n{reported} cases, {failures} with a lost violation")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
