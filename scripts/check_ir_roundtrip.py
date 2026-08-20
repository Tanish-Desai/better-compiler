#!/usr/bin/env python3
"""SAFETY CHECK: can we read and re-print every real reproducer unchanged?

WHAT THIS CHECKS AND WHY IT MATTERS
-----------------------------------
Our IR reader (``ce/irmodel.py``) only understands a fraction of LLVM IR.  Real
bug reproducers are full of things we never modelled: vector types, metadata,
attribute groups, ``byval``, and so on.

So the shrinker rests on one safety rule:

    **anything we do not understand must survive parse-then-print unchanged.**

If that rule breaks, we would silently corrupt code we only meant to shrink,
and every result after that point would be garbage.

This script tests the rule the only way that means anything: against all 1462
real reproducers in the benchmark dataset.  For each one it parses the IR,
prints it back out, and compares byte for byte.

    Expected result:  1462 ok, 0 mismatched, 0 crashed

It has already earned its keep -- it caught three real bugs (dropped blank
lines, dropped comments, and instructions at column 0 being re-indented).

Usage::

    python3 scripts/check_ir_roundtrip.py [dataset_dir]

Exits 1 if anything fails to round-trip, so it can be used in CI.
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from ce.irmodel import parse_module  # noqa: E402


def main(argv):
    dataset = (
        argv[1] if len(argv) > 1
        else os.environ.get("LAB_DATASET_DIR")
        or os.path.join(os.path.dirname(__file__), os.pardir, "llvm-apr-benchmark", "dataset")
    )
    if not os.path.isdir(dataset):
        print(f"dataset directory not found: {dataset}", file=sys.stderr)
        return 2

    stats = Counter()
    failures = []
    for name in sorted(os.listdir(dataset)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(dataset, name), "r", encoding="utf-8") as f:
            issue = json.load(f)
        stats["issues"] += 1
        for group in issue.get("tests", []):
            for test in group.get("tests", []):
                body = test.get("test_body")
                if not body:
                    continue
                stats["reproducers"] += 1
                try:
                    out = parse_module(body).text()
                except Exception as e:  # noqa: BLE001
                    stats["crashed"] += 1
                    failures.append((name, test.get("test_name"), f"{type(e).__name__}: {e}"))
                    continue
                if out.rstrip("\n") != body.rstrip("\n"):
                    stats["mismatched"] += 1
                    failures.append((name, test.get("test_name"), "round-trip mismatch"))
                else:
                    stats["ok"] += 1

    print(f"issues:      {stats['issues']}")
    print(f"reproducers: {stats['reproducers']}")
    print(f"round-trip:  {stats['ok']} ok, {stats['mismatched']} mismatched, {stats['crashed']} crashed")

    for issue, test, why in failures[:20]:
        print(f"  FAIL {issue} :: {test} :: {why}")
    if len(failures) > 20:
        print(f"  ... and {len(failures) - 20} more")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
