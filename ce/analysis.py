"""Select equal-budget, completed cells before comparing repair conditions.

An interrupted bug-major sweep contains partial bugs and sometimes interrupted
trials. Neither should silently become a lower-budget pass@3 observation.
This module reads records only; it does not alter experiment outcomes.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Sequence

from .feedback import CONDITIONS


def completed_record(run: dict) -> bool:
    """Mirror the runner's resume predicate, without reading a record twice."""
    iterations = run.get("iterations") or []
    if run.get("notes", {}).get("llm_error") or not iterations:
        return False
    budget = run.get("max_iterations", 0) or 0
    fixed = run.get("totals", {}).get("fixed", run.get("fixed", False))
    return bool(fixed or len(iterations) >= budget)


def select_complete_runs(
    runs: Sequence[dict], *, repeats: int = 3,
    conditions: Sequence[str] = tuple(CONDITIONS), promotion: bool = True,
) -> tuple[list[dict], dict]:
    """Keep bugs with exactly one completed record for every expected cell.

    Trials beyond the requested budget are ignored, so they cannot increase
    pass@k. Duplicate records for one trial invalidate that bug's comparison;
    choosing whichever duplicate succeeded would bias the outcome.
    """
    if repeats < 1:
        raise ValueError("repeats must be positive")
    conditions = tuple(dict.fromkeys(conditions))
    if not conditions:
        raise ValueError("at least one condition is required")
    cells = defaultdict(list)
    bugs = set()
    ignored = 0
    for run in runs:
        if (run.get("condition") not in conditions
                or bool(run.get("notes", {}).get("allow_promotion", True)) != promotion):
            continue
        bug = str(run.get("bug_id"))
        bugs.add(bug)
        raw_trial = run.get("trial", 1)
        try:
            trial = int(raw_trial)
            if isinstance(raw_trial, bool) or (isinstance(raw_trial, float) and raw_trial != trial):
                trial = 0
        except (TypeError, ValueError, OverflowError):
            trial = 0
        if not 1 <= trial <= repeats:
            ignored += 1
            continue
        cells[(bug, run["condition"], trial)].append(run)

    selected = []
    complete_bugs = []
    excluded = {}
    for bug in sorted(bugs):
        problems = []
        accepted = []
        for condition in conditions:
            for trial in range(1, repeats + 1):
                records = cells.get((bug, condition, trial), [])
                reason = ("missing" if not records else "duplicate" if len(records) != 1
                          else "incomplete" if not completed_record(records[0]) else None)
                if reason:
                    problems.append({"condition": condition, "trial": trial, "reason": reason})
                else:
                    accepted.append(records[0])
        if problems:
            excluded[bug] = problems
        else:
            complete_bugs.append(bug)
            selected.extend(accepted)
    return selected, {
        "repeat_count": repeats,
        "conditions": list(conditions),
        "promotion": promotion,
        "bugs_seen": sorted(bugs),
        "complete_bugs": complete_bugs,
        "excluded_bugs": excluded,
        "ignored_out_of_budget_records": ignored,
        "note": "Partial snapshots are descriptive; fixed-horizon inference is for the final planned sample.",
    }
