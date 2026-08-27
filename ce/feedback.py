"""STEP 7 -- The experiment itself: one bug in, one AI message out.

WHAT THIS FILE IS FOR
---------------------
This is where the two independent ideas get combined into the actual
experiment.  We have two knobs:

    knob 1 -- how much shrinking?   raw / generic / llvmreduce / iraware
    knob 2 -- how is it laid out?   plain / structured

Turning both gives a 2x4 grid of eight ways to present the same bug:

                  |  plain                |  structured
    --------------+-----------------------+-----------------------------
    raw           |  raw-plain            |  raw-structured
    generic       |  generic-plain        |  generic-structured
    llvmreduce    |  llvmreduce-plain     |  llvmreduce-structured
    iraware       |  iraware-plain        |  iraware-structured

Plus a ninth, ``baseline``, which gets no counterexample at all -- just
"this is wrong" -- to show what the AI can do with nothing.

``llvmreduce`` (``reduce_llvmreduce.py``) was added for docs/IMPLEMENTATION.md
Blocker 5: ``generic`` is honest but attackable as a strawman (176 of 183
attempts produced invalid IR), so it alone can't tell you whether an
IR-aware-but-not-counterexample-aware reducer would ALSO have helped. The
Blocker-5 comparison holds structure fixed and reads across the ``generic`` /
``llvmreduce`` / ``iraware`` row: only the last one is claimed to understand
the counterexample, and the middle one is what isolates that from merely
"produces valid IR".

WHY A GRID INSTEAD OF JUST "OURS VS THEIRS"?
--------------------------------------------
Because a grid can answer *why* something helped, and a two-way comparison
cannot.  Reading across and down the table tells you:

    does shrinking help?           compare rows
    does layout help?              compare columns
    does LLVM knowledge help
      beyond generic shrinking?    compare the generic row to the iraware row
    do they help more together?    look at iraware-structured vs each alone

That last one matters: it is possible that neither alone does much but the
combination does.

A WARNING ABOUT THE CONDITION LETTERS
-------------------------------------
``context.md`` names the conditions **twice, and the two lists disagree**:

  * section 15 (prose):  A=baseline, B=raw, C=generic, D=iraware,
                         E=structured, F=both
  * section 16 (table):  the clean 2x3 grid above

We implement the section-16 grid, because it is the design that can actually
separate the factors.  Its letters are ``MATRIX_LETTERS``.  Section 15's
letters still resolve, via ``LEGACY_LETTERS``, so old notes do not break.

``llvmreduce-plain``/``llvmreduce-structured`` (added for Blocker 5, after
both letter schemes were written) have **no letter in either scheme** -- they
are a second baseline bolted onto the reduction knob, not part of either
historical grid. Refer to them only by full name.

    >>> Always write the full condition name in reports and papers.
    >>> A bare "condition C" is ambiguous in this project.

ADDING A NEW CONDITION
----------------------
Add a row to ``CONDITIONS`` here.  Nothing else changes -- the repair loop
never checks which strategy it is running.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .alive import AliveRun, FunctionResult, parse_alive_output, run_alive_tv
from .irmodel import measure_pair
from .oracle import Oracle, Violation
from .reduce_generic import reduce_generic
from .reduce_iraware import reduce_iraware
from .reduce_llvmreduce import reduce_llvmreduce
from .reduction import Reduction, make_reduction
from .structured import render_plain, render_structured

REDUCTIONS = ("raw", "generic", "llvmreduce", "iraware")
STRUCTURES = ("plain", "structured")


@dataclass(frozen=True)
class Condition:
    """One cell of the experimental matrix."""

    name: str
    reduction: str
    structure: str
    #: Baseline conditions get no counterexample at all, only pass/fail.
    counterexample: bool = True

    @property
    def structured(self) -> bool:
        return self.structure == "structured"


CONDITIONS: Dict[str, Condition] = {
    "baseline": Condition("baseline", "raw", "plain", counterexample=False),
    **{
        f"{r}-{s}": Condition(f"{r}-{s}", r, s)
        for r in REDUCTIONS
        for s in STRUCTURES
    },
}

#: context.md s16 matrix.
MATRIX_LETTERS = {
    "A": "raw-plain",
    "B": "generic-plain",
    "C": "iraware-plain",
    "D": "raw-structured",
    "E": "generic-structured",
    "F": "iraware-structured",
}

#: context.md s15 prose ordering, kept so the proposal's labels resolve.
LEGACY_LETTERS = {
    "A": "baseline",
    "B": "raw-plain",
    "C": "generic-plain",
    "D": "iraware-plain",
    "E": "raw-structured",
    "F": "iraware-structured",
}


def resolve_condition(name: str, *, legacy: bool = False) -> Condition:
    """Look up a condition by full name or by matrix letter."""
    key = name.strip()
    if key in CONDITIONS:
        return CONDITIONS[key]
    letters = LEGACY_LETTERS if legacy else MATRIX_LETTERS
    upper = key.upper()
    if upper in letters:
        return CONDITIONS[letters[upper]]
    raise KeyError(
        f"unknown condition {name!r}; expected one of {sorted(CONDITIONS)} "
        f"or a letter in {sorted(letters)}"
    )


def estimate_tokens(text: str) -> int:
    """Rough token count (~4 chars/token).

    Deliberately model-agnostic: the experiment compares conditions against
    each other, and every condition is measured with the same ruler. Replace
    with a real tokenizer before quoting absolute costs.
    """
    return (len(text) + 3) // 4


@dataclass
class Feedback:
    """The message for the LLM, plus everything needed to report on it."""

    text: str
    condition: str
    #: None when the transformation verified or alive-tv could not run.
    violation: Optional[FunctionResult] = None
    reduction: Optional[Reduction] = None
    metrics: Dict[str, object] = field(default_factory=dict)
    error: Optional[str] = None

    def summary(self) -> dict:
        out = {"condition": self.condition, **self.metrics}
        if self.reduction is not None:
            out.update(self.reduction.summary())
        if self.error:
            out["error"] = self.error
        return out


_NO_CE_TEXT = (
    "The transformation performed by this pass is incorrect: alive2 reports "
    "that the optimized IR does not refine the original IR."
)


def build_feedback(
    src: str,
    tgt: str,
    condition: str = "iraware-structured",
    *,
    extra_args: Sequence[str] = (),
    alive_tv: Optional[str] = None,
    bug_type: str = "miscompilation",
    timeout: float = 120.0,
    oracle_budget: Optional[int] = 400,
    oracle_strictness: str = "error_class",
    allow_promotion: bool = True,
    alive_output: Optional[str] = None,
    legacy_letters: bool = False,
) -> Feedback:
    """Produce the feedback message for one src/tgt pair under one condition.

    ``alive_output`` may be supplied to reuse a verification the caller already
    performed (``llvm_helper.alive2_check``'s ``log``); the reducers still need
    a live ``alive-tv`` because reduction is oracle-gated by definition.
    """
    cond = resolve_condition(condition, legacy=legacy_letters)

    run: AliveRun = (
        parse_alive_output(alive_output)
        if alive_output is not None
        else run_alive_tv(src, tgt, extra_args, alive_tv=alive_tv, timeout=timeout)
    )
    if run.tool_error:
        return Feedback(run.raw or run.tool_error, cond.name, error=run.tool_error)

    violation = run.first_violation()
    if violation is None:
        return Feedback(
            "alive2 reports the transformation is correct.",
            cond.name,
            metrics={"verified": True},
        )

    if not cond.counterexample:
        return Feedback(
            _NO_CE_TEXT,
            cond.name,
            violation=violation,
            metrics=_metrics(_NO_CE_TEXT, src, tgt),
        )

    red_src, red_tgt, reduction = _apply_reduction(
        cond, src, tgt, violation,
        extra_args=extra_args, alive_tv=alive_tv, timeout=timeout,
        budget=oracle_budget, strictness=oracle_strictness,
        allow_promotion=allow_promotion,
    )

    # Re-verify the reduced pair so the rendered trace describes the IR the LLM
    # is actually shown, not the original. Without this the structured view
    # would cite values the reduced IR no longer defines.
    shown = violation
    if (red_src, red_tgt) != (src, tgt):
        rerun = run_alive_tv(red_src, red_tgt, extra_args, alive_tv=alive_tv, timeout=timeout)
        refreshed = rerun.violation_for(violation.name) or rerun.first_violation()
        if refreshed is not None:
            shown = refreshed
        else:
            # The reduced pair no longer reproduces; fall back rather than
            # emit feedback that does not match its own IR.
            red_src, red_tgt, shown = src, tgt, violation
            if reduction is not None:
                reduction.error = "reduced pair failed re-verification; reverted"

    text = (
        render_structured(shown, red_src, red_tgt, bug_type=bug_type)
        if cond.structured
        else render_plain(shown, red_src, red_tgt)
    )
    return Feedback(
        text=text,
        condition=cond.name,
        violation=shown,
        reduction=reduction,
        metrics=_metrics(text, red_src, red_tgt, original=(src, tgt)),
    )


def _apply_reduction(
    cond: Condition,
    src: str,
    tgt: str,
    violation: FunctionResult,
    *,
    extra_args: Sequence[str],
    alive_tv: Optional[str],
    timeout: float,
    budget: Optional[int],
    strictness: str,
    allow_promotion: bool,
):
    if cond.reduction == "raw":
        return src, tgt, None

    oracle = Oracle(
        target=Violation.from_result(violation),
        extra_args=extra_args,
        alive_tv=alive_tv,
        timeout=timeout,
        strictness=strictness,
        max_calls=budget,
    )
    if cond.reduction == "generic":
        reduction = reduce_generic(src, tgt, oracle)
    elif cond.reduction == "llvmreduce":
        reduction = reduce_llvmreduce(src, tgt, oracle)
    else:
        reduction = reduce_iraware(
            src, tgt, oracle, violation, allow_promotion=allow_promotion
        )
    if reduction.error:
        return src, tgt, reduction
    return reduction.src, reduction.tgt, reduction


def _metrics(text: str, src: str, tgt: str, original=None) -> Dict[str, object]:
    out: Dict[str, object] = {
        "prompt_chars": len(text),
        "prompt_tokens_est": estimate_tokens(text),
        **{f"shown_{k}": v for k, v in measure_pair(src, tgt).items()},
    }
    if original is not None:
        out.update({f"original_{k}": v for k, v in measure_pair(*original).items()})
    return out


def build_feedback_from_check(
    check_log: dict,
    condition: str = "iraware-structured",
    **kwargs,
) -> Feedback:
    """Adapter for ``llvm_helper.alive2_check``'s ``{"src","tgt","log"}`` dict.

    This is the single integration point with the benchmark: wherever the
    repair loop currently pastes ``log`` into the prompt, it calls this instead.
    """
    return build_feedback(
        check_log["src"],
        check_log["tgt"],
        condition,
        alive_output=check_log.get("log"),
        **kwargs,
    )
