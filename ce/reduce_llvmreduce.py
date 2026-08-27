"""STEP 5b -- Shrinker #2 (second baseline): LLVM's own `llvm-reduce`.

WHY THIS EXISTS
---------------
``reduce_generic.py`` is a deliberately dumb control group: line-level ddmin
with zero LLVM knowledge. That is honest, but it is also attackable. A
reviewer put it exactly right (docs/IMPLEMENTATION.md Blocker 5):

    "line-level ddmin on .ll obviously produces invalid code -- that's a
    strawman."

They have a point: in our sample run, 176 of 183 generic-reducer attempts
produced unparseable IR and were rejected outright. That baseline's weakness
conflates two different things an LLM-repair experiment might be measuring:

    (a) does the reducer understand LLVM IR at all (functions, basic blocks,
        instructions, well-formedness)?
    (b) does the reducer understand the COUNTEREXAMPLE -- that ``src``/``tgt``
        are one function twice, that a violation has SSA dependencies, that
        the alive2 trace says which values and blocks actually mattered?

``reduce_generic`` fails at (a), so failing at (b) too tells us nothing about
whether (b) specifically is what matters. This module adds a reducer that
passes (a) -- ``llvm-reduce`` ships with LLVM, and its built-in passes
(function/block/instruction/operand-level reduction) only ever produce
well-formed IR -- while still failing at (b): it has no idea the two files
are related, and no idea what a "counterexample" is. It only knows "the test
script said yes or no" (see ``_llvmreduce_test.py``).

That isolates the variable ``reduce_iraware.py`` is actually claimed to add:
not "understands IR" (both `llvmreduce` and `iraware` do), but
"counterexample-aware" (only `iraware` does -- tandem editing, dependency
closure, counterexample-seeded search; see that file's docstring).

HOW THE INTEGRATION WORKS
--------------------------
``llvm-reduce`` reduces **one file** against **one opaque interestingness
test** -- it has no native concept of a src/tgt pair. This module supplies
that pairing entirely from the outside, by running it twice:

    1. reduce ``src``, holding ``tgt`` fixed at its ORIGINAL text
    2. reduce ``tgt``, holding ``src`` fixed at the ALREADY-REDUCED result of
       step 1 (chaining, not independent -- a smaller ``src`` can permit
       removing more of ``tgt``, e.g. when a divergence only depended on a
       ``src`` region step 1 already deleted)
    3. re-verify the pair TOGETHER (both sides reduced) before accepting it,
       since steps 1-2 only ever checked one side against a fixed partner

This sequencing is an integration choice we made, not a property of
``llvm-reduce`` itself -- exactly the point: the tool contributes zero
awareness of the relationship between the two files, only IR-valid
single-file reduction.

WHY EXTERNAL CALL COUNTING
---------------------------
Every candidate ``llvm-reduce`` considers spawns ``_llvmreduce_test.py`` as a
**separate subprocess**, which calls alive-tv itself. Those calls happen
outside this process and can't go through ``Oracle.check()`` directly, so
each side's test-script log gets tallied and merged into the real ``Oracle``
via ``record_external`` -- otherwise `.stats()` would silently undercount the
true verifier cost this baseline spent, breaking the "same recorded budget
for every condition" fairness rule.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from typing import Dict, Optional, Tuple

from .oracle import Oracle
from .reduction import Reduction, make_reduction, _Timer

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_TEST_SCRIPT = os.path.join(_THIS_DIR, "_llvmreduce_test.py")

#: Safety-net wall-clock cap per side. Not derived from the oracle budget --
#: the interestingness test already self-limits via CE_LLVMREDUCE_MAX_CALLS,
#: so this only guards against llvm-reduce itself hanging or an unbounded
#: budget (`max_calls=None`) running long.
DEFAULT_SUBPROCESS_TIMEOUT = 600.0


def default_llvm_reduce() -> Optional[str]:
    """Resolve the `llvm-reduce` binary: explicit override, else alongside
    `opt` in the same LLVM build (it's a default-built tool, no extra
    CMake/build step needed -- see docs/IMPLEMENTATION.md Blocker 5)."""
    explicit = os.environ.get("LAB_LLVM_LLVM_REDUCE")
    if explicit:
        return explicit
    build_dir = os.environ.get("LAB_LLVM_BUILD_DIR")
    if build_dir:
        candidate = os.path.join(build_dir, "bin", "llvm-reduce")
        if os.path.exists(candidate):
            return candidate
    return None


def _reduce_one_side(
    text: str, other_text: str, side: str, oracle: Oracle,
    llvm_reduce: str, timeout: float,
) -> Tuple[str, Dict[str, object]]:
    """Run `llvm-reduce` on `text` (the `side` file), holding `other_text`
    fixed. Returns `(reduced_text, external_stats)`; the caller merges
    `external_stats` into `oracle` via `record_external`."""
    with tempfile.TemporaryDirectory() as d:
        input_path = os.path.join(d, f"{side}.ll")
        other_path = os.path.join(d, f"other_{side}.ll")
        # llvm-reduce has no -o/--output flag; it always writes its result to
        # `reduced.ll` in the process's CWD (verified against the real
        # binary), hence `cwd=d` below rather than an explicit output path.
        out_path = os.path.join(d, "reduced.ll")
        log_path = os.path.join(d, f"{side}.log")
        with open(input_path, "w", encoding="utf-8") as f:
            f.write(text)
        with open(other_path, "w", encoding="utf-8") as f:
            f.write(other_text)
        open(log_path, "w", encoding="utf-8").close()

        env = os.environ.copy()
        env["CE_LLVMREDUCE_SIDE"] = side
        env["CE_LLVMREDUCE_OTHER_FILE"] = other_path
        env["CE_LLVMREDUCE_LOG"] = log_path
        env["CE_LLVMREDUCE_ALIVE_TV"] = (
            oracle.alive_tv or os.environ.get("LAB_LLVM_ALIVE_TV", "")
        )
        env["CE_LLVMREDUCE_STRICTNESS"] = oracle.strictness
        env["CE_LLVMREDUCE_TARGET_FUNCTION"] = oracle.target.function or ""
        env["CE_LLVMREDUCE_TARGET_ERROR_CLASS"] = oracle.target.error_class or ""
        env["CE_LLVMREDUCE_TARGET_KIND"] = oracle.target.kind
        env["CE_LLVMREDUCE_EXTRA_ARGS"] = " ".join(oracle.extra_args)
        env["CE_LLVMREDUCE_TIMEOUT"] = str(oracle.timeout)
        if oracle.max_calls is not None:
            env["CE_LLVMREDUCE_MAX_CALLS"] = str(
                max(oracle.max_calls - oracle.calls, 0)
            )

        try:
            subprocess.run(
                [
                    llvm_reduce, f"--test={sys.executable}",
                    f"--test-arg={_TEST_SCRIPT}",
                    "-j", "1",  # sequential test invocations -- keeps the
                                # log-based call counting and budget check race-free
                    input_path,
                ],
                env=env, cwd=d, timeout=timeout, capture_output=True, check=True,
            )
            with open(out_path, encoding="utf-8") as f:
                reduced = f.read()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                FileNotFoundError, OSError):
            reduced = text  # llvm-reduce failed/timed out: keep this side as-is

        stats = {"calls": 0, "accepted": 0, "rejected": 0, "seconds": 0.0}
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                stats["calls"] += 1
                stats["accepted" if rec.get("ok") else "rejected"] += 1
                stats["seconds"] += rec.get("seconds", 0.0)
        return reduced, stats


def reduce_llvmreduce(
    src: str, tgt: str, oracle: Oracle, *,
    llvm_reduce: Optional[str] = None,
    subprocess_timeout: float = DEFAULT_SUBPROCESS_TIMEOUT,
) -> Reduction:
    """The second baseline: real IR-valid reduction, zero counterexample
    awareness. See module docstring for the two-pass integration design."""
    binary = llvm_reduce or default_llvm_reduce()
    if not binary:
        return make_reduction(
            "llvmreduce", src, tgt, src, tgt,
            oracle_stats=oracle.stats(),
            error=(
                "llvm-reduce binary not found (set LAB_LLVM_LLVM_REDUCE, or "
                "build it under $LAB_LLVM_BUILD_DIR/bin/llvm-reduce -- it is "
                "a default ninja target alongside opt, no extra CMake flag "
                "needed)"
            ),
        )

    with _Timer() as timer:
        reduced_src, stats_src = _reduce_one_side(
            src, tgt, "src", oracle, binary, subprocess_timeout
        )
        oracle.record_external(**stats_src)

        reduced_tgt, stats_tgt = _reduce_one_side(
            tgt, reduced_src, "tgt", oracle, binary, subprocess_timeout
        )
        oracle.record_external(**stats_tgt)

        # Steps above only ever checked one side against a fixed partner;
        # confirm the fully-reduced pair still reproduces together before
        # accepting it (mirrors reduce_generic.py's own final safety check).
        if not oracle.check(reduced_src, reduced_tgt):
            reduced_src, reduced_tgt = src, tgt

    passes = []
    if reduced_src != src:
        passes.append("llvm-reduce-src")
    if reduced_tgt != tgt:
        passes.append("llvm-reduce-tgt")

    return make_reduction(
        "llvmreduce", src, tgt, reduced_src, reduced_tgt,
        passes_applied=passes,
        oracle_stats=oracle.stats(),
        seconds=timer.elapsed,
    )
