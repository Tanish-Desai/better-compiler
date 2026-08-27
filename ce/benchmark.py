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
) -> str:
    """Where a run record for ``(bug_id, condition, allow_promotion)`` lives.

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

    ``examples/repair_experiment.py``'s own "already done" pre-check calls
    this directly (rather than duplicating the naming scheme) specifically so
    the two can never drift apart again.
    """
    suffix = "" if allow_promotion else ".no-promotion"
    return os.path.join(directory, f"{bug_id}.{condition}{suffix}.json")


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
            "totals": self.totals(),
            "iterations": [i.as_dict() for i in self.iterations],
            "certificate": self.certificate,
            "notes": self.notes,
        }

    def write(self, directory: str) -> str:
        """Write the run record and return the path. See ``run_record_path``."""
        os.makedirs(directory, exist_ok=True)
        path = run_record_path(
            directory, self.bug_id, self.condition,
            allow_promotion=self.notes.get("allow_promotion", True),
        )
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.as_dict(), f, indent=2)
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


def summarize(runs: Sequence[dict]) -> Dict[str, Dict[str, object]]:
    """Aggregate run records by condition: the headline experiment table.

    Reports repair rate as the primary metric and the cost metrics beside it,
    because a condition that repairs more bugs while spending far more is a
    different finding from one that repairs more for less (``context.md`` s19).
    """
    by_condition: Dict[str, List[dict]] = {}
    for run in runs:
        by_condition.setdefault(run.get("condition", "?"), []).append(run)

    table: Dict[str, Dict[str, object]] = {}
    for condition, group in sorted(by_condition.items()):
        n = len(group)
        fixed = sum(1 for r in group if r.get("totals", {}).get("fixed"))
        def mean(key: str) -> float:
            vals = [float(r.get("totals", {}).get(key, 0) or 0) for r in group]
            return round(sum(vals) / len(vals), 2) if vals else 0.0

        table[condition] = {
            "bugs_attempted": n,
            "bugs_fixed": fixed,
            "repair_rate": round(fixed / n, 4) if n else 0.0,
            "mean_iterations": mean("iterations"),
            "mean_prompt_tokens_est": mean("prompt_tokens_est"),
            "mean_oracle_calls": mean("oracle_calls"),
            "mean_wall_seconds": mean("wall_seconds"),
        }
    return table
