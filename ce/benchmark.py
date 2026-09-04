"""STEP 8 -- Plug all of this into the existing repair loop.

WHAT THIS FILE IS FOR
---------------------
The ``llvm-apr-benchmark`` repo already has a working AI repair loop: it asks a
model for a patch, builds LLVM, runs the tests, and if they fail it pastes the
failure into the next prompt and tries again.

Our entire research changes **one step** of that loop: the paste.

Here is where the benchmark produces the thing we want to intercept::

    env.check_full()
      -> llvm_helper.verify_test_group
        -> llvm_helper.verify_dispatch
          -> llvm_helper.alive2_check  ->  {"src", "tgt", "log"}

and its example agent then dumps that dictionary straight into the prompt.
This file provides a **drop-in replacement** for that step:

    from ce.benchmark import normalize_feedback

    res, log = env.check_full()
    if not res:
        feedback = normalize_feedback(log, condition="iraware-structured")
        messages.append({"role": "user", "content": feedback.text})

Not every failure is a miscompilation.  A patch might fail to compile, or
crash, or break an unrelated regression test -- none of which produce a
counterexample.  ``normalize_feedback`` passes those straight through
unchanged, so a repair loop can safely route *all* of its feedback through
this one function.

ALSO IN HERE: THE EXPERIMENT RECORD
-----------------------------------
:class:`RunLog` and :class:`Iteration` record what happened on each attempt --
how many turns it took, how many tokens, how many Alive2 calls, whether it was
finally fixed.  This is the raw data the results table is computed from, and
``summarize()`` at the bottom turns a directory of them into that table.

A DELIBERATE NON-DEPENDENCY
---------------------------
This file does **not** import anything from the benchmark.  The benchmark's
modules read ``LAB_*`` environment variables the moment you import them, which
would make our package impossible to test on a machine without LLVM set up.
The only thing we depend on is the *shape* of that dictionary.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .feedback import Feedback, build_feedback_from_check, resolve_condition

#: Keys that identify an alive2 result dict from ``llvm_helper.alive2_check``.
_ALIVE_KEYS = ("src", "tgt", "log")


def is_alive2_log(log: Any) -> bool:
    """True when ``log`` is an ``alive2_check`` result rather than plain text."""
    return isinstance(log, dict) and all(k in log for k in _ALIVE_KEYS)


def first_failed_test(log: Any) -> Optional[dict]:
    """Mirror of ``llvm_helper.get_first_failed_test`` without the import."""
    if not isinstance(log, list):
        return None
    for entry in log:
        if isinstance(entry, dict) and not entry.get("result"):
            return entry
    return None


def normalize_feedback(
    log: Any,
    condition: str = "iraware-structured",
    *,
    bug_type: str = "miscompilation",
    max_log_size: int = 1_000_000_000,
    **kwargs,
) -> Feedback:
    """Turn one ``check_fast``/``check_full`` result into an LLM message.

    Drop-in for the example agent's ``normalize_feedback``: same input, and
    ``.text`` is the same kind of string.  When the failure is not an alive2
    refinement failure -- a build error, a crash, a lit regression -- there is
    no counterexample to reduce and the original text is passed through, so a
    repair loop can route *all* its feedback through this one call.
    """
    failed = first_failed_test(log)
    inner = failed.get("log") if failed else log

    if not is_alive2_log(inner):
        text = inner if isinstance(inner, str) else json.dumps(failed or log, indent=2)
        if len(text) > max_log_size:
            text = text[:max_log_size] + "\n<Truncated>..."
        return Feedback(text=text, condition=resolve_condition(condition).name,
                        metrics={"counterexample": False, "prompt_chars": len(text)})

    fb = build_feedback_from_check(inner, condition, bug_type=bug_type, **kwargs)
    fb.metrics.setdefault("counterexample", True)
    if failed:
        fb.metrics["failed_test"] = failed.get("name")
    return fb


def run_record_path(
    directory: str, bug_id: str, condition: str, *, allow_promotion: bool = True,
    trial: int = 1,
) -> str:
    """Where a run record for ``(bug_id, condition, allow_promotion, trial)`` lives.

    One file per ``(bug, condition)`` keeps different conditions from
    overwriting each other, which a single ``<bug_id>.json`` would do -- but
    ``allow_promotion`` needs the same treatment (docs/IMPLEMENTATION.md
    Blocker 7): it is a separate ablation axis, not part of ``condition``,
    yet ``--no-promotion`` only makes sense for ``iraware*`` conditions. Without
    this, running the ablation for a bug/condition already run either
    silently overwrote the paired result or (worse) got skipped entirely by
    the "already done" check, because both wrote to the identical path.

    The default (``allow_promotion=True``) keeps the original filename
    unchanged, so existing run records and tooling that assumes
    ``<bug_id>.<condition>.json`` are unaffected; only the ablation variant
    gets a suffix.

    ``trial`` is the pass@k repeat index (docs/ANALYSIS_PLAN.md). It gets the
    same treatment for the same reason: without it, ``--repeat 3`` would write
    all three samples of a (bug, condition) cell to one path, so the "already
    done" check would stop after the first and k would silently collapse to 1.
    Trial 1 keeps the unsuffixed name, so records written before repeats
    existed are still read as what they are -- the first trial.

    ``examples/repair_experiment.py``'s own "already done" pre-check calls
    this directly (rather than duplicating the naming scheme) specifically so
    the two can never drift apart again.
    """
    suffix = "" if allow_promotion else ".no-promotion"
    if trial and trial > 1:
        suffix += f".t{trial}"
    return os.path.join(directory, f"{bug_id}.{condition}{suffix}.json")


def record_is_complete(path: str) -> bool:
    """True when ``path`` holds a finished run that a resume may skip.

    A plain ``os.path.exists`` is not enough, because two kinds of file can
    sit at a record's path without the run behind them having finished:

    **A run that ended in an endpoint error.** ``repair()`` breaks out of its
    loop when the model call fails, and already refuses to write anything at
    all when that happens before the first turn -- filing an infrastructure
    outage as a failed repair would drag the condition's rate down. But when
    it happens on turn 2 of 4, some iterations were already recorded, so a
    record *is* written, carrying ``notes.llm_error`` and a ``fixed: false``
    that means "the endpoint died", not "the model could not fix this bug".
    Without this check a resume skips that cell forever and the contamination
    is permanent. This is not hypothetical: a whole nine-condition pilot was
    lost to it once (docs/IMPLEMENTATION.md Blocker 14).

    **A truncated file**, from a sweep killed mid-write. ``RunLog.write``
    renames into place atomically now, so this should not happen going
    forward, but records written before that change can still be short.

    Either way the answer is the same -- redo the cell -- so both collapse
    into one predicate. Anything unreadable is treated as incomplete.
    """
    try:
        with open(path, encoding="utf-8") as f:
            record = json.load(f)
    except (OSError, ValueError):
        return False
    if not isinstance(record, dict):
        return False
    return not record.get("notes", {}).get("llm_error")


@dataclass
class Iteration:
    """One turn of the repair loop, as a row of experiment data."""

    index: int
    condition: str
    fixed: bool
    feedback: Dict[str, object] = field(default_factory=dict)
    llm: Dict[str, object] = field(default_factory=dict)
    seconds: float = 0.0

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "condition": self.condition,
            "fixed": self.fixed,
            "seconds": round(self.seconds, 3),
            "feedback": self.feedback,
            "llm": self.llm,
        }


@dataclass
class RunLog:
    """Everything one (bug, condition) repair attempt produced.

    ``context.md`` s18 asks for repair correctness *and* cost, and s28 warns
    that the comparison is only fair if every condition gets the same budget.
    Recording the budget alongside the outcome is what makes that checkable
    after the fact instead of assumed.
    """

    bug_id: str
    condition: str
    model: str = ""
    max_iterations: int = 0
    #: pass@k repeat index, 1-based. See ``run_record_path``.
    trial: int = 1
    iterations: List[Iteration] = field(default_factory=list)
    fixed: bool = False
    #: Populated from ``lab_env.Environment.dump()``.
    certificate: Optional[dict] = None
    started_at: float = field(default_factory=time.time)
    notes: Dict[str, object] = field(default_factory=dict)

    def record(self, iteration: Iteration) -> None:
        self.iterations.append(iteration)
        self.fixed = self.fixed or iteration.fixed

    def totals(self) -> Dict[str, object]:
        """Aggregate cost across iterations: the RQ4 metrics."""
        def total(key: str) -> float:
            return sum(float(i.feedback.get(key, 0) or 0) for i in self.iterations)

        return {
            "iterations": len(self.iterations),
            "fixed": self.fixed,
            "prompt_tokens_est": total("prompt_tokens_est"),
            "oracle_calls": total("oracle_calls"),
            "reduction_seconds": round(total("seconds"), 3),
            "llm_prompt_tokens": sum(
                int(i.llm.get("prompt_tokens", 0) or 0) for i in self.iterations
            ),
            "llm_completion_tokens": sum(
                int(i.llm.get("completion_tokens", 0) or 0) for i in self.iterations
            ),
            "wall_seconds": round(time.time() - self.started_at, 3),
        }

    def as_dict(self) -> dict:
        return {
            "bug_id": self.bug_id,
            "condition": self.condition,
            "model": self.model,
            "max_iterations": self.max_iterations,
            "trial": self.trial,
            "totals": self.totals(),
            "iterations": [i.as_dict() for i in self.iterations],
            "certificate": self.certificate,
            "notes": self.notes,
        }

    def write(self, directory: str) -> str:
        """Write the run record and return the path. See ``run_record_path``.

        Written to a temporary file and then renamed, because a sweep runs for
        days and can be killed at any instant. ``os.replace`` is atomic, so a
        reader either sees the previous record or the new one -- never a
        half-written file that ``json.load`` chokes on and that
        :func:`record_is_complete` would then have to throw away.
        """
        os.makedirs(directory, exist_ok=True)
        path = run_record_path(
            directory, self.bug_id, self.condition,
            allow_promotion=self.notes.get("allow_promotion", True),
            trial=self.trial,
        )
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.as_dict(), f, indent=2)
        os.replace(tmp, path)
        return path


def load_runs(directory: str) -> List[dict]:
    """Read every run record in ``directory``, for offline analysis."""
    out: List[dict] = []
    if not os.path.isdir(directory):
        return out
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(directory, name), "r", encoding="utf-8") as f:
                out.append(json.load(f))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def per_bug_outcomes(runs: Sequence[dict]) -> Dict[str, Dict[str, bool]]:
    """``{condition: {bug_id: fixed_in_at_least_one_trial}}``.

    This is the unit the significance test consumes (docs/ANALYSIS_PLAN.md):
    one paired binary observation per bug, not one per run. With ``--repeat k``
    a bug contributes k records to one cell, and collapsing them here -- rather
    than in the caller -- keeps the summary table and the McNemar table
    counting the same thing.
    """
    out: Dict[str, Dict[str, bool]] = {}
    for run in runs:
        cell = out.setdefault(run.get("condition", "?"), {})
        bug = str(run.get("bug_id"))
        cell[bug] = cell.get(bug, False) or bool(run.get("totals", {}).get("fixed"))
    return out


def summarize(runs: Sequence[dict]) -> Dict[str, Dict[str, object]]:
    """Aggregate run records by condition: the headline experiment table.

    Reports repair rate as the primary metric and the cost metrics beside it,
    because a condition that repairs more bugs while spending far more is a
    different finding from one that repairs more for less (``context.md`` s19).

    With ``--repeat k`` there are k records per (bug, condition). The two rates
    answer different questions and both are reported:

    ``pass_at_k``
        fraction of *bugs* fixed on at least one of the k trials. This is the
        headline repair rate and the input to the significance test.
    ``pass_at_1``
        fraction of *runs* that fixed their bug -- the expected success of a
        single attempt. Equal to ``pass_at_k`` when k = 1.

    Reporting only the first would hide that a condition needed three tries;
    reporting only the second would throw away the paired structure the test
    needs.
    """
    by_condition: Dict[str, List[dict]] = {}
    for run in runs:
        by_condition.setdefault(run.get("condition", "?"), []).append(run)
    outcomes = per_bug_outcomes(runs)

    table: Dict[str, Dict[str, object]] = {}
    for condition, group in sorted(by_condition.items()):
        runs_n = len(group)
        run_fixed = sum(1 for r in group if r.get("totals", {}).get("fixed"))
        bugs = outcomes.get(condition, {})
        bugs_n = len(bugs)
        bugs_fixed = sum(1 for v in bugs.values() if v)
        def mean(key: str) -> float:
            vals = [float(r.get("totals", {}).get(key, 0) or 0) for r in group]
            return round(sum(vals) / len(vals), 2) if vals else 0.0

        table[condition] = {
            "bugs_attempted": bugs_n,
            "bugs_fixed": bugs_fixed,
            "runs": runs_n,
            "pass_at_k": round(bugs_fixed / bugs_n, 4) if bugs_n else 0.0,
            "pass_at_1": round(run_fixed / runs_n, 4) if runs_n else 0.0,
            # Kept under its original name so existing notes and tooling that
            # read "repair_rate" still resolve; it is the pass@k number.
            "repair_rate": round(bugs_fixed / bugs_n, 4) if bugs_n else 0.0,
            "mean_iterations": mean("iterations"),
            "mean_prompt_tokens_est": mean("prompt_tokens_est"),
            "mean_oracle_calls": mean("oracle_calls"),
            "mean_wall_seconds": mean("wall_seconds"),
        }
    return table
