"""Measure physical feedback work and freeze the actual dataset/guard inputs."""

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from ce.alive import AliveRun, FunctionResult
from ce.benchmark import Iteration, RunLog
import ce.feedback as feedback
from ce.oracle import Oracle, Violation
from ce.reduction import make_reduction
from examples import repair_experiment as runner


ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("failed", [False, True])
def test_refresh_cost_is_distinct_from_candidate_budget_and_survives_fallback(monkeypatch, failed):
    source = "define i32 @f(i32 %x) {\n  %a = add i32 %x, 0\n  ret i32 %a\n}\n"
    target = source.replace("ret i32 %a", "ret i32 2")
    small_source = "define i32 @f(i32 %x) {\n  ret i32 %x\n}\n"
    small_target = small_source.replace("ret i32 %x", "ret i32 2")
    fn = FunctionResult(name="@f", src_ir=source, tgt_ir=target, verified=False,
                        error_class="Value mismatch", src_value="1", tgt_value="2")
    parsed = AliveRun(functions=[fn], num_incorrect=1)
    monkeypatch.setattr(feedback, "parse_alive_output", lambda text: parsed)
    reduction = make_reduction("iraware", source, target, small_source, small_target,
                               passes_applied=["slice"], oracle_stats={"oracle_calls": 5}, seconds=1.25)
    monkeypatch.setattr(feedback, "_apply_reduction", lambda *a, **kw: (small_source, small_target, reduction))
    refresh = AliveRun(tool_error="refresh timeout") if failed else parsed
    monkeypatch.setattr(feedback, "run_alive_tv", lambda *a, **kw: refresh)
    ticks = iter((10.0, 10.25))
    monkeypatch.setattr(feedback.time, "perf_counter", lambda: next(ticks, 10.25))
    result = feedback.build_feedback(source, target, "iraware-plain", alive_output="stored report")
    summary = result.summary()
    assert summary["oracle_calls"] == 5
    assert summary["feedback_refresh_calls"] == 1
    assert summary["feedback_refresh_seconds"] == 0.25
    assert summary["feedback_refresh_tool_failures"] == int(failed)
    assert summary["reduction_fallback"] == failed
    run = RunLog("1", "iraware-plain", max_iterations=1)
    run.record(Iteration(0, "iraware-plain", True, feedback=summary))
    totals = run.totals()
    assert totals["oracle_calls"] == 5
    assert totals["feedback_refresh_calls"] == 1
    assert totals["reduction_and_refresh_calls"] == 6
    assert totals["reduction_and_refresh_seconds"] == 1.5


@pytest.mark.parametrize("condition", ["raw-plain", "baseline"])
def test_no_reduction_does_not_invent_refresh_cost(monkeypatch, condition):
    fn = FunctionResult(name="@f", src_ir="", tgt_ir="", verified=False,
                        error_class="Value mismatch", src_value="1", tgt_value="2")
    monkeypatch.setattr(feedback, "parse_alive_output", lambda text: AliveRun(functions=[fn], num_incorrect=1))
    monkeypatch.setattr(feedback, "run_alive_tv", lambda *a, **kw: pytest.fail("unexpected verifier refresh"))
    summary = feedback.build_feedback("", "", condition, alive_output="stored").summary()
    assert summary["feedback_refresh_calls"] == 0
    assert summary["feedback_refresh_seconds"] == 0


def test_external_shim_logs_tool_failures_and_reason(monkeypatch, tmp_path):
    import ce._llvmreduce_test as shim
    candidate = tmp_path / "candidate.ll"
    other = tmp_path / "other.ll"
    log = tmp_path / "checks.jsonl"
    candidate.write_text("candidate")
    other.write_text("other")
    log.write_text("")
    for key, value in {"CE_LLVMREDUCE_SIDE": "src", "CE_LLVMREDUCE_OTHER_FILE": str(other),
                       "CE_LLVMREDUCE_LOG": str(log), "CE_LLVMREDUCE_TARGET_FUNCTION": "@f",
                       "CE_LLVMREDUCE_MAX_CALLS": "10"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(shim.sys, "argv", ["shim", str(candidate)])
    monkeypatch.setattr("ce.oracle.run_alive_tv", lambda *a, **kw: AliveRun(tool_error="tool timed out"))
    assert shim.main() == 1
    item = json.loads(log.read_text())
    assert item["tool_failures"] == 1
    assert "tool timed out" in item["reason"]


def test_external_wrapper_merges_actual_child_tool_failures(monkeypatch):
    import importlib
    wrapper = importlib.import_module("ce.reduce_llvmreduce")

    def child(command, **kwargs):
        env = kwargs["env"]
        assert env["CE_LLVMREDUCE_MAX_CALLS"] == "399"
        Path(env["CE_LLVMREDUCE_LOG"]).write_text(
            json.dumps({"ok": False, "seconds": 0.5, "tool_failures": 1}) + "\n" +
            json.dumps({"ok": True, "seconds": 0.25, "tool_failures": 0}) + "\n")
        (Path(kwargs["cwd"]) / "reduced.ll").write_text("reduced source")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(wrapper.subprocess, "run", child)
    oracle = Oracle(Violation("@f", "Value mismatch"), max_calls=400)
    text, stats = wrapper._reduce_one_side("source", "target", "src", oracle, "llvm-reduce", 2)
    assert text == "reduced source"
    oracle.record_external(**stats)
    assert oracle.calls == 2
    assert oracle.accepted == oracle.rejected == oracle.tool_failures == 1
    assert oracle.seconds == 0.75


def test_dataset_identity_is_portable_cached_and_changes_with_selected_inputs(monkeypatch, tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    for directory in (first, second):
        (directory / "1.json").write_text('{"test": "before"}')
        (directory / "2.json").write_text('{"test": "untouched"}')
    runner._hash_dataset_records.cache_clear()
    config = SimpleNamespace(dataset_bug_ids=("1", "2"))
    monkeypatch.setenv("LAB_DATASET_DIR", str(first))
    baseline = runner.dataset_identity(config)
    assert runner.dataset_identity(config) == baseline
    assert runner._hash_dataset_records.cache_info().hits == 1
    monkeypatch.setenv("LAB_DATASET_DIR", str(second))
    assert runner.dataset_identity(config) == baseline
    changed = second / "1.json"
    stat = changed.stat()
    changed.write_text('{"test": "after!"}')
    os.utime(changed, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    updated = runner.dataset_identity(config)
    assert updated["sha256"] != baseline["sha256"]
    assert updated["records"]["1"] != baseline["records"]["1"]
    assert updated["records"]["2"] == baseline["records"]["2"]


@pytest.mark.parametrize("module", ["protocol.py", "experiment.py"])
def test_harness_identity_includes_protocol_and_resume_guard_sources(monkeypatch, tmp_path, module):
    # Copy the relevant source tree; mutate only its guard module, never the
    # real checkout. Data files and physical checkout paths are irrelevant.
    files = list((ROOT / "ce").glob("*.py")) + [ROOT / "examples/repair_experiment.py",
             ROOT / "llvm-apr-benchmark/scripts/llvm_helper.py",
             ROOT / "llvm-apr-benchmark/scripts/lab_env.py"]
    for file in files:
        destination = tmp_path / file.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(file.read_bytes())
    monkeypatch.setattr(runner, "_HERE", str(tmp_path / "examples"))
    monkeypatch.delenv("LAB_DATASET_DIR", raising=False)
    args = SimpleNamespace(max_iterations=4, oracle_budget=400, strictness="error_class")
    first = runner.experiment_config(args)
    changed = tmp_path / "ce" / module
    changed.write_bytes(changed.read_bytes() + b"\n# changed guard behavior\n")
    assert runner.experiment_config(args)["harness_code_hash"] != first["harness_code_hash"]


@pytest.mark.parametrize("limit", ["0", "-1", "not-an-integer"])
def test_invalid_completion_limit_rejected_by_config_and_model(monkeypatch, limit):
    monkeypatch.setenv("LAB_LLM_MAX_TOKENS", limit)
    args = SimpleNamespace(max_iterations=4, oracle_budget=400, strictness="error_class")
    with pytest.raises(ValueError, match="LAB_LLM_MAX_TOKENS must be a positive integer"):
        runner.experiment_config(args)
    # Validate before constructing the SDK client or reading credentials.
    with pytest.raises(ValueError, match="LAB_LLM_MAX_TOKENS must be a positive integer"):
        runner.Model()


def test_completion_limit_fails_cleanly_before_plan_writes(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LAB_LLM_MAX_TOKENS", "0")
    monkeypatch.delenv("LAB_DATASET_DIR", raising=False)
    destination = tmp_path / "new-results"
    with pytest.raises(SystemExit) as error:
        runner.main(["--plan", "--out", str(destination), "1"])
    assert error.value.code == 2
    assert "LAB_LLM_MAX_TOKENS must be a positive integer" in capsys.readouterr().err
    assert not destination.exists()


def test_prompt_literal_reformat_preserves_exact_model_input():
    assert hashlib.sha256(runner.FORMAT_REQUIREMENT.encode()).hexdigest() == (
        "4eef2bd02f3d1d4a71e5257a2ff92267984c098e6f0ad08dc66172b5dbb73168")
    assert hashlib.sha256(runner.SYSTEM_PROMPT.encode()).hexdigest() == (
        "95a36ffc45d1665b72e07e013f7e73cc7ed16a514509156a2044ce253468cc92")
