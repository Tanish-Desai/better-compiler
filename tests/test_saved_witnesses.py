"""Real-witness extraction and cost/validity accounting without external tools."""

import json

from ce.alive import AliveRun, FunctionResult
from ce.feedback import Feedback
from ce.reduction import make_reduction
from scripts import compare_saved_witnesses as compare


SRC = "define i32 @f(i32 %x) {\n  %dead = add i32 %x, 0\n  ret i32 %x\n}\n"
TGT = "define i32 @f(i32 %x) {\n  %dead = add i32 %x, 0\n  ret i32 2\n}\n"
SMALL_SRC = "define i32 @f(i32 %x) {\n  ret i32 %x\n}\n"
SMALL_TGT = "define i32 @f(i32 %x) {\n  ret i32 2\n}\n"
ARGS = ("-src-unroll=4", "-tgt-unroll=4")


def saved_record(condition="raw-plain", *, src=SRC, tgt=TGT, trial=1):
    if condition == "raw-plain":
        payload = (f"Source:\n{src.strip()}\n\nTarget:\n{tgt.strip()}\n\n"
                   "Transformation doesn't verify!\nERROR: Value mismatch\n\n"
                   "Source:\n  %x = 1\n\nTarget:\n  %x = 2\n\nSummary:\n")
    else:
        payload = (f"SOURCE (pre-transformation IR):\n{src.strip()}\n\n"
                   f"TARGET (post-transformation IR):\n{tgt.strip()}\n\n"
                   "WHAT THE TRANSFORMATION CHANGED:\n  ...\n")
    return {"bug_id": 1, "condition": condition, "trial": trial,
            "iterations": [{"feedback": {"failed_test": "f"}}],
            "certificate": {"log": {"messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "Detailed information:\n" + payload +
                 "\nPlease modify the following code in x.cpp to fix the bug:\n```cpp\ncode\n```"},
                {"role": "assistant", "content": "a model edit"},
                {"role": "user", "content": "Feedback:\nSource:\nNOT INITIAL\n\nTarget:\nLATER\n\n"
                 "Transformation doesn't verify!"},
            ]}}}


def dataset(*, additional_args="-src-unroll=4 -tgt-unroll=4", tests=None):
    return {"bug_id": 1, "patch": "PRIVATE REFERENCE FIX MUST NOT BE EXPORTED",
            "tests": [{"file": "llvm/test/reproducer.ll", "tests": tests or [
                {"test_name": "f", "test_body": SRC, "additional_args": additional_args}]}]}


def blobs(value, name="1.json"):
    return {name: json.dumps(value).encode()}


def live_run(src=SRC, tgt=TGT, *, error_class="Value mismatch", name="@f"):
    fn = FunctionResult(name=name, src_ir=src, tgt_ir=tgt, verified=False,
                        error_class=error_class, src_value="1", tgt_value="2")
    return AliveRun(functions=[fn], num_incorrect=1, raw="fresh, not the saved report")


def witness():
    return compare.Witness("1", SRC, TGT, ARGS, "f", ["saved.json"])


def test_extraction_agrees_across_layout_and_trial_and_restores_verifier_context():
    records = {**blobs(saved_record(), "plain.json"),
               **blobs(saved_record("raw-structured", trial=2), "structured.json")}
    witnesses, issues = compare.collect_witnesses(records, blobs(dataset()))
    assert not issues
    assert len(witnesses) == 1
    actual = witnesses[0]
    assert (actual.src, actual.tgt, actual.extra_args) == (SRC, TGT, ARGS)
    assert actual.origins == ["plain.json", "structured.json"]
    assert actual.dataset_sha256 == compare.digest(next(iter(blobs(dataset()).values())))
    assert "PRIVATE REFERENCE" not in json.dumps(compare.asdict(actual))


def test_only_initial_message_is_extracted_and_nested_trace_labels_are_ignored():
    record = saved_record()
    content = record["certificate"]["log"]["messages"][1]["content"]
    record["certificate"]["log"]["messages"][1]["content"] = content.replace("\n", "\r\n")
    assert compare.initial_pair(record) == (SRC, TGT)


def test_conflicting_trial_is_excluded_instead_of_picking_a_convenient_witness():
    records = {**blobs(saved_record(), "one.json"),
               **blobs(saved_record(tgt=SMALL_TGT, trial=2), "two.json")}
    witnesses, issues = compare.collect_witnesses(records, blobs(dataset()))
    assert witnesses == []
    assert "disagree" in issues[0]["error"]
    assert issues[0]["origins"] == ["one.json", "two.json"]


def test_a_malformed_raw_copy_cannot_be_silently_ignored():
    malformed = saved_record()
    malformed["certificate"] = {}
    records = {**blobs(saved_record(), "good.json"), **blobs(malformed, "bad.json")}
    witnesses, issues = compare.collect_witnesses(records, blobs(dataset()))
    assert not witnesses
    assert {issue["stage"] for issue in issues} == {"extract", "corpus"}


def test_missing_raw_coverage_and_nonjson_records_remain_visible():
    records = {**blobs({"bug_id": 2, "condition": "baseline"}, "baseline.json"), "bad.json": b"bad"}
    witnesses, issues = compare.collect_witnesses(records, {})
    assert not witnesses
    assert any(issue.get("bug_id") == "2" and "no usable" in issue["error"] for issue in issues)
    assert any(issue["file"] == "bad.json" for issue in issues if "file" in issue)


def test_ambiguous_test_arguments_reject_the_case_rather_than_default_to_no_args():
    tests = [{"test_name": "f", "test_body": "different body", "additional_args": "-src-unroll=4"},
             {"test_name": "f", "test_body": "another body", "additional_args": "-src-unroll=8"}]
    witnesses, issues = compare.collect_witnesses(blobs(saved_record()), blobs(dataset(tests=tests)))
    assert not witnesses
    assert any("ambiguous verifier arguments" in issue["error"] for issue in issues)


def test_exact_source_body_resolves_duplicate_test_names():
    tests = [{"test_name": "f", "test_body": SRC, "additional_args": "-src-unroll=4"},
             {"test_name": "f", "test_body": "another body", "additional_args": "-src-unroll=8"}]
    witnesses, issues = compare.collect_witnesses(blobs(saved_record()), blobs(dataset(tests=tests)))
    assert not issues
    assert witnesses[0].extra_args == ("-src-unroll=4",)


def test_equal_caps_ablation_fresh_checks_and_separate_physical_costs():
    verifier_calls, builders = [], []

    def verify(src, tgt, args, **kwargs):
        verifier_calls.append((src, tgt, args, kwargs))
        return live_run(src, tgt)

    def build(src, tgt, condition, **kwargs):
        builders.append((condition, kwargs))
        if condition == "raw-plain":
            return Feedback("raw", condition)
        red = make_reduction(condition, src, tgt, SMALL_SRC, SMALL_TGT,
                             oracle_stats={"oracle_calls": 7, "oracle_seconds": 2.5,
                                           "oracle_tool_failures": 3})
        return Feedback("rendered smaller witness", condition, reduction=red,
                        metrics={"feedback_refresh_calls": 1, "feedback_refresh_seconds": 0.75})

    result = compare.compare_witness(witness(), alive_tv="alive-tv", verifier=verify,
                                     feedback_builder=build)
    assert result["establishment_calls"] == 1
    assert len(verifier_calls) == 6  # One shared establishment plus five independent postchecks.
    assert all(call[2] == ARGS for call in verifier_calls)
    assert [r["method"] for r in result["methods"]] == list(compare.METHODS)
    assert all(kwargs["oracle_budget"] == 400 and kwargs["oracle_strictness"] == "error_class"
               and kwargs["extra_args"] == ARGS and kwargs["alive_output"] == "fresh, not the saved report"
               for _, kwargs in builders)
    assert [kwargs["allow_promotion"] for _, kwargs in builders] == [True, True, True, True, False]
    raw, reduced = result["methods"][:2]
    assert raw["total_physical_calls"] == 1
    assert raw["displayed_size"] == result["original_size"]
    assert reduced["total_physical_calls"] == 9  # Seven candidates + refresh + independent postcheck.
    assert reduced["candidate_tool_failures"] == 3
    assert reduced["refresh_seconds"] == 0.75
    assert reduced["displayed_src"] == SMALL_SRC
    assert reduced["displayed_size"]["instructions"] < result["original_size"]["instructions"]
    assert all(row["status"] == "ok" and row["same_violation_fresh"] for row in result["methods"])


def test_a_different_function_or_error_class_is_not_a_success():
    count = 0

    def verify(src, tgt, args, **kwargs):
        nonlocal count
        count += 1
        if count == 1:
            return live_run()
        return live_run(error_class="Target is more poisonous than source" if count % 2 else "Value mismatch",
                        name="@f" if count % 2 else "@unrelated")

    result = compare.compare_witness(witness(), alive_tv="alive-tv", verifier=verify,
                                     feedback_builder=lambda s, t, condition, **kw: Feedback("raw", condition))
    assert all(row["status"] == "failed" and not row["same_violation_fresh"] for row in result["methods"])


def test_hybrid_spends_only_the_remaining_budget_and_keeps_the_smaller_seed(monkeypatch):
    def paired(src, tgt, oracle, violation):
        assert (src, tgt, oracle.max_calls) == (SRC, TGT, 8)
        oracle.record_external(calls=3)
        return make_reduction("iraware", src, tgt, SMALL_SRC, SMALL_TGT)

    def sequential(src, tgt, oracle):
        assert (src, tgt) == (SMALL_SRC, SMALL_TGT)
        assert (oracle.calls, oracle.max_calls) == (3, 32)
        oracle.record_external(calls=29)
        return make_reduction("llvmreduce", src, tgt, SRC, TGT)

    monkeypatch.setattr(compare, "reduce_iraware", paired)
    monkeypatch.setattr(compare, "reduce_llvmreduce", sequential)
    checks = []

    def verify(src, tgt, args, **kwargs):
        assert args == ARGS
        checks.append((src, tgt))
        return live_run(src, tgt)

    result = compare.compare_witness(witness(), alive_tv="alive-tv", budget=32,
                                     hybrid_prefix_calls=8, verifier=verify,
                                     feedback_builder=lambda s, t, condition, **kw: Feedback("raw", condition))
    hybrid = result["methods"][-1]
    assert hybrid["method"] == compare.HYBRID
    assert hybrid["feedback"]["hybrid_selected_stage"] == "iraware"
    assert hybrid["candidate_calls"] == 32
    assert hybrid["total_physical_calls"] == 34
    assert checks[-2:] == [(SMALL_SRC, SMALL_TGT)] * 2
    assert hybrid["status"] == "ok" and hybrid["same_violation_fresh"]


def test_invalid_original_is_retained_with_all_five_methods_explicitly_skipped():
    def forbidden(*args, **kwargs):
        raise AssertionError("reduction must not run without an established failure")

    result = compare.compare_witness(witness(), alive_tv="alive-tv",
                                     verifier=lambda *a, **kw: AliveRun(tool_error="timeout"),
                                     feedback_builder=forbidden)
    assert result["status"] == "skipped"
    assert len(result["methods"]) == 5
    assert all(row["status"] == "skipped" for row in result["methods"])
    assert result["establishment"]["tool_error"] == "timeout"


def test_an_unidentified_violation_cannot_satisfy_the_same_error_class_policy():
    result = compare.compare_witness(witness(), alive_tv="alive-tv",
                                     verifier=lambda *a, **kw: live_run(error_class=None))
    assert result["status"] == "skipped"
    assert "parsed function and error class" in result["reason"]


def test_fallback_reports_the_original_display_and_costs_incurred():
    def build(src, tgt, condition, **kwargs):
        red = make_reduction("stub", src, tgt, src, tgt, error="refresh failed",
                             oracle_stats={"oracle_calls": 4})
        return Feedback("original display", condition, reduction=red,
                        metrics={"reduction_fallback": True, "attempted_size_after": {"instructions": 2},
                                 "feedback_refresh_calls": 1})

    result = compare.compare_witness(witness(), alive_tv="alive-tv", feedback_builder=build,
                                     verifier=lambda *a, **kw: live_run())
    assert all(row["status"] == "fallback" and row["displayed_src"] == SRC
               and row["displayed_size"] == result["original_size"]
               and row["total_physical_calls"] == 6 for row in result["methods"])


def test_a_method_exception_is_visible_and_does_not_suppress_later_arms():
    def build(src, tgt, condition, **kwargs):
        if condition == "generic-plain":
            raise RuntimeError("broken reducer")
        return Feedback("raw", condition)

    result = compare.compare_witness(witness(), alive_tv="alive-tv", feedback_builder=build,
                                     verifier=lambda *a, **kw: live_run())
    assert result["methods"][1]["status"] == "error"
    assert result["methods"][1]["cost_complete"] is False
    assert len(result["methods"]) == 5
    assert result["methods"][-1]["status"] == "ok"


def test_llvm_binary_override_is_restored_even_on_failure(monkeypatch):
    monkeypatch.setenv("LAB_LLVM_LLVM_REDUCE", "previous")
    try:
        with compare.llvm_reduce_override("chosen"):
            assert compare.os.environ["LAB_LLVM_LLVM_REDUCE"] == "chosen"
            raise RuntimeError("failure")
    except RuntimeError:
        pass
    assert compare.os.environ["LAB_LLVM_LLVM_REDUCE"] == "previous"


def test_plan_needs_no_model_verifier_or_output_directory(tmp_path, monkeypatch):
    records, dataset_dir = tmp_path / "results", tmp_path / "dataset"
    records.mkdir()
    dataset_dir.mkdir()
    (records / "1.raw-plain.json").write_text(json.dumps(saved_record()))
    (dataset_dir / "1.json").write_text(json.dumps(dataset()))

    def forbidden(*args, **kwargs):
        raise AssertionError("--plan must not resolve or run external tools")

    monkeypatch.setattr(compare, "binary_path", forbidden)
    output = tmp_path / "output"
    assert compare.main(["--directory", str(records), "--dataset", str(dataset_dir),
                         "--out", str(output), "--plan"]) == 0
    assert not output.exists()
