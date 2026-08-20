"""STEP 3 -- The referee: "after that edit, is it still the same bug?"

WHY THIS FILE IS THE MOST IMPORTANT ONE
---------------------------------------
Shrinking a counterexample is easy if you do not care about correctness -- you
could delete everything and end up with an empty file.  What makes shrinking
*useful* is that the result still demonstrates the same compiler bug.

So the shrinkers here never just delete things and hope.  They work like this::

    1. propose an edit          (delete an instruction, drop a flag, ...)
    2. re-run Alive2 on the result
    3. is it STILL the same bug?
         yes -> keep the edit
         no  -> throw it away and try something else

This file is step 2 and 3.  In program-reduction research this check is
traditionally called the **oracle** -- it is the thing that answers "does this
still count?"

Because every single edit is checked this way, a shrunk counterexample is not
"probably still valid" -- it is *provably* still a counterexample, verified by
the same formal tool that found it in the first place.  That is the difference
between this and simply making the text shorter.

The price is that reduction costs a lot of Alive2 runs, so this class also
counts them (``.calls``, ``.seconds``).  Those counts are experiment data:
one of the research questions is how expensive each shrinking strategy is.

WHAT COUNTS AS "THE SAME BUG"?
------------------------------
This turns out not to have one obvious answer, so it is a setting you choose:

``any_failure``
    Any refinement failure at all still occurs.  Weakest; permits the reducer
    to drift onto an unrelated bug in the same function.
``error_class`` (default)
    The same alive2 error class still occurs, in the same function.
``error_class_and_kind``
    As above, and the target-side outcome is still of the same kind (poison
    stays poison, UB stays UB, a wrong concrete value stays a wrong concrete
    value).  Strictest, and the closest to "the same semantic reason".

The default (``error_class``) is a reasonable middle ground.  The weakest
setting lets the shrinker wander off onto a *different* bug that happens to
live in the same function, which would quietly invalidate the experiment --
so if you use it, say so when you write up results.  See
``docs/METHODOLOGY.md``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from .alive import AliveRun, FunctionResult, run_alive_tv

STRICTNESS_LEVELS = ("any_failure", "error_class", "error_class_and_kind")


def outcome_kind(fn: FunctionResult) -> str:
    """Coarse label for what went wrong on the target side.

    Distinguishes the three qualitatively different ways a target can fail to
    refine its source, so the strict oracle can tell them apart.
    """
    value = (fn.tgt_value or "").strip()
    if "poison" in value:
        return "poison"
    if "undef" in value:
        return "undef"
    if "UB triggered" in value or any(a.is_ub for a in fn.tgt_trace):
        return "ub"
    if any(a.is_poison for a in fn.tgt_trace) and not any(a.is_poison for a in fn.src_trace):
        return "poison"
    if value:
        return "value"
    return "unknown"


@dataclass(frozen=True)
class Violation:
    """The violation a reduction must preserve."""

    function: Optional[str]
    error_class: Optional[str]
    kind: str = "unknown"

    @classmethod
    def from_result(cls, fn: FunctionResult) -> "Violation":
        return cls(function=fn.name, error_class=fn.error_class, kind=outcome_kind(fn))

    def describe(self) -> str:
        where = self.function or "<first function>"
        return f"{self.error_class or 'refinement failure'} in {where} ({self.kind})"


@dataclass
class OracleResult:
    """Whether one candidate still exhibits the target violation."""

    ok: bool
    reason: str
    run: Optional[AliveRun] = None
    violation: Optional[FunctionResult] = None

    def __bool__(self) -> bool:
        return self.ok


@dataclass
class Oracle:
    """Oracle-gated ``alive-tv`` runner that also records its own cost.

    The call counters are experiment data, not bookkeeping: RQ4 asks how
    expensive each reduction strategy is, and verifier calls are the dominant
    cost.
    """

    target: Violation
    extra_args: Sequence[str] = ()
    alive_tv: Optional[str] = None
    timeout: float = 60.0
    strictness: str = "error_class"
    #: Give up after this many calls; ``None`` means unlimited.
    max_calls: Optional[int] = None

    calls: int = field(default=0, init=False)
    accepted: int = field(default=0, init=False)
    rejected: int = field(default=0, init=False)
    tool_failures: int = field(default=0, init=False)
    seconds: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        if self.strictness not in STRICTNESS_LEVELS:
            raise ValueError(
                f"strictness must be one of {STRICTNESS_LEVELS}, got {self.strictness!r}"
            )

    @property
    def exhausted(self) -> bool:
        return self.max_calls is not None and self.calls >= self.max_calls

    def check(self, src: str, tgt: str) -> OracleResult:
        """Run alive-tv on a candidate and judge whether it still counts."""
        if self.exhausted:
            return OracleResult(False, "oracle call budget exhausted")

        start = time.time()
        run = run_alive_tv(
            src, tgt, self.extra_args, alive_tv=self.alive_tv, timeout=self.timeout
        )
        self.seconds += time.time() - start
        self.calls += 1

        if run.tool_error:
            self.tool_failures += 1
            self.rejected += 1
            return OracleResult(False, f"alive-tv failed: {run.tool_error}", run)

        # An unparseable or non-type-checking candidate is the normal outcome
        # of an over-aggressive edit, so it is a rejection, not an error.
        if run.num_errors and not run.num_incorrect:
            self.rejected += 1
            return OracleResult(False, "alive2 reported an error (invalid IR?)", run)

        found = self._match(run)
        if found is None:
            self.rejected += 1
            return OracleResult(False, "target violation no longer reproduces", run)

        self.accepted += 1
        return OracleResult(True, "violation preserved", run, found)

    __call__ = check

    def _match(self, run: AliveRun) -> Optional[FunctionResult]:
        candidates: List[FunctionResult] = [f for f in run.functions if not f.verified]
        if not candidates:
            return None
        if self.strictness == "any_failure":
            return candidates[0]

        for fn in candidates:
            if self.target.function is not None and fn.name != self.target.function:
                continue
            if fn.error_class != self.target.error_class:
                continue
            if self.strictness == "error_class_and_kind" and outcome_kind(fn) != self.target.kind:
                continue
            return fn
        return None

    def stats(self) -> dict:
        return {
            "oracle_calls": self.calls,
            "oracle_accepted": self.accepted,
            "oracle_rejected": self.rejected,
            "oracle_tool_failures": self.tool_failures,
            "oracle_seconds": round(self.seconds, 3),
            "oracle_strictness": self.strictness,
        }


def establish(
    src: str,
    tgt: str,
    extra_args: Sequence[str] = (),
    *,
    alive_tv: Optional[str] = None,
    timeout: float = 120.0,
    strictness: str = "error_class",
    max_calls: Optional[int] = None,
) -> tuple:
    """Verify the original pair really fails, and build an Oracle for it.

    Returns ``(run, violation_result, oracle)``.  ``oracle`` is ``None`` when
    the pair verifies (nothing to reduce) or alive-tv could not run.
    """
    run = run_alive_tv(src, tgt, extra_args, alive_tv=alive_tv, timeout=timeout)
    violation = run.first_violation()
    if run.tool_error or violation is None:
        return run, None, None
    oracle = Oracle(
        target=Violation.from_result(violation),
        extra_args=extra_args,
        alive_tv=alive_tv,
        timeout=timeout,
        strictness=strictness,
        max_calls=max_calls,
    )
    return run, violation, oracle
