"""Compare the proposed reducer against both strong matched-renderer controls."""
import json
from pathlib import Path

import pytest

from ce.protocol import load_protocol
from examples import analyze_significance as analysis
from examples import repair_experiment as runner

ROOT = Path(__file__).resolve().parent.parent
PATH = ROOT / "data/repair_v3_shared_h100_protocol.json"


def test_schedule_preserves_original_remaining_tiers_and_strong_controls():
    protocol, _ = load_protocol(str(PATH))
    old, _ = load_protocol(str(ROOT / "data/repair_v3_protocol.json"))
    assert protocol["bugs"] == old["bugs"]
    assert protocol["conditions"] == ["raw-plain", "raw-structured", "llvmreduce-structured", "iraware-structured"]
    assert len(protocol["bugs"]) * len(protocol["conditions"]) * protocol["repeat"] == 192
    assert len(protocol["primary_comparisons"]) == 3


def test_condition_positions_balance_across_bugs_without_changing_cells():
    protocol, _ = load_protocol(str(PATH))
    conditions = protocol["conditions"]
    from collections import Counter
    positions = Counter()
    for bug_index in range(16):
        for trial in range(1, 4):
            order = runner.scheduled_conditions(conditions, bug_index, trial, protocol["condition_order"])
            assert sorted(order) == sorted(conditions)
            positions.update((condition, position) for position, condition in enumerate(order))
    assert len(positions) == 16
    assert set(positions.values()) == {12}
    assert runner.scheduled_conditions(conditions, 1, 3) == conditions


def environment(monkeypatch, protocol):
    for variable, field in (("LAB_LLM_MODEL", "model"), ("LAB_LLM_TEMP", "temperature"),
                            ("LAB_LLM_MAX_TOKENS", "max_completion_tokens"),
                            ("LAB_LLM_MODEL_REPOSITORY", "model_repository"),
                            ("LAB_LLM_MODEL_REVISION", "model_revision")):
        monkeypatch.setenv(variable, str(protocol[field]))


def test_shared_plan_uses_pinned_configuration_without_model_or_build(tmp_path, monkeypatch, capsys):
    protocol, _ = load_protocol(str(PATH))
    environment(monkeypatch, protocol)
    monkeypatch.delenv("LAB_DATASET_DIR", raising=False)
    monkeypatch.setattr(runner, "_import_benchmark", lambda: pytest.fail("LLVM import"))
    assert runner.main(["--protocol", str(PATH), "--out", str(tmp_path / "new"), "--plan"]) == 0
    assert "192 cells" in capsys.readouterr().out
    assert not (tmp_path / "new").exists()
    monkeypatch.setenv("LAB_LLM_MODEL_REVISION", "wrong-revision")
    with pytest.raises(SystemExit) as error:
        runner.main(["--protocol", str(PATH), "--out", str(tmp_path / "new"), "--plan"])
    assert error.value.code == 2


@pytest.mark.parametrize("missing", [False, True])
def test_final_192_cell_panel_uses_only_its_three_frozen_comparisons(monkeypatch, capsys, missing):
    protocol, digest = load_protocol(str(PATH))
    config = {key: protocol[key] for key in
              ("model", "harness_version", "max_iterations", "oracle_budget",
               "oracle_strictness", "temperature", "max_completion_tokens", "condition_order",
               "require_semantic_baseline", "model_repository", "model_revision")}
    config.update(protocol_id=protocol["id"], protocol_sha256=digest)
    rows = [{"bug_id": bug, "condition": condition, "trial": trial,
             "max_iterations": 4, "iterations": [{}] * 4, "totals": {"fixed": False},
             "notes": {"experiment_config": config}}
            for bug in protocol["bugs"] for condition in protocol["conditions"] for trial in range(1, 4)]
    if missing:
        rows.pop()
    monkeypatch.setattr(analysis, "load_experiment_runs", lambda *args: rows)
    args = ["unused", "--protocol", str(PATH), "--final", "--json"]
    if missing:
        with pytest.raises(SystemExit) as error:
            analysis.main(args)
        assert error.value.code == 2
    else:
        assert analysis.main(args) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["registered_family_size"] == len(result["primary"]) == 3
        assert all(row["n_bugs"] == 16 and row["p_bh"] == 1.0 for row in result["primary"])
        assert {row["question"] for row in result["primary"]} == {
            pair["question"] for pair in protocol["primary_comparisons"]}


@pytest.mark.parametrize("update", [
    {"primary_comparisons": [{"better": "iraware-plain", "worse": "raw-structured", "question": "bad"}]},
    {"primary_comparisons": []}, {"model_revision": "main"},
])
def test_invalid_primary_family_or_unpinned_revision_is_rejected(tmp_path, update):
    protocol, _ = load_protocol(str(PATH))
    protocol.update(update)
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(protocol))
    with pytest.raises(ValueError):
        load_protocol(str(path))
