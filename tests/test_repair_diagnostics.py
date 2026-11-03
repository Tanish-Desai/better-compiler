"""Saved diagnostics must remain paired to their exact proposal, including failures."""
import json
from pathlib import Path

from scripts.diagnose_repairs import diagnose_run, main


HUNK = "int f() {\n  return 0;\n}"


def edit(value):
    return f"<<<<<<< SEARCH\n  return 0;\n=======\n  return {value};\n>>>>>>> REPLACE"


def saved(turns):
    messages = [{"role": "user", "content":
                 f"Please modify the following code in file.cpp to fix the bug:\n```cpp\n{HUNK}\n```"}]
    iterations = []
    for reply, patch, stage, feedback in turns:
        iterations.append({"patch": patch, "validation": {"stage": stage, "passed": feedback is None,
                                                          "failure_kind": "test_failure" if stage == "regression" else "error"},
                           "llm": {"finish_reason": "length" if patch == "truncated" else "stop"}})
        messages.append({"role": "assistant", "content": reply})
        if feedback is not None:
            messages.append({"role": "user", "content": "Feedback:\n" + feedback +
                             "\nPlease adjust code according to the feedback.\nReturn edits."})
    return {"bug_id": "115575", "condition": "raw-plain", "model": "test", "trial": 1,
            "totals": {"fixed": False}, "iterations": iterations,
            "notes": {"experiment_config": {"model": "test", "harness_version": "repair-v3",
                                             "patch_format": "search-replace-v1"}},
            "certificate": {"log": {"messages": messages}}}


def test_earlier_lit_failure_is_paired_with_earlier_patch_not_later_build_failure():
    run = saved([(edit(1), "applied", "regression", "FAIL: LLVM :: vector-test.ll\nCHECK: expected shufflevector"),
                 (edit(2), "applied", "build", "file.cpp:1: error: no member named broken")])
    report = diagnose_run(Path("run.json"), run)
    first, second = report["turns"]
    assert "+  return 1;" in first["patch_diff"] and "return 2;" not in first["patch_diff"]
    assert first["regression_tests"] == ["LLVM :: vector-test.ll"]
    assert "shufflevector" in first["returned_feedback"]
    assert "broken" in second["returned_feedback"] and "shufflevector" not in second["returned_feedback"]
    assert second["build_diagnostic_category"] == "compiler_diagnostic"


def test_invalid_format_can_mean_an_exact_anchor_failure_and_keeps_rejected_reply():
    reply = edit(1).replace("return 0;", "return missing;")
    run = saved([(reply, "invalid_format", "no_patch", "No source was changed.")])
    turn = diagnose_run(Path("run.json"), run)["turns"][0]
    assert "SEARCH was not found exactly" in turn["replay_error"]
    assert turn["reply"] == reply and turn["patch_diff"] == ""


def test_parseable_truncated_prefix_never_appears_as_an_applied_patch():
    run = saved([(edit(1), "truncated", "no_patch", "Output limit: no patch applied")])
    turn = diagnose_run(Path("run.json"), run)["turns"][0]
    assert turn["finish_reason"] == "length"
    assert turn["patch"] == "truncated" and turn["patch_diff"] == ""


def test_absent_final_feedback_is_not_filled_from_another_turn():
    run = saved([(edit(1), "applied", "regression", "FAIL: LLVM :: previous.ll"),
                 (edit(2), "applied", "regression", None)])
    report = diagnose_run(Path("run.json"), run)
    assert report["turns"][0]["feedback_available"]
    assert not report["turns"][1]["feedback_available"]
    assert report["turns"][1]["regression_tests"] == []


def test_missing_reply_does_not_shift_another_patch_into_its_turn():
    run = saved([(edit(1), "applied", "regression", "FAIL: LLVM :: first.ll"),
                 (edit(2), "applied", "build", "error: second patch")])
    run["certificate"]["log"]["messages"].pop(1)
    report = diagnose_run(Path("run.json"), run)
    assert not report["reply_iteration_count_match"]
    assert report["unaligned_saved_replies"] == [edit(2)]
    assert all(not row["patch_diff"] and not row["feedback_available"] for row in report["turns"])


def test_cli_json_keeps_full_evidence_and_does_not_modify_records(tmp_path, capsys):
    log = "FAIL: LLVM :: example.ll\n" + "original diagnostic\n" * 300
    run = saved([(edit(1), "applied", "regression", log)])
    path = tmp_path / "115575.raw-plain.json"
    path.write_text(json.dumps(run))
    original = path.read_bytes()
    assert main([str(tmp_path), "--max-chars", "100"]) == 0
    console = capsys.readouterr().out
    assert "Console excerpt shortened" in console
    assert main([str(tmp_path), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["runs"][0]["turns"][0]["returned_feedback"] == log
    assert path.read_bytes() == original
