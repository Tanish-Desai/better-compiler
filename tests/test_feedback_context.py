"""Verifier context and displayed-feedback integrity without a live LLVM build."""

from dataclasses import replace
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from ce.alive import AliveRun, FunctionResult
from ce.benchmark import normalize_feedback
import ce.feedback as feedback
from ce.reduction import make_reduction


ROOT = Path(__file__).resolve().parent.parent
ARGS = ("-src-unroll=4", "-tgt-unroll=4")
SOURCE = "define i32 @f(i32 %x) {\n  %dead = add i32 %x, 0\n  ret i32 %x\n}\n"
TARGET = "define i32 @f(i32 %x) {\n  %dead = add i32 %x, 0\n  ret i32 2\n}\n"
SMALL_SOURCE = "define i32 @f(i32 %x) {\n  ret i32 %x\n}\n"
SMALL_TARGET = "define i32 @f(i32 %x) {\n  ret i32 2\n}\n"


def violation(**kwargs):
    return FunctionResult(name="@f", src_ir=SOURCE, tgt_ir=TARGET,
                          verified=False, error_class="Value mismatch",
                          src_value="1", tgt_value="2", **kwargs)


def alive_run(result=None, **kwargs):
    return AliveRun(functions=[result or violation()], num_incorrect=1, **kwargs)


def check_record(**kwargs):
    return {"src": SOURCE, "tgt": TARGET, "log": "saved Alive2 report", **kwargs}


def install_reducer(monkeypatch, *, reduction_error=None):
    seen = []

    def reducer(src, tgt, oracle, *args, **kwargs):
        seen.append(oracle)
        return make_reduction("stub", src, tgt, SMALL_SOURCE, SMALL_TARGET,
                              passes_applied=["slice"], seconds=1.25,
                              oracle_stats={"oracle_calls": 5}, error=reduction_error)

    for name in ("reduce_generic", "reduce_llvmreduce", "reduce_iraware"):
        monkeypatch.setattr(feedback, name, reducer)
    monkeypatch.setattr(feedback, "parse_alive_output", lambda text: alive_run())
    return seen


@pytest.mark.parametrize("condition", [
    "generic-plain", "generic-structured", "llvmreduce-plain",
    "llvmreduce-structured", "iraware-plain", "iraware-structured",
])
def test_test_unroll_args_reach_every_reducer_and_refresh(monkeypatch, condition):
    seen = install_reducer(monkeypatch)
    refresh_calls = []

    def refresh(src, tgt, args, **kwargs):
        refresh_calls.append((src, tgt, args))
        return alive_run(replace(violation(), src_ir=src, tgt_ir=tgt))

    monkeypatch.setattr(feedback, "run_alive_tv", refresh)
    log = [{"result": False, "name": "loop",
            "additional_args": "-src-unroll=4  -tgt-unroll=4",
            "log": check_record()}]
    result = normalize_feedback(log, condition)
    assert len(seen) == 1
    assert seen[0].extra_args == ARGS
    assert refresh_calls == [(SMALL_SOURCE, SMALL_TARGET, ARGS)]
    assert result.summary()["verifier_args"] == list(ARGS)
    assert result.summary()["displayed_pair_changed"]
    assert not result.summary()["reduction_fallback"]


@pytest.mark.parametrize("metadata,override,expected", [
    ({"additional_args": "-src-unroll=4 -tgt-unroll=4"}, {}, ARGS),
    ({"additional_args": list(ARGS)}, {}, ARGS),
    ({"additional_args": "-src-unroll=4 -tgt-unroll=4"}, {"extra_args": ()}, ()),
    ({"additional_args": None}, {}, ()),
    ({}, {}, ()),  # Historical logs have no verifier-argument metadata.
])
def test_direct_check_context_and_explicit_empty_override(monkeypatch, metadata, override, expected):
    seen = install_reducer(monkeypatch)
    monkeypatch.setattr(feedback, "run_alive_tv", lambda *a, **kw: alive_run())
    result = feedback.build_feedback_from_check(check_record(**metadata), "iraware-plain", **override)
    assert seen[0].extra_args == expected
    assert result.summary()["verifier_args"] == list(expected)


def test_outer_context_and_explicit_empty_override_do_not_duplicate_keywords(monkeypatch):
    seen = install_reducer(monkeypatch)
    monkeypatch.setattr(feedback, "run_alive_tv", lambda *a, **kw: alive_run())
    log = [{"result": False, "additional_args": "-src-unroll=4",
            "log": check_record(additional_args="-src-unroll=8")}]
    normalize_feedback(log, "iraware-plain", extra_args=())
    assert seen[0].extra_args == ()
    normalize_feedback(log, "iraware-plain")
    assert seen[1].extra_args == ("-src-unroll=4",)


@pytest.mark.parametrize("strictness,changed,accepted", [
    ("error_class", {"name": "@other"}, False),
    ("error_class", {"error_class": "Target is more poisonous than source"}, False),
    ("error_class", {"tgt_value": "poison"}, True),
    ("error_class_and_kind", {"tgt_value": "poison"}, False),
    ("error_class_and_kind", {}, True),
    ("any_failure", {"name": "@other", "error_class": "Source is more defined than target"}, True),
])
def test_refresh_obeys_the_same_configured_oracle_policy(monkeypatch, strictness, changed, accepted):
    seen = install_reducer(monkeypatch)
    refreshed = replace(violation(), src_ir=SMALL_SOURCE, tgt_ir=SMALL_TARGET, **changed)
    monkeypatch.setattr(feedback, "run_alive_tv", lambda *a, **kw: alive_run(refreshed))
    result = feedback.build_feedback(SOURCE, TARGET, "iraware-plain",
                                     alive_output="saved", oracle_strictness=strictness)
    assert seen[0].strictness == strictness
    assert result.summary()["reduction_fallback"] is (not accepted)
    if accepted:
        assert result.violation is refreshed
    else:
        assert result.violation.name == "@f"
    assert result.summary()["displayed_pair_changed"] is accepted


def test_other_function_failure_cannot_mask_selected_function_becoming_correct(monkeypatch):
    install_reducer(monkeypatch)
    rerun = AliveRun(functions=[replace(violation(), verified=True, error_class=None),
                               replace(violation(), name="@other")],
                     num_correct=1, num_incorrect=1)
    monkeypatch.setattr(feedback, "run_alive_tv", lambda *a, **kw: rerun)
    result = feedback.build_feedback(SOURCE, TARGET, "iraware-plain", alive_output="saved")
    assert result.summary()["reduction_fallback"]
    assert "@other" not in result.text


@pytest.mark.parametrize("rerun", [
    AliveRun(tool_error="process failed"),
    AliveRun(functions=[violation()], num_errors=1),
    AliveRun(functions=[replace(violation(), verified=True)], num_correct=1),
])
def test_refresh_tool_errors_or_no_violation_fall_back_even_under_any_failure(monkeypatch, rerun):
    install_reducer(monkeypatch)
    monkeypatch.setattr(feedback, "run_alive_tv", lambda *a, **kw: rerun)
    result = feedback.build_feedback(SOURCE, TARGET, "iraware-plain", alive_output="saved",
                                     oracle_strictness="any_failure")
    assert result.summary()["reduction_fallback"]


@pytest.mark.parametrize("initial_error", [None, "reducer timed out"])
def test_fallback_measures_displayed_pair_and_retains_attempted_work(monkeypatch, initial_error):
    install_reducer(monkeypatch, reduction_error=initial_error)
    refresh_calls = []

    def refresh(*args, **kwargs):
        refresh_calls.append(args)
        return AliveRun(tool_error="refresh failed")

    monkeypatch.setattr(feedback, "run_alive_tv", refresh)
    result = feedback.build_feedback(SOURCE, TARGET, "iraware-plain", alive_output="saved")
    summary = result.summary()
    assert len(refresh_calls) == (0 if initial_error else 1)
    assert result.reduction.src == SOURCE
    assert result.reduction.tgt == TARGET
    assert summary["size_after"] == summary["size_before"]
    assert summary["shown_instructions"] == summary["original_instructions"]
    assert summary["reduction_ratio_instructions"] == 0
    assert summary["reduction_ratio_chars"] == 0
    assert summary["passes_applied"] == []
    assert summary["attempted_passes_applied"] == ["slice"]
    assert summary["attempted_size_after"]["instructions"] < summary["size_after"]["instructions"]
    assert summary["oracle_calls"] == 5
    assert summary["seconds"] == 1.25
    assert summary["reduction_fallback"]
    assert summary["error"]
    assert not summary["displayed_pair_changed"]


@pytest.fixture
def benchmark_helper(monkeypatch):
    for key in ("LAB_LLVM_DIR", "LAB_LLVM_BUILD_DIR", "LAB_LLVM_ALIVE_TV", "LAB_DATASET_DIR"):
        monkeypatch.setenv(key, "test-placeholder")
    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace())
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: SimpleNamespace(stderr=b"--quiet"))
    spec = importlib.util.spec_from_file_location("feedback_test_llvm_helper",
                                                ROOT / "llvm-apr-benchmark/scripts/llvm_helper.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_benchmark_test_results_preserve_each_tests_own_verifier_args(monkeypatch, benchmark_helper):
    seen = []

    def verify(*args):
        seen.append(args[4])
        return False, check_record()

    monkeypatch.setattr(benchmark_helper, "verify_dispatch", verify)
    tests = [{"file": "loop.ll", "commands": ["opt %s"], "tests": [
        {"test_name": "first", "test_body": SOURCE, "additional_args": "-src-unroll=4 -tgt-unroll=4"},
        {"test_name": "second", "test_body": SOURCE},
    ]}]
    passed, results = benchmark_helper.verify_test_group(False, tests, "miscompilation")
    assert not passed
    assert seen == ["-src-unroll=4 -tgt-unroll=4", None]
    assert [r["additional_args"] for r in results] == seen


def test_direct_alive2_result_preserves_arguments(monkeypatch, benchmark_helper):
    monkeypatch.setattr(benchmark_helper.subprocess, "check_output", lambda *a, **kw: b"1 incorrect transformations")
    passed, result = benchmark_helper.alive2_check(SOURCE, TARGET, "-src-unroll=4 -tgt-unroll=4")
    assert not passed
    assert result["additional_args"] == "-src-unroll=4 -tgt-unroll=4"
