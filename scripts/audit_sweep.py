#!/usr/bin/env python3
"""Read-only coverage and failure audit; never builds LLVM or queries a model.

Unlike an interim pass@k table, this requires trials 1..K to be complete
before a bug-condition cell contributes to a comparison. Infrastructure symptoms
are flagged for inspection and do not automatically remove completed outcomes.
Diagnostic categories are text-based leads, not causal attribution.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import re
import statistics
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ce.benchmark import record_is_complete  # noqa: E402
from ce.experiment import ExperimentMismatch, load_experiment_runs  # noqa: E402
from ce.feedback import CONDITIONS  # noqa: E402
from examples.analyze_significance import PRIMARY, mcnemar_exact  # noqa: E402
from scripts.triage_sweep import patch_statuses  # noqa: E402


def feedback_after_replies(run: dict) -> list[str]:
    """Align returned validation feedback with the proposal that caused it.

    A fixed final proposal has no following feedback. Extra or missing messages
    leave an empty entry rather than shifting all subsequent diagnostics.
    """
    messages = ((run.get("certificate") or {}).get("log") or {}).get("messages", [])
    outputs: list[str] = []
    awaiting = False
    for message in messages:
        if message.get("role") == "assistant":
            outputs.append("")
            awaiting = True
        elif awaiting and message.get("role") == "user":
            content = message.get("content") or ""
            if content.startswith("Feedback:\n"):
                outputs[-1] = content[len("Feedback:\n"):].split(
                    "\nPlease adjust code according to the feedback.", 1)[0]
            awaiting = False
    return outputs


def diagnostic(text: str) -> tuple[str, str]:
    """Classify diagnostic evidence, preserving a useful first error signature."""
    lower = text.lower()
    patterns = (
        ("resource_failure", r"out of memory|oom[- ]kill|cannot allocate memory|"
         r"killed signal terminated program|killed process|no space left on device|disk quota exceeded"),
        ("tool_failure", r"command not found|permission denied|cmake error|"
         r"error while loading shared libraries|unable to execute command|"
         r"cannot execute binary|ninja: error:|internal compiler error|"
         r"clang frontend command failed|please submit a bug report"),
        ("compiler_diagnostic", r"(?:fatal )?error:"),
    )
    for category, pattern in patterns:
        if re.search(pattern, lower):
            lines = text.splitlines()
            line = next((line.strip() for line in lines
                         if re.search(pattern, line, re.IGNORECASE)), category)
            # File/line locations vary across revisions; retain the actual error.
            line = re.sub(r"^.*?:\d+:\d+:\s*", "", line)
            return category, line[:300]
    return "unclassified", "no recognized diagnostic in saved feedback"


def read_records(directory: str, model: str | None = None) -> tuple[list[tuple[Path, dict]], list[str]]:
    # Use the same mixed-configuration guard as the other analysis commands.
    load_experiment_runs(directory, model)
    records = []
    unreadable = []
    for path in sorted(Path(directory).glob("*.json")):
        if path.name.startswith("."):
            continue
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            unreadable.append(path.name)
            continue
        if not isinstance(run, dict) or not {"bug_id", "condition"} <= run.keys():
            continue
        if model is None or run.get("model") == model:
            records.append((path, run))
    return records, unreadable


def sample_bugs(path: str) -> list[str]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, list):
        return [str(bug) for bug in data]
    if "bug_ids" in data:
        return [str(bug) for bug in data["bug_ids"]]
    return [str(row["bug_id"]) for tier in data["tiers"].values() for row in tier]


def audit(directory: str, *, repeat: int = 3, conditions: list[str] | None = None,
          bugs: list[str] | None = None, model: str | None = None,
          allow_promotion: bool = True) -> dict:
    records, unreadable = read_records(directory, model)
    conditions = list(conditions or sorted(CONDITIONS))
    chosen = [(path, run) for path, run in records
              if run.get("notes", {}).get("allow_promotion", True) == allow_promotion
              and run.get("condition") in conditions]
    seen = sorted({str(run["bug_id"]) for _, run in chosen})
    selected_bugs = list(dict.fromkeys(bugs if bugs is not None else seen))
    by_identity: dict[tuple[str, str, int], list[tuple[Path, dict]]] = defaultdict(list)
    turns = Counter()
    build_diagnostics = Counter()
    diagnostic_examples: dict[tuple[str, str], list[str]] = defaultdict(list)
    regression_tests = Counter()
    exposure = {"initial": Counter(), "subsequent": Counter()}
    reductions: dict[str, dict[str, dict]] = defaultdict(dict)
    condition_counts = {condition: Counter() for condition in conditions}
    rows = []
    durations = []
    invalid_trial_records = []
    for path, run in chosen:
        bug, condition = str(run["bug_id"]), run["condition"]
        raw_trial = run.get("trial", 1)
        try:
            trial = int(raw_trial)
            if trial < 1 or isinstance(raw_trial, bool) or (isinstance(raw_trial, float) and raw_trial != trial):
                trial = None
        except (ValueError, TypeError, OverflowError):
            trial = None
        if trial is None:
            invalid_trial_records.append(path.name)
        else:
            by_identity[(bug, condition, trial)].append((path, run))
        if bug not in selected_bugs:
            continue
        counter = condition_counts[condition]
        counter["records"] += 1
        counter["fixed_records"] += bool(run.get("totals", {}).get("fixed"))
        complete = record_is_complete(str(path))
        counter["finished_records"] += complete
        counter["incomplete_records"] += not complete
        statuses = patch_statuses(run)
        returned_logs = feedback_after_replies(run)
        suspected_infrastructure = False
        validation_timeout = False
        turn_rows = []
        for index, iteration in enumerate(run.get("iterations", [])):
            turns["total"] += 1
            counter["turns"] += 1
            status = statuses[index] if index < len(statuses) else None
            turns[f"patch_{status or 'unknown'}"] += 1
            counter["no_op_replies"] += status == "unchanged"
            validation = iteration.get("validation") or {}
            stage = validation.get("stage", "unknown")
            passed = bool(validation.get("passed", iteration.get("fixed", False)))
            turns[f"{stage}_{'passed' if passed else 'failed'}"] += 1
            counter["recorded_build_attempts"] += stage in ("build", "reproducer", "regression")
            counter["recorded_build_failures"] += stage == "build" and not passed
            counter["recorded_reproducer_passed_turns"] += stage == "regression" or (stage == "reproducer" and passed)
            counter["full_pass_turns"] += bool(iteration.get("fixed"))
            fb = iteration.get("feedback") or {}
            phase = exposure["initial" if index == 0 else "subsequent"]
            phase["turns"] += 1
            phase["alive2_routed_feedback"] += fb.get("counterexample") is True
            phase["baseline_witness_suppressed"] += fb.get("counterexample") is True and condition == "baseline"
            phase["witness_eligible_feedback"] += (fb.get("counterexample") is True
                                                   and condition != "baseline"
                                                   and not fb.get("error") and not fb.get("verified"))
            phase["metadata_missing"] += "counterexample" not in fb
            if fb.get("counterexample") and condition.split("-", 1)[0] in ("generic", "llvmreduce", "iraware"):
                phase_name = "initial" if index == 0 else "subsequent"
                reduction = reductions[bug].setdefault(condition, {}).setdefault(
                    phase_name, {"feedbacks": 0, "no_accepted_pass": 0,
                                 "no_instruction_shrink": 0, "no_character_shrink": 0,
                                 "size_metadata_missing": 0, "fallback_errors": Counter(),
                                 "oracle_calls": 0, "oracle_tool_failures": 0,
                                 "original_instructions": [], "shown_instructions": [],
                                 "original_chars": [], "shown_chars": []})
                reduction["feedbacks"] += 1
                reduction["no_accepted_pass"] += not fb.get("passes_applied")
                before, after = fb.get("size_before") or {}, fb.get("size_after") or {}
                for measure in ("instructions", "chars"):
                    original = fb.get(f"original_{measure}", before.get(measure))
                    shown = fb.get(f"shown_{measure}")
                    # size_after may describe an attempted reduction that was
                    # discarded. Without actual shown metrics an error means
                    # the displayed size is unknown, not the attempted size.
                    if shown is None and not fb.get("error"):
                        shown = after.get(measure)
                    if isinstance(original, (int, float)):
                        reduction[f"original_{measure}"].append(original)
                    if isinstance(shown, (int, float)):
                        reduction[f"shown_{measure}"].append(shown)
                    if isinstance(original, (int, float)) and isinstance(shown, (int, float)):
                        key = "no_instruction_shrink" if measure == "instructions" else "no_character_shrink"
                        reduction[key] += shown >= original
                    elif measure == "instructions":
                        reduction["size_metadata_missing"] += 1
                reduction["oracle_calls"] += int(fb.get("oracle_calls", 0) or 0)
                reduction["oracle_tool_failures"] += int(fb.get("oracle_tool_failures", 0) or 0)
                if fb.get("error"):
                    reduction["fallback_errors"][str(fb["error"])] += 1
            category = signature = None
            text = returned_logs[index] if index < len(returned_logs) else ""
            if stage == "build" and not passed:
                category, signature = diagnostic(text)
                build_diagnostics[(category, signature)] += 1
                location = f"{path.name}:turn{index + 1}"
                if len(diagnostic_examples[(category, signature)]) < 3:
                    diagnostic_examples[(category, signature)].append(location)
                if category in ("resource_failure", "tool_failure"):
                    suspected_infrastructure = True
            if validation.get("failure_kind") == "infrastructure":
                suspected_infrastructure = True
            if validation.get("failure_kind") == "timeout":
                validation_timeout = True
            if stage == "regression" and not passed:
                for test in set(re.findall(r"^(?:FAIL|UNRESOLVED):\s*(.+)$", text, re.MULTILINE)):
                    regression_tests[test.strip()] += 1
            turn_rows.append({"turn": index + 1, "patch": status, "stage": stage,
                              "passed": passed, "diagnostic_category": category,
                              "diagnostic_signature": signature})
        counter["suspected_infrastructure_records"] += suspected_infrastructure
        counter["validation_timeout_records"] += validation_timeout
        wall = run.get("totals", {}).get("wall_seconds")
        if isinstance(wall, (int, float)) and wall >= 0:
            durations.append(float(wall))
        rows.append({"file": path.name, "bug": bug, "condition": condition,
                     "trial": trial, "finished": complete,
                     "suspected_infrastructure": suspected_infrastructure,
                     "validation_timeout": validation_timeout,
                     "fixed": bool(run.get("totals", {}).get("fixed")),
                     "turns": turn_rows})

    row_map = {row["file"]: row for row in rows}
    cells: dict[tuple[str, str], dict] = {}
    duplicate_identities = []
    for bug in selected_bugs:
        for condition in conditions:
            missing, incomplete, review_required, duplicates = [], [], [], []
            successes = 0
            for trial in range(1, repeat + 1):
                found = by_identity.get((bug, condition, trial), [])
                if not found:
                    missing.append(trial)
                elif len(found) > 1:
                    duplicates.append(trial)
                    duplicate_identities.append({"bug": bug, "condition": condition,
                                                 "trial": trial,
                                                 "files": [path.name for path, _ in found]})
                else:
                    row = row_map[found[0][0].name]
                    if not row["finished"]:
                        incomplete.append(trial)
                    else:
                        successes += row["fixed"]
                    if row["suspected_infrastructure"]:
                        review_required.append(trial)
            complete = not (missing or incomplete or duplicates)
            cells[(bug, condition)] = {
                "complete": complete, "fixed": successes > 0 if complete else None,
                "fixed_trials": successes if complete else None,
                "missing_trials": missing, "incomplete_trials": incomplete,
                "review_required_trials": review_required, "duplicate_trials": duplicates,
            }
    common = [bug for bug in selected_bugs
              if all(cells[(bug, condition)]["complete"] for condition in conditions)]
    by_condition = {}
    for condition in conditions:
        complete_bugs = [bug for bug in selected_bugs if cells[(bug, condition)]["complete"]]
        fixed_bugs = [bug for bug in complete_bugs if cells[(bug, condition)]["fixed"]]
        counts = dict(condition_counts[condition])
        counts.update({"complete_bug_cells": len(complete_bugs), "fixed_bugs": fixed_bugs,
                       "pass_at_k": len(fixed_bugs) / len(complete_bugs) if complete_bugs else None,
                       "pass_at_1": (sum(cells[(bug, condition)]["fixed_trials"] for bug in complete_bugs)
                                     / (repeat * len(complete_bugs))) if complete_bugs else None,
                       "common_panel_fixed_bugs": [bug for bug in common if cells[(bug, condition)]["fixed"]]})
        by_condition[condition] = counts
    comparisons = []
    for better, worse, question in PRIMARY:
        if better not in conditions or worse not in conditions:
            continue
        paired = [bug for bug in selected_bugs
                  if cells[(bug, better)]["complete"] and cells[(bug, worse)]["complete"]]
        both = [bug for bug in paired if cells[(bug, better)]["fixed"] and cells[(bug, worse)]["fixed"]]
        only_better = [bug for bug in paired if cells[(bug, better)]["fixed"] and not cells[(bug, worse)]["fixed"]]
        only_worse = [bug for bug in paired if not cells[(bug, better)]["fixed"] and cells[(bug, worse)]["fixed"]]
        neither = [bug for bug in paired if not cells[(bug, better)]["fixed"] and not cells[(bug, worse)]["fixed"]]
        one, two = mcnemar_exact(len(only_better), len(only_worse))
        comparisons.append({"better": better, "worse": worse, "question": question,
                            "n": len(paired), "paired_bugs": paired, "both": both,
                            "only_better": only_better, "only_worse": only_worse,
                            "neither": neither,
                            "rate_difference": ((len(only_better) - len(only_worse)) / len(paired)
                                                if paired else None),
                            "p_one_sided_unadjusted": one if paired else None,
                            "p_two_sided_unadjusted": two if paired else None})
    return {
        "directory": directory, "expected_repeat": repeat, "conditions": conditions,
        "scope": "specified sample" if bugs is not None else "observed bugs only",
        "bugs": selected_bugs, "outside_sample_bugs": sorted(set(seen) - set(selected_bugs)),
        "record_count": len(rows), "unreadable_json": unreadable,
        "invalid_trial_records": invalid_trial_records,
        "duplicate_identities": duplicate_identities,
        "complete_all_condition_bugs": common,
        "planned_runs": len(selected_bugs) * len(conditions) * repeat,
        "usable_planned_trial_records": sum(
            1 for row in rows if row["trial"] is not None and 1 <= row["trial"] <= repeat and row["finished"]
            and len(by_identity[(row["bug"], row["condition"], row["trial"])]) == 1),
        "by_condition": by_condition,
        "by_bug": {bug: {condition: cells[(bug, condition)] for condition in conditions}
                   for bug in selected_bugs},
        "turns": dict(turns), "feedback_exposure": {key: dict(value) for key, value in exposure.items()},
        "reduction_by_bug": dict(reductions),
        "build_diagnostics": [{"category": category, "signature": signature, "turns": count,
                               "examples": diagnostic_examples[(category, signature)]}
                              for (category, signature), count in build_diagnostics.most_common()],
        "regression_test_failures": dict(regression_tests.most_common()),
        "primary_pairs_descriptive": comparisons,
        "timing": {"records_with_wall_seconds": len(durations),
                   "sum_recorded_wall_seconds": round(sum(durations), 3),
                   "median_recorded_wall_seconds": round(statistics.median(durations), 3) if durations else None,
                   "eta": None,
                   "note": "Run JSON stores durations, not start/end timestamps. No timestamp-based ETA; later LLVM revisions may cost more."},
        "interpretation": [
            "Interim descriptive audit. Do not stop or select conditions because a p-value crosses a threshold.",
            "Each comparison uses the same bugs with all requested trial IDs completed in both arms.",
            "Condition-specific rates can have different bug denominators; compare the common panel or paired counts.",
            "Infrastructure flags are diagnostic leads, not causal attribution. Completed outcomes remain in paired counts until a documented review decides otherwise.",
            "No-edit source replies differ from counterexample reductions that remove nothing.",
            "Feedback metadata describes the input shown to a proposal, not the outcome of that proposal.",
            "counterexample=True records Alive2 routing, even for baseline. Witness-eligible counts exclude baseline, tool errors, and verified outputs; inspect saved prompts to establish literal content.",
            "Timeouts are reported separately, not automatically excluded: a candidate patch can itself cause a timeout.",
        ],
    }


def print_report(report: dict) -> None:
    print(f"{report['record_count']} records; {report['usable_planned_trial_records']}/{report['planned_runs']} usable planned trials")
    print(f"Complete across all {len(report['conditions'])} conditions and {report['expected_repeat']} trials: "
          f"{len(report['complete_all_condition_bugs'])} bugs ({', '.join(report['complete_all_condition_bugs']) or 'none'})")
    print("\nPer condition: complete bug cells / selected bugs; fixed within those cells")
    for condition, counts in report["by_condition"].items():
        print(f"  {condition:<24} {counts['complete_bug_cells']:>2}/{len(report['bugs'])} complete; "
              f"{len(counts['fixed_bugs'])} fixed; {counts.get('incomplete_records', 0)} incomplete records; "
              f"{counts.get('suspected_infrastructure_records', 0)} infrastructure suspects")
    print("\nCoverage by bug: completed conditions / requested conditions")
    for bug, cells in report["by_bug"].items():
        complete = sum(cell["complete"] for cell in cells.values())
        fixed = [condition for condition, cell in cells.items() if cell["fixed"]]
        print(f"  {bug:<10} {complete}/{len(cells)} complete; fixed: {', '.join(fixed) or 'none yet'}")
    print("\nInitial iraware reductions (medians; passes alone do not establish shrinking)")
    found_reduction = False
    for bug, conditions in report["reduction_by_bug"].items():
        for condition, phases in conditions.items():
            if not condition.startswith("iraware") or "initial" not in phases:
                continue
            found_reduction = True
            reduction = phases["initial"]
            def median_size(key):
                values = reduction[key]
                return f"{statistics.median(values):g}" if values else "unknown"
            print(f"  {bug}/{condition}: instructions {median_size('original_instructions')} -> "
                  f"{median_size('shown_instructions')}; chars {median_size('original_chars')} -> "
                  f"{median_size('shown_chars')}; no accepted pass "
                  f"{reduction['no_accepted_pass']}/{reduction['feedbacks']}; "
                  f"fallback errors {sum(reduction['fallback_errors'].values())}")
    if not found_reduction:
        print("  No recorded initial iraware reduction metadata.")
    print("\nBuild diagnostic evidence (top 12; categories require inspection)")
    turns = report["turns"]
    print(f"  {turns.get('total', 0)} turns; {turns.get('patch_unchanged', 0)} unchanged replies; "
          f"{turns.get('patch_mismatch', 0)} apply failures; "
          f"{turns.get('build_failed', 0)} recorded build-stage failures; "
          f"{turns.get('unknown_failed', 0)} turns without stage metadata")
    for item in report["build_diagnostics"][:12]:
        print(f"  {item['turns']:>4} {item['category']}: {item['signature']}")
        print(f"       example: {item['examples'][0]}")
    if not report["build_diagnostics"]:
        print("  No per-turn build-stage records available; do not infer compiler success from missing metadata.")
    print("\nPrimary paired counts (descriptive; incomplete bugs excluded)")
    for row in report["primary_pairs_descriptive"]:
        print(f"  {row['better']} vs {row['worse']}: n={row['n']}, "
              f"both={len(row['both'])}, only-first={len(row['only_better'])}, "
              f"only-second={len(row['only_worse'])}, neither={len(row['neither'])}")
    print("\nFeedback shown to model")
    for phase, counts in report["feedback_exposure"].items():
        print(f"  {phase}: {counts.get('alive2_routed_feedback', 0)}/{counts.get('turns', 0)} "
              f"Alive2-routed; {counts.get('witness_eligible_feedback', 0)} witness-eligible; "
              f"{counts.get('baseline_witness_suppressed', 0)} baseline-suppressed; "
              f"{counts.get('metadata_missing', 0)} missing metadata")
    print(f"\nTiming: {report['timing']['note']}")
    print("Use --json for full cell coverage, diagnostic examples, and regression-test names.")
    if report["unreadable_json"] or report["duplicate_identities"] or report["invalid_trial_records"]:
        print("WARNING: unreadable JSON, invalid trial IDs or duplicate trial identities found; inspect JSON output.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--condition", nargs="+", choices=sorted(CONDITIONS))
    parser.add_argument("--sample", help="sample JSON; otherwise reports only bugs already observed")
    parser.add_argument("--model")
    parser.add_argument("--no-promotion", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    try:
        report = audit(args.directory, repeat=args.repeat, conditions=args.condition,
                       bugs=sample_bugs(args.sample) if args.sample else None,
                       model=args.model, allow_promotion=not args.no_promotion)
    except (ExperimentMismatch, OSError, ValueError) as error:
        parser.error(str(error))
    if not report["record_count"]:
        print(f"no selected run records in {args.directory}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
