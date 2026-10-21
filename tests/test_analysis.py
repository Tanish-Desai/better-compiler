"""Equal exposure matters: early stops and duplicate trials can change pass@3."""

import json

import pytest

from ce.analysis import completed_record, select_complete_runs
from ce.benchmark import record_is_complete
from ce.feedback import CONDITIONS
from examples import analyze_significance, summarize_results


def run(bug="1", condition="baseline", trial=1, fixed=False, turns=4, **notes):
    return {"bug_id": bug, "condition": condition, "trial": trial,
            "max_iterations": 4, "iterations": [{}] * turns,
            "totals": {"fixed": fixed}, "notes": notes}


def test_unequal_trials_do_not_count_as_pass_at_three():
    rows = [run(condition=c, trial=t) for c in ("baseline", "raw-plain")
            for t in (1, 2, 3)]
    rows[-1] = run(condition="raw-plain", trial=3, turns=2, llm_error="outage")
    selected, coverage = select_complete_runs(rows, conditions=("baseline", "raw-plain"))
    assert not selected
    assert coverage["excluded_bugs"]["1"] == [
        {"condition": "raw-plain", "trial": 3, "reason": "incomplete"}]


def test_early_fix_is_complete_but_extra_success_cannot_inflate_rate():
    rows = [run(trial=t, turns=1 if t == 1 else 4, fixed=t == 1) for t in (1, 2, 3)]
    rows.append(run(trial=4, fixed=True))
    selected, coverage = select_complete_runs(rows, conditions=("baseline",))
    assert len(selected) == 3
    assert coverage["complete_bugs"] == ["1"]
    assert coverage["ignored_out_of_budget_records"] == 1


def test_duplicate_trials_are_not_cherry_picked():
    rows = [run(trial=t) for t in (1, 2, 3)] + [run(trial=1, fixed=True)]
    selected, coverage = select_complete_runs(rows, conditions=("baseline",))
    assert not selected
    assert coverage["excluded_bugs"]["1"][0]["reason"] == "duplicate"


@pytest.mark.parametrize("trial", [None, True, 1.5, "invalid", 0])
def test_invalid_trial_cannot_supply_missing_first_trial(trial):
    selected, coverage = select_complete_runs(
        [run(trial=trial)], repeats=1, conditions=("baseline",))
    assert not selected
    assert coverage["excluded_bugs"]["1"][0]["reason"] == "missing"


def test_missing_condition_does_not_disappear_from_denominator():
    selected, coverage = select_complete_runs([run(trial=t) for t in (1, 2, 3)],
                                             conditions=("baseline", "raw-plain"))
    assert not selected
    assert len(coverage["excluded_bugs"]["1"]) == 3


def test_promotion_axes_stay_separate():
    rows = [run(trial=t) for t in (1, 2, 3)]
    rows += [run(trial=t, allow_promotion=False, fixed=True) for t in (1, 2, 3)]
    selected, _ = select_complete_runs(rows, conditions=("baseline",))
    assert len(selected) == 3
    assert not any(r["totals"]["fixed"] for r in selected)


def test_completion_agrees_with_resume(tmp_path):
    for index, row in enumerate([run(), run(turns=1), run(fixed=True, turns=1),
                                 run(llm_error="outage"), run(turns=0)]):
        path = tmp_path / f"{index}.json"
        path.write_text(json.dumps(row), encoding="utf-8")
        assert completed_record(row) == record_is_complete(str(path))


def write_bug(directory, bug, *, partial=False):
    for condition in CONDITIONS:
        for trial in (1, 2, 3):
            if partial and condition == "raw-plain" and trial == 3:
                continue
            row = run(bug=bug, condition=condition, trial=trial,
                      fixed=condition == "raw-plain" and trial == 1)
            path = directory / f"{bug}.{condition}.t{trial}.json"
            path.write_text(json.dumps(row), encoding="utf-8")


def test_summary_and_significance_use_same_complete_panel(tmp_path, capsys):
    write_bug(tmp_path, "1")
    write_bug(tmp_path, "2", partial=True)
    assert summarize_results.main([str(tmp_path), "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["paired_bug_count"] == 1
    assert summary["paired"]["raw-plain"]["runs"] == 3
    assert summary["all"]["raw-plain"]["runs"] == 5
    assert analyze_significance.main([str(tmp_path), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["analysis_status"] == "interim_descriptive"
    assert all(row["n_bugs"] == 1 for row in report["primary"])
    anchor = report["primary"][-1]
    assert anchor["only_better"] == 1
    assert anchor["only_worse"] == 0


def test_final_refuses_missing_planned_bug(tmp_path, capsys):
    records = tmp_path / "runs"
    records.mkdir()
    write_bug(records, "1")
    sample = tmp_path / "sample.json"
    sample.write_text('["1", "2"]', encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        analyze_significance.main([str(records), "--final", "--sample", str(sample)])
    assert error.value.code == 2
    assert "missing matched bugs: 2" in capsys.readouterr().err


def test_final_excludes_extra_diagnostic_bugs(tmp_path, capsys):
    records = tmp_path / "runs"
    records.mkdir()
    write_bug(records, "1")
    write_bug(records, "diagnostic")
    sample = tmp_path / "sample.json"
    sample.write_text('["1"]', encoding="utf-8")
    assert analyze_significance.main([str(records), "--final", "--sample", str(sample), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["coverage"]["complete_bugs"] == ["1"]
    assert report["coverage"]["excluded_extra_bugs"] == ["diagnostic"]
    assert all(row["n_bugs"] == 1 for row in report["primary"])


def test_missing_primary_pairs_do_not_reduce_bh_family(tmp_path, capsys):
    for condition in ("raw-plain", "baseline"):
        for trial in (1, 2, 3):
            row = run(condition=condition, trial=trial, fixed=condition == "raw-plain")
            (tmp_path / f"1.{condition}.{trial}.json").write_text(json.dumps(row), encoding="utf-8")
    assert analyze_significance.main([str(tmp_path), "--json", "--condition", "raw-plain", "baseline"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["registered_family_size"] == 4
    assert report["primary"][0]["p_one_sided"] == 0.5
    assert report["primary"][0]["p_bh"] == 1.0
