#!/usr/bin/env python3
"""Interestingness-test shim, invoked BY `llvm-reduce` once per candidate.

NOT MEANT TO BE IMPORTED. `llvm-reduce` runs this as `<python> <this file>
<candidate-file>` (see `reduce_llvmreduce.py`'s `--test`/`--test-arg` wiring)
and treats its exit code as the entire signal: 0 means "still interesting,
keep reducing"; nonzero means "discard this candidate." It never sees why.

That opacity is the point (docs/IMPLEMENTATION.md Blocker 5): everything
oracle/counterexample-related lives entirely on our side of this process
boundary. `llvm-reduce`'s own reduction passes are genuinely IR-aware — they
understand functions, basic blocks, instructions, operands — but they have no
concept of "these two files are a src/tgt pair" or "the same Alive2
violation" beyond the single bit this script returns. That is what makes it a
fair second baseline: IR-valid by construction, counterexample-blind by
construction.

Reads its parameters from environment variables (set by the orchestrator in
`reduce_llvmreduce.py`) rather than argv, since `llvm-reduce`'s `--test-arg`
mechanism only prepends fixed tokens before the candidate path it appends
itself — env vars are simpler than threading everything through that.
"""

from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, os.pardir))

from ce.oracle import Oracle, Violation  # noqa: E402


def _env(name: str, default: str = None):
    value = os.environ.get(name)
    return default if value in (None, "") else value


def main() -> int:
    candidate_path = sys.argv[-1]
    with open(candidate_path, encoding="utf-8") as f:
        candidate_text = f.read()

    other_path = os.environ["CE_LLVMREDUCE_OTHER_FILE"]
    with open(other_path, encoding="utf-8") as f:
        other_text = f.read()

    side = os.environ["CE_LLVMREDUCE_SIDE"]  # "src" or "tgt"
    log_path = os.environ["CE_LLVMREDUCE_LOG"]

    # Honor the same call budget our own Oracle would (CE_LLVMREDUCE_MAX_CALLS
    # is set to the *remaining* budget by the orchestrator), without needing
    # cross-process shared state: the log file IS the call count so far.
    max_calls_env = _env("CE_LLVMREDUCE_MAX_CALLS")
    if max_calls_env is not None:
        try:
            with open(log_path, encoding="utf-8") as f:
                already = sum(1 for _ in f)
        except FileNotFoundError:
            already = 0
        if already >= int(max_calls_env):
            return 1  # budget exhausted: report "boring" without spending a call

    oracle = Oracle(
        target=Violation(
            function=_env("CE_LLVMREDUCE_TARGET_FUNCTION"),
            error_class=_env("CE_LLVMREDUCE_TARGET_ERROR_CLASS"),
            kind=_env("CE_LLVMREDUCE_TARGET_KIND", "unknown"),
        ),
        extra_args=tuple(_env("CE_LLVMREDUCE_EXTRA_ARGS", "").split()),
        alive_tv=_env("CE_LLVMREDUCE_ALIVE_TV"),
        timeout=float(_env("CE_LLVMREDUCE_TIMEOUT", "60.0")),
        strictness=_env("CE_LLVMREDUCE_STRICTNESS", "error_class"),
    )

    if side == "src":
        result = oracle.check(candidate_text, other_text)
    else:
        result = oracle.check(other_text, candidate_text)

    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ok": bool(result),
            "seconds": oracle.seconds,
        }) + "\n")

    return 0 if result else 1


if __name__ == "__main__":
    raise SystemExit(main())
