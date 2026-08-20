"""Command-line access to everything, for poking at it by hand.

    python3 -m ce.cli <command> <src.ll> <tgt.ll>

FOUR COMMANDS
-------------
``check``
    Run Alive2 on a pair of files and print what we understood from its
    output.  Use this first if something looks wrong -- it tells you whether
    the problem is Alive2, our parsing, or something later.

``reduce``
    Shrink a counterexample with one strategy and print the before/after
    sizes.  ``--strategy iraware`` (smart) or ``--strategy generic`` (dumb).

``feedback``
    Show the exact message an AI model would receive under one condition.
    Handy for eyeballing whether the message is actually readable.

``compare``
    Run *all six* conditions on the same pair and print a comparison table.
    This is the one that generates experiment data.

A GOOD FIRST COMMAND TO TRY
---------------------------
::

    python3 -m ce.cli compare data/samples/poison.src.ll data/samples/poison.tgt.ll

NOTE ON EXIT CODES
------------------
``check`` exits 1 when the transformation does **not** verify.  That is not an
error -- for us, "does not verify" is the interesting case.  Exit code 2 means
something actually went wrong (Alive2 missing, file unreadable, and so on).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional, Sequence

from .alive import parse_extra_args, run_alive_tv
from .feedback import CONDITIONS, MATRIX_LETTERS, build_feedback
from .oracle import establish
from .reduce_generic import reduce_generic
from .reduce_iraware import reduce_iraware


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("src", help="source (pre-transformation) .ll file")
    p.add_argument("tgt", help="target (post-transformation) .ll file")
    p.add_argument("--alive-tv", default=os.environ.get("LAB_LLVM_ALIVE_TV"),
                   help="path to alive-tv [$LAB_LLVM_ALIVE_TV]")
    p.add_argument("--extra-args", default="",
                   help="extra alive-tv arguments, e.g. '-src-unroll=8 -tgt-unroll=8'")
    p.add_argument("--timeout", type=float, default=120.0)


def _add_reduce_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("--budget", type=int, default=400,
                   help="max alive-tv calls per reduction (0 = unlimited)")
    p.add_argument("--strictness", default="error_class",
                   choices=["any_failure", "error_class", "error_class_and_kind"],
                   help="what the oracle requires a reduction to preserve")
    p.add_argument("--no-promotion", action="store_true",
                   help="disable operand promotion (the generalising pass)")


def cmd_check(args) -> int:
    run = run_alive_tv(_read(args.src), _read(args.tgt),
                       parse_extra_args(args.extra_args),
                       alive_tv=args.alive_tv, timeout=args.timeout)
    if run.tool_error:
        print(f"error: {run.tool_error}", file=sys.stderr)
        return 2
    print(json.dumps({
        "verified": run.verified,
        "correct": run.num_correct,
        "incorrect": run.num_incorrect,
        "failed_to_prove": run.num_failed_to_prove,
        "errors": run.num_errors,
        "functions": [
            {
                "name": f.name,
                "verified": f.verified,
                "error_class": f.error_class,
                "violated_property": f.violated_property,
                "example": [str(a) for a in f.example],
                "executed_blocks": f.executed_blocks(),
                "src_value": f.src_value,
                "tgt_value": f.tgt_value,
            }
            for f in run.functions
        ],
    }, indent=2))
    return 0 if run.verified else 1


def cmd_reduce(args) -> int:
    src, tgt = _read(args.src), _read(args.tgt)
    budget = args.budget or None
    _, violation, oracle = establish(
        src, tgt, parse_extra_args(args.extra_args),
        alive_tv=args.alive_tv, timeout=args.timeout,
        strictness=args.strictness, max_calls=budget,
    )
    if oracle is None or violation is None:
        print("nothing to reduce: the transformation verifies (or alive-tv failed)",
              file=sys.stderr)
        return 2

    if args.strategy == "generic":
        result = reduce_generic(src, tgt, oracle)
    else:
        result = reduce_iraware(src, tgt, oracle, violation,
                                allow_promotion=not args.no_promotion)

    if args.out:
        os.makedirs(args.out, exist_ok=True)
        for name, text in (("reduced.src.ll", result.src), ("reduced.tgt.ll", result.tgt)):
            with open(os.path.join(args.out, name), "w", encoding="utf-8") as f:
                f.write(text)
        with open(os.path.join(args.out, "reduction.json"), "w", encoding="utf-8") as f:
            json.dump(result.summary(), f, indent=2)

    print(json.dumps(result.summary(), indent=2))
    if not args.quiet:
        print("\n--- reduced source ---")
        print(result.src.rstrip())
        print("\n--- reduced target ---")
        print(result.tgt.rstrip())
    return 0


def cmd_feedback(args) -> int:
    fb = build_feedback(
        _read(args.src), _read(args.tgt), args.condition,
        extra_args=parse_extra_args(args.extra_args),
        alive_tv=args.alive_tv, timeout=args.timeout,
        oracle_budget=args.budget or None,
        oracle_strictness=args.strictness,
        allow_promotion=not args.no_promotion,
        bug_type=args.bug_type,
    )
    if args.json:
        print(json.dumps({"summary": fb.summary(), "text": fb.text}, indent=2))
    else:
        print(json.dumps(fb.summary(), indent=2), file=sys.stderr)
        print(fb.text)
    return 2 if fb.error else 0


def cmd_compare(args) -> int:
    src, tgt = _read(args.src), _read(args.tgt)
    conditions: List[str] = args.conditions or [
        MATRIX_LETTERS[k] for k in sorted(MATRIX_LETTERS)
    ]
    rows = []
    for cond in conditions:
        fb = build_feedback(
            src, tgt, cond,
            extra_args=parse_extra_args(args.extra_args),
            alive_tv=args.alive_tv, timeout=args.timeout,
            oracle_budget=args.budget or None,
            oracle_strictness=args.strictness,
            allow_promotion=not args.no_promotion,
            bug_type=args.bug_type,
        )
        rows.append(fb.summary())
        if args.dump_dir:
            os.makedirs(args.dump_dir, exist_ok=True)
            with open(os.path.join(args.dump_dir, f"{cond}.txt"), "w", encoding="utf-8") as f:
                f.write(fb.text)

    record = {"src": args.src, "tgt": args.tgt, "conditions": rows}
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2)

    if args.json:
        print(json.dumps(record, indent=2))
    else:
        _print_table(rows)
    return 0


_TABLE_COLUMNS = (
    ("condition", 22),
    ("prompt_tokens_est", 10),
    ("shown_instructions", 8),
    ("shown_lines", 7),
    ("reduction_ratio_instructions", 10),
    ("oracle_calls", 8),
    ("seconds", 8),
)


def _print_table(rows: Sequence[dict]) -> None:
    header = "  ".join(name[:width].ljust(width) for name, width in _TABLE_COLUMNS)
    print(header)
    print("-" * len(header))
    for row in rows:
        cells = []
        for name, width in _TABLE_COLUMNS:
            val = row.get(name, "-")
            if isinstance(val, float):
                val = f"{val:.3f}"
            cells.append(str(val)[:width].ljust(width))
        print("  ".join(cells))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ce",
        description="IR-aware minimization and structuring of Alive2 counterexamples",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("check", help="run alive-tv and print the parsed result")
    _add_common(p)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("reduce", help="reduce a counterexample")
    _add_common(p)
    _add_reduce_opts(p)
    p.add_argument("--strategy", default="iraware", choices=["generic", "iraware"])
    p.add_argument("--out", help="directory to write the reduced pair into")
    p.add_argument("--quiet", action="store_true", help="print metrics only")
    p.set_defaults(func=cmd_reduce)

    p = sub.add_parser("feedback", help="render the LLM message for one condition")
    _add_common(p)
    _add_reduce_opts(p)
    p.add_argument("--condition", default="iraware-structured",
                   help=f"one of {sorted(CONDITIONS)} or a matrix letter A-F")
    p.add_argument("--bug-type", default="miscompilation")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_feedback)

    p = sub.add_parser("compare", help="run every condition on one pair")
    _add_common(p)
    _add_reduce_opts(p)
    p.add_argument("--conditions", nargs="*", help="default: the full A-F matrix")
    p.add_argument("--bug-type", default="miscompilation")
    p.add_argument("--dump-dir", help="write each condition's rendered message here")
    p.add_argument("--out", help="write the JSON record here")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_compare)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
