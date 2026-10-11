"""Regression tests for accidentally resuming or analysing a different experiment."""

from pathlib import Path
import subprocess
import sys

import pytest

from ce.benchmark import Iteration, RunLog, load_runs
from ce.experiment import ExperimentMismatch, load_experiment_runs, prepare_output

ROOT = Path(__file__).resolve().parent.parent


def write_run(directory, config, bug="111"):
    run = RunLog(bug, "baseline", model=config["model"],
                 notes={"experiment_config": config}, max_iterations=1)
    run.record(Iteration(0, "baseline", False))
    return run.write(str(directory))


def test_same_experiment_resumes_and_manifest_is_not_a_run(tmp_path):
    config = {"model": "qwen3", "harness_version": "v2", "temperature": 0.8}
    prepare_output(str(tmp_path), config)
    write_run(tmp_path, config)
    prepare_output(str(tmp_path), config)
    assert len(load_runs(str(tmp_path))) == 1
    assert len(load_experiment_runs(str(tmp_path))) == 1


@pytest.mark.parametrize("changed", [
    {"model": "qwen2.5"}, {"harness_version": "v1"},
    {"temperature": 0.0}, {"max_iterations": 6}, {"oracle_budget": 100},
])
def test_different_experiment_refused_before_records_are_replaced(tmp_path, changed):
    config = {"model": "qwen3", "harness_version": "v2", "temperature": 0.8,
              "max_iterations": 4, "oracle_budget": 400}
    path = Path(write_run(tmp_path, config))
    original = path.read_bytes()
    with pytest.raises(ExperimentMismatch):
        prepare_output(str(tmp_path), config | changed)
    assert path.read_bytes() == original
    assert not (tmp_path / ".experiment.json").exists()


def test_analysis_requires_model_selection_for_mixed_runs(tmp_path):
    write_run(tmp_path, {"model": "qwen3"}, "111")
    write_run(tmp_path, {"model": "qwen2.5"}, "222")
    with pytest.raises(ExperimentMismatch):
        load_experiment_runs(str(tmp_path))
    assert len(load_experiment_runs(str(tmp_path), "qwen3")) == 1


def test_model_selection_does_not_merge_different_prompts(tmp_path):
    write_run(tmp_path, {"model": "qwen3", "prompt_hash": "old"}, "111")
    write_run(tmp_path, {"model": "qwen3", "prompt_hash": "new"}, "222")
    with pytest.raises(ExperimentMismatch):
        load_experiment_runs(str(tmp_path), "qwen3")


def test_historical_model_upgrade_cannot_skip_old_cell(tmp_path):
    run = RunLog("111", "baseline", model="qwen2.5")
    run.record(Iteration(0, "baseline", False))
    path = Path(run.write(str(tmp_path)))
    original = path.read_bytes()
    with pytest.raises(ExperimentMismatch):
        prepare_output(str(tmp_path), {"model": "qwen3", "harness_version": "v2"})
    assert path.read_bytes() == original


def test_plan_does_not_need_endpoint_or_llvm_or_create_output(tmp_path, monkeypatch):
    import examples.repair_experiment as rx
    for key in ("LAB_DATASET_DIR", "LAB_LLM_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(rx, "_import_benchmark", lambda: pytest.fail("LLVM imported"))
    out = tmp_path / "unused"
    assert rx.main(["115575", "--out", str(out), "--plan"]) == 0
    assert not out.exists()


def test_wrong_served_model_fails_before_llvm_or_output_creation(tmp_path, monkeypatch):
    import examples.repair_experiment as rx
    monkeypatch.setenv("LAB_LLM_TOKEN", "test")
    monkeypatch.setattr(rx, "_import_benchmark", lambda: pytest.fail("LLVM imported"))

    class WrongEndpoint:
        def check_endpoint(self):
            raise RuntimeError("model not served")

    monkeypatch.setattr(rx, "Model", WrongEndpoint)
    out = tmp_path / "unused"
    with pytest.raises(SystemExit) as error:
        rx.main(["115575", "--out", str(out)])
    assert error.value.code == 2
    assert not out.exists()


def test_baseline_build_failure_stops_sweep_instead_of_repeating_every_condition(tmp_path, monkeypatch):
    import examples.repair_experiment as rx
    monkeypatch.setenv("LAB_LLM_TOKEN", "test")
    monkeypatch.setattr(rx, "_import_benchmark", lambda: None)
    monkeypatch.setattr(rx, "Model", lambda: type("Endpoint", (), {"check_endpoint": lambda self: None})())
    calls = []

    def broken(*args, **kwargs):
        calls.append(args)
        raise rx.BaselineBuildError("compile job killed")

    monkeypatch.setattr(rx, "repair", broken)
    assert rx.main(["115575", "--out", str(tmp_path / "runs"),
                    "--repeat", "3", "--condition", "baseline", "raw-plain"]) == 1
    assert len(calls) == 1


@pytest.mark.parametrize("script", ["scripts/triage_sweep.py", "scripts/inspect_patches.py",
                                    "examples/summarize_results.py", "examples/analyze_significance.py"])
def test_cli_refuses_mixed_results_even_in_json_mode(tmp_path, script):
    write_run(tmp_path, {"model": "qwen3"}, "111")
    write_run(tmp_path, {"model": "qwen2.5"}, "222")
    result = subprocess.run([sys.executable, str(ROOT / script), str(tmp_path), "--json"],
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert "experiment configurations" in result.stderr
    assert not result.stdout
