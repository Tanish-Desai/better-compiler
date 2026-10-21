"""Schedules and budgets are frozen before data, not inferred from successful cells."""

import json
from pathlib import Path

import pytest

from ce.protocol import load_protocol
from examples import repair_experiment as rx
from examples import analyze_significance as analysis
from examples import summarize_results as summary


ROOT = Path(__file__).resolve().parent.parent
PROTOCOL = ROOT / "data/repair_v3_protocol.json"


def test_followup_is_exactly_the_remaining_two_tiers_and_all_primary_arms():
    protocol, _ = load_protocol(str(PROTOCOL))
    sample = json.loads((ROOT / "data/experiment_sample.json").read_text())
    expected = [row["bug_id"] for tier in ("medium", "hard") for row in sample["tiers"][tier]]
    assert protocol["bugs"] == expected
    assert not set(expected) & set(protocol["excluded_development_bugs"])
    assert set(protocol["conditions"]) == {arm for pair in analysis.PRIMARY for arm in pair[:2]}
    assert len(expected) * len(protocol["conditions"]) * protocol["repeat"] == 288


def test_protocol_plan_does_not_touch_endpoint_or_llvm(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LAB_LLM_MODEL", "qwen3-coder-30b")
    monkeypatch.setenv("LAB_LLM_TEMP", "0.8")
    monkeypatch.delenv("LAB_LLM_TOKEN", raising=False)
    monkeypatch.setattr(rx, "_import_benchmark", lambda: pytest.fail("LLVM imported"))
    out = tmp_path / "new"
    assert rx.main(["--protocol", str(PROTOCOL), "--out", str(out), "--plan"]) == 0
    assert "288 cells" in capsys.readouterr().out
    assert not out.exists()


@pytest.mark.parametrize("extra", [["--repeat", "1"], ["--max-iterations", "6"],
                                   ["--sample"], ["115575"], ["--no-promotion"]])
def test_runner_cannot_silently_override_frozen_schedule(tmp_path, monkeypatch, extra):
    monkeypatch.setenv("LAB_LLM_MODEL", "qwen3-coder-30b")
    monkeypatch.setenv("LAB_LLM_TEMP", "0.8")
    with pytest.raises(SystemExit) as error:
        rx.main(["--protocol", str(PROTOCOL), "--out", str(tmp_path / "new"), "--plan"] + extra)
    assert error.value.code == 2
    assert not (tmp_path / "new").exists()


def test_wrong_model_does_not_start_a_protocol_run(tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_LLM_MODEL", "other")
    with pytest.raises(SystemExit) as error:
        rx.main(["--protocol", str(PROTOCOL), "--out", str(tmp_path / "new"), "--plan"])
    assert error.value.code == 2


@pytest.mark.parametrize("extra", [["--repeat", "1"], ["--condition", "baseline"]])
def test_summary_refuses_conflicting_protocol_override(tmp_path, extra):
    with pytest.raises(SystemExit) as error:
        summary.main([str(tmp_path), "--protocol", str(PROTOCOL)] + extra)
    assert error.value.code == 2


def test_v3_cannot_reuse_v2_output_directory(tmp_path, monkeypatch):
    config = json.loads((ROOT / "results-qwen3-v2/.experiment.json").read_text())
    (tmp_path / ".experiment.json").write_text(json.dumps(config))
    monkeypatch.setenv("LAB_LLM_MODEL", "qwen3-coder-30b")
    with pytest.raises(SystemExit) as error:
        rx.main(["--protocol", str(PROTOCOL), "--out", str(tmp_path), "--plan"])
    assert error.value.code == 2
    assert json.loads((tmp_path / ".experiment.json").read_text()) == config


def test_protocol_rejects_duplicate_bugs_and_budget_changes_have_distinct_hashes(tmp_path):
    data, first = load_protocol(str(PROTOCOL))
    path = tmp_path / "protocol.json"
    data["repeat"] = 4
    path.write_text(json.dumps(data))
    assert load_protocol(str(path))[1] != first
    data["bugs"].append(data["bugs"][0])
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="duplicate bugs"):
        load_protocol(str(path))


def test_protocol_hash_is_portable_across_json_whitespace_and_newlines(tmp_path):
    data, expected = load_protocol(str(PROTOCOL))
    path = tmp_path / "protocol.json"
    path.write_bytes(json.dumps(data, sort_keys=True, indent=4).replace("\n", "\r\n").encode())
    assert load_protocol(str(path))[1] == expected


@pytest.mark.parametrize("missing", [False, True])
def test_final_protocol_analysis_requires_every_one_of_288_cells(monkeypatch, capsys, missing):
    protocol, digest = load_protocol(str(PROTOCOL))
    config = {key: protocol[key] for key in
              ("model", "harness_version", "max_iterations", "oracle_budget",
               "oracle_strictness", "temperature", "max_completion_tokens")}
    config.update(protocol_id=protocol["id"], protocol_sha256=digest)
    rows = [{"bug_id": bug, "condition": condition, "trial": trial,
             "max_iterations": 4, "iterations": [{}] * 4,
             "totals": {"fixed": False}, "notes": {"experiment_config": config}}
            for bug in protocol["bugs"] for condition in protocol["conditions"]
            for trial in range(1, 4)]
    if missing:
        rows.pop()
    monkeypatch.setattr(analysis, "load_experiment_runs", lambda *args: rows)
    argv = ["unused", "--protocol", str(PROTOCOL), "--final", "--json"]
    if missing:
        with pytest.raises(SystemExit) as error:
            analysis.main(argv)
        assert error.value.code == 2
        assert "missing matched bugs" in capsys.readouterr().err
    else:
        assert analysis.main(argv) == 0
        result = json.loads(capsys.readouterr().out)
        assert len(result["coverage"]["complete_bugs"]) == 16
        assert len(result["primary"]) == 4
        assert all(row["n_bugs"] == 16 for row in result["primary"])
