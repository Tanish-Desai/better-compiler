"""The audit must not turn partial coverage or infrastructure faults into repair evidence."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

from ce.experiment import ExperimentMismatch
from scripts.audit_sweep import audit, diagnostic, feedback_after_replies


CONFIG = {"model": "qwen3", "harness_version": "repair-v2"}


def put(directory, bug="1", condition="raw-plain", trial=1, fixed=False,
        count=4, notes=None, stage="reproducer", feedback=None, diagnostic_text="",
        name=None, validation_kind="error"):
    run = {"bug_id": bug, "condition": condition, "trial": trial, "model": "qwen3",
           "max_iterations": 4, "notes": {"experiment_config": CONFIG, **(notes or {})},
           "totals": {"fixed": fixed, "wall_seconds": 120},
           "iterations": [], "certificate": {"log": {"messages": []}}}
    messages = run["certificate"]["log"]["messages"]
    messages.append({"role": "user", "content": "initial input"})
    for index in range(count):
        run["iterations"].append({"index": index, "patch": "applied", "fixed": fixed,
                                  "feedback": feedback or {"counterexample": index == 0},
                                  "validation": {"stage": stage, "passed": fixed,
                                                 "failure_kind": None if fixed else validation_kind}})
        messages.append({"role": "assistant", "content": "proposed code"})
        if not fixed:
            messages.append({"role": "user", "content": "Feedback:\n" + diagnostic_text
                             + "\nPlease adjust code according to the feedback.\nformat requirement"})
    path = Path(directory) / (name or f"{bug}.{condition}.t{trial}.json")
    path.write_text(json.dumps(run), encoding="utf-8")
    return path, run


def test_partial_trials_do_not_make_pass_at_three_or_a_pair(tmp_path):
    put(tmp_path, condition="raw-plain", fixed=True, count=1)
    for trial in (1, 2, 3):
        put(tmp_path, condition="baseline", trial=trial)
    report = audit(str(tmp_path), conditions=["raw-plain", "baseline"])
    assert report["by_condition"]["raw-plain"]["complete_bug_cells"] == 0
    assert report["by_condition"]["raw-plain"]["pass_at_k"] is None
    assert report["primary_pairs_descriptive"][0]["n"] == 0
    assert report["by_bug"]["1"]["raw-plain"]["missing_trials"] == [2, 3]


def test_full_pairs_count_bugs_not_trials_and_preserve_pairing(tmp_path):
    for bug in ("1", "2", "3", "4"):
        for condition in ("raw-plain", "baseline"):
            for trial in (1, 2, 3):
                fixed = (bug == "1" or (bug == "2" and condition == "raw-plain")
                         or (bug == "3" and condition == "baseline")) and trial == 2
                put(tmp_path, bug=bug, condition=condition, trial=trial,
                    fixed=fixed, count=1 if fixed else 4)
    report = audit(str(tmp_path), conditions=["raw-plain", "baseline"])
    row = report["primary_pairs_descriptive"][0]
    assert row["n"] == 4
    assert row["both"] == ["1"]
    assert row["only_better"] == ["2"]
    assert row["only_worse"] == ["3"]
    assert row["neither"] == ["4"]
    assert report["by_condition"]["raw-plain"]["pass_at_1"] == pytest.approx(2 / 12)
    assert report["by_condition"]["raw-plain"]["pass_at_k"] == 0.5


def test_endpoint_failure_excluded_even_after_maximum_turns(tmp_path):
    put(tmp_path, notes={"llm_error": "connection dropped"})
    report = audit(str(tmp_path), repeat=1, conditions=["raw-plain"])
    assert report["by_bug"]["1"]["raw-plain"]["incomplete_trials"] == [1]


@pytest.mark.parametrize("text,category", [
    ("c++: fatal error: Killed signal terminated program cc1plus", "resource_failure"),
    ("fatal error: no space left on device", "resource_failure"),
    ("/bin/sh: ninja: command not found", "tool_failure"),
    ("/llvm/lib/InstCombine.cpp:12:4: error: no member named 'foo' in 'Value'", "compiler_diagnostic"),
    ("ninja: build stopped: subcommand failed.", "unclassified"),
])
def test_diagnostic_categories_do_not_call_every_failed_build_an_oom(text, category):
    assert diagnostic(text)[0] == category


def test_infrastructure_suspects_require_review_without_changing_completed_outcomes(tmp_path):
    put(tmp_path, bug="compiler", stage="build", diagnostic_text="x.cpp:1:2: error: undeclared identifier 'x'")
    put(tmp_path, bug="resource", stage="build", diagnostic_text="c++: fatal error: Killed signal terminated program cc1plus")
    report = audit(str(tmp_path), repeat=1, conditions=["raw-plain"])
    assert report["by_bug"]["compiler"]["raw-plain"]["complete"]
    assert report["by_bug"]["resource"]["raw-plain"]["complete"]
    assert report["by_bug"]["resource"]["raw-plain"]["review_required_trials"] == [1]
    assert {row["category"] for row in report["build_diagnostics"]} == {"resource_failure", "compiler_diagnostic"}
    assert report["usable_planned_trial_records"] == 2


def test_feedback_alignment_and_regression_names_are_from_the_same_turn(tmp_path):
    path, run = put(tmp_path, count=2, stage="regression", validation_kind="test_failure",
                    diagnostic_text="FAIL: LLVM :: Transforms/example.ll (1 of 10)")
    # A later turn may fail at build; the earlier lit failure must remain visible.
    run["iterations"][1]["validation"]["stage"] = "build"
    run["certificate"]["log"]["messages"][-1]["content"] = "Feedback:\nx.cpp:4:1: error: bad type"
    path.write_text(json.dumps(run), encoding="utf-8")
    logs = feedback_after_replies(run)
    assert logs[0].startswith("FAIL:")
    assert "bad type" in logs[1]
    report = audit(str(tmp_path), repeat=1, conditions=["raw-plain"])
    assert report["regression_test_failures"] == {"LLVM :: Transforms/example.ll (1 of 10)": 1}
    assert report["build_diagnostics"][0]["signature"] == "error: bad type"


def test_initial_feedback_is_counted_and_passes_are_not_a_shrink_measure(tmp_path):
    fb = {"counterexample": True, "passes_applied": ["strip-flags"],
          "original_instructions": 8, "shown_instructions": 8,
          "original_chars": 100, "shown_chars": 110, "oracle_calls": 30}
    put(tmp_path, condition="iraware-plain", fixed=True, count=1, feedback=fb)
    report = audit(str(tmp_path), repeat=1, conditions=["iraware-plain"])
    reduction = report["reduction_by_bug"]["1"]["iraware-plain"]["initial"]
    assert reduction["feedbacks"] == 1
    assert reduction["no_accepted_pass"] == 0
    assert reduction["no_instruction_shrink"] == 1
    assert reduction["no_character_shrink"] == 1
    assert report["feedback_exposure"]["initial"]["witness_eligible_feedback"] == 1


def test_baseline_alive2_flag_does_not_mean_a_witness_was_shown(tmp_path):
    put(tmp_path, condition="baseline", fixed=True, count=1,
        feedback={"counterexample": True})
    report = audit(str(tmp_path), repeat=1, conditions=["baseline"])
    initial = report["feedback_exposure"]["initial"]
    assert initial["alive2_routed_feedback"] == 1
    assert initial["baseline_witness_suppressed"] == 1
    assert initial["witness_eligible_feedback"] == 0


def test_timeout_is_not_automatically_treated_as_infrastructure(tmp_path):
    put(tmp_path, validation_kind="timeout")
    report = audit(str(tmp_path), repeat=1, conditions=["raw-plain"])
    assert report["by_bug"]["1"]["raw-plain"]["complete"]
    assert report["by_condition"]["raw-plain"]["validation_timeout_records"] == 1


def test_fallback_does_not_report_attempted_size_as_shown_size(tmp_path):
    fb = {"counterexample": True, "passes_applied": [], "size_before": {"instructions": 30},
          "size_after": {"instructions": 4}, "error": "re-verification failed"}
    put(tmp_path, condition="iraware-plain", fixed=True, count=1, feedback=fb)
    report = audit(str(tmp_path), repeat=1, conditions=["iraware-plain"])
    reduction = report["reduction_by_bug"]["1"]["iraware-plain"]["initial"]
    assert reduction["original_instructions"] == [30]
    assert reduction["shown_instructions"] == []
    assert reduction["size_metadata_missing"] == 1


@pytest.mark.parametrize("bad_trial", ["bad", None, 1.5, 0, False])
def test_invalid_trial_is_reported_without_crashing_or_becoming_trial_one(tmp_path, bad_trial):
    path, run = put(tmp_path)
    run["trial"] = bad_trial
    path.write_text(json.dumps(run), encoding="utf-8")
    report = audit(str(tmp_path), repeat=1, conditions=["raw-plain"])
    assert report["invalid_trial_records"] == [path.name]
    assert report["by_bug"]["1"]["raw-plain"]["missing_trials"] == [1]
    assert report["usable_planned_trial_records"] == 0


def test_duplicate_identity_does_not_double_sample_and_unreadable_json_is_reported(tmp_path):
    path, run = put(tmp_path)
    (tmp_path / "duplicate.json").write_text(json.dumps(run), encoding="utf-8")
    (tmp_path / "truncated.json").write_text('{"bug_id":', encoding="utf-8")
    report = audit(str(tmp_path), repeat=1, conditions=["raw-plain"])
    assert report["by_bug"]["1"]["raw-plain"]["duplicate_trials"] == [1]
    assert report["usable_planned_trial_records"] == 0
    assert report["unreadable_json"] == ["truncated.json"]


def test_specified_sample_includes_unattempted_bugs_without_an_eta(tmp_path):
    put(tmp_path)
    report = audit(str(tmp_path), repeat=1, conditions=["raw-plain"], bugs=["1", "2"])
    assert report["planned_runs"] == 2
    assert report["by_bug"]["2"]["raw-plain"]["missing_trials"] == [1]
    assert report["timing"]["eta"] is None
    assert report["timing"]["sum_recorded_wall_seconds"] == 120


def test_promotion_ablation_is_not_silently_pooled(tmp_path):
    put(tmp_path, notes={"allow_promotion": False})
    assert audit(str(tmp_path), repeat=1, conditions=["raw-plain"])["record_count"] == 0
    assert audit(str(tmp_path), repeat=1, conditions=["raw-plain"], allow_promotion=False)["record_count"] == 1


def test_mixed_models_are_rejected_before_audit(tmp_path):
    put(tmp_path)
    path, run = put(tmp_path, bug="2")
    run["model"] = "other"
    run["notes"]["experiment_config"] = {"model": "other"}
    path.write_text(json.dumps(run), encoding="utf-8")
    with pytest.raises(ExperimentMismatch):
        audit(str(tmp_path))
    assert audit(str(tmp_path), model="qwen3")["record_count"] == 1


def test_json_cli_works_without_llvm_environment(tmp_path):
    put(tmp_path)
    root = Path(__file__).resolve().parent.parent
    process = subprocess.run([sys.executable, str(root / "scripts/audit_sweep.py"), str(tmp_path),
                              "--repeat", "1", "--condition", "raw-plain", "--json"],
                             capture_output=True, text=True)
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout)["complete_all_condition_bugs"] == ["1"]
