"""Snapshot tables must remain reproducible after the live repair parser changes."""

import io
import json
import tarfile

import pytest

from scripts import report_snapshot as report


def fake_archive():
    files = {
        "results-qwen3-v2/.experiment.json": json.dumps({"model": "fixture", "harness_version": "repair-v2"}),
        "data/experiment_sample.json": json.dumps({"tiers": {"easy": [{"bug_id": "1"}, {"bug_id": "2"}]}}),
        "examples/repair_experiment.py": "# frozen fixture runner\n",
        "llvm-apr-benchmark/dataset/1.json": json.dumps({"patch": "reference patch one"}),
        "llvm-apr-benchmark/dataset/2.json": json.dumps({"patch": "reference patch two"}),
    }
    for bug in ("1", "2"):
        for condition in report.CONDITIONS:
            for trial in (1, 2, 3):
                # Bug 2 is intentionally missing one trial, despite an early
                # success in another arm: it cannot enter the matched table.
                if (bug, condition, trial) == ("2", "baseline", 3):
                    continue
                fixed = condition == "raw-structured" and trial == 1
                fb = {"condition": condition, "counterexample": True}
                messages = [
                    {"role": "user", "content": "Please modify the following code in x.cpp to fix the bug:\n```cpp\nbool f() { return false; }\n```"},
                    {"role": "assistant", "content": "```cpp\nbool f() { return true; }\n```"},
                ]
                if not fixed:
                    messages.append({"role": "user", "content": "Feedback:\nx.cpp:1:1: error: invalid type\nPlease adjust code according to the feedback."})
                run = {"bug_id": bug, "condition": condition, "trial": trial, "model": "fixture",
                       "max_iterations": 1, "notes": {},
                       "totals": {"fixed": fixed, "wall_seconds": 12,
                                  "llm_prompt_tokens": 100, "llm_completion_tokens": 20},
                       "iterations": [{"index": 0, "patch": "applied", "fixed": fixed,
                                       "feedback": fb, "validation": {"stage": "regression" if fixed else "build"}}],
                       "certificate": {"patch": f"fixture patch {bug}", "log": {"messages": messages}}}
                files[f"results-qwen3-v2/{bug}.{condition}.t{trial}.json"] = json.dumps(run)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, value in files.items():
            data = value.encode()
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return buffer.getvalue()


@pytest.fixture
def frozen_git(monkeypatch):
    archive = fake_archive()

    def git(*args):
        if args[0] == "rev-parse":
            return b"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
        if args[0] == "archive":
            return archive
        if args[0] == "show":
            return b"2026-10-21T23:03:06+05:30\n"
        raise AssertionError(args)

    monkeypatch.setattr(report, "git", git)


def test_snapshot_generation_is_identical_for_the_same_frozen_inputs(frozen_git):
    first = report.snapshot_report("frozen", "results-qwen3-v2")
    second = report.snapshot_report("frozen", "results-qwen3-v2")
    assert first == second
    assert report.tex_tables(first) == report.tex_tables(second)
    assert first["provenance"]["snapshot_committed_at"] == "2026-10-21T23:03:06+05:30"
    assert "generated_at_utc" not in first["provenance"]


def test_partial_bug_success_is_not_counted_in_the_common_panel(frozen_git):
    result = report.snapshot_report("frozen", "results-qwen3-v2")
    assert result["all_records"] == 53
    assert result["all_fixed_records"] == 2
    assert result["matched_bugs"] == ["1"]
    assert result["matched_records"] == 27
    assert result["matched_fixed_records"] == 1
    assert result["per_condition"]["raw-structured"]["matched_fixed_records"] == 1
    assert result["per_condition"]["raw-structured"]["all_fixed_records"] == 2
    assert all(row["n"] == 1 for row in result["primary_comparisons_descriptive"])


def test_historical_parser_intentionally_replays_the_old_multifence_defect():
    reply = "Explanation\n```cpp\nint first;\n```\nreasoning prose\n```cpp\nint final;\n```"
    code = report.historical_v2_extract(reply)
    assert "reasoning prose" in code
    assert "```" in code
    # Do not replace this function with the current, corrected live parser.


def test_underlying_linker_symbol_is_reported_before_collect2_summary():
    text = "x.cpp:123: undefined reference to `missingFunction()'\ncollect2: error: ld returned 1 exit status"
    assert report.error_signature(text) == "undefined reference to `missingFunction()'"


def test_tex_rows_are_terminated_and_counts_are_scope_specific(frozen_git):
    text = report.tex_tables(report.snapshot_report("frozen", "results-qwen3-v2"))
    assert r"Condition & Bugs / 1 & Runs / 3" in text
    assert r"over all 53 saved runs" in text
    for line in text.splitlines():
        if " & " in line:
            assert r"\\" in line, line
    assert all(f"\\label{{tab:v2-{name}}}" in text for name in ("matched", "pairs", "diagnostics", "reducers"))


def test_cli_writes_machine_data_and_tex_without_touching_originals(frozen_git, tmp_path):
    assert report.main(["--snapshot", "frozen", "--out", str(tmp_path)]) == 0
    json_path = tmp_path / "v2_snapshot.json"
    first = json_path.read_bytes()
    assert json.loads(first)["all_records"] == 53
    assert report.main(["--snapshot", "frozen", "--out", str(tmp_path)]) == 0
    assert json_path.read_bytes() == first
    assert (tmp_path / "v2_snapshot_tables.tex").exists()
