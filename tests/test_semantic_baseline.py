"""Do not spend repairs or label tool failures as failed witness treatments."""
import pytest

from ce.alive import AliveRun, FunctionResult
from examples import repair_experiment as runner
from test_repair_pipeline import FakeEnvironment, args, model


@pytest.mark.parametrize("parsed", [AliveRun(tool_error="timeout"), AliveRun(num_errors=1),
                                     AliveRun(num_correct=1), AliveRun(num_incorrect=1)])
def test_unusable_alive_report_stops_before_llm_or_patch(monkeypatch, parsed):
    class BrokenWitness(FakeEnvironment):
        def check_fast(self):
            return False, {"src": "source", "tgt": "target", "log": "tool report"}

    monkeypatch.setattr(runner, "Env", BrokenWitness)
    monkeypatch.setattr(runner, "parse_alive_output", lambda text: parsed)
    monkeypatch.setattr(runner, "bug_hunk", lambda *values: pytest.fail("patch construction reached"))
    llm = model()
    llm.chat = lambda *values: pytest.fail("model called")
    config = args()
    config.require_semantic_baseline = True
    with pytest.raises(runner.BaselineBuildError, match="not a clean semantic violation"):
        runner.repair("1", "raw-structured", config, llm)


def test_valid_witness_is_confirmed_and_plain_tool_failure_is_not(monkeypatch):
    violation = FunctionResult(name="@f", src_ir="source", tgt_ir="target", verified=False,
                               error_class="Value mismatch")
    monkeypatch.setattr(runner, "parse_alive_output", lambda text: AliveRun(functions=[violation], num_incorrect=1))
    result = runner.semantic_baseline([{"result": False, "log": {"src": "s", "tgt": "t", "log": "report"}}])
    assert result == {"semantic_failure_confirmed": True, "function": "@f", "error_class": "Value mismatch"}
    with pytest.raises(runner.BaselineBuildError, match="not an Alive2 witness"):
        runner.semantic_baseline("opt crashed")


def test_mixed_incorrect_and_unproved_functions_are_not_a_clean_baseline(monkeypatch):
    violation = FunctionResult(name="@f", src_ir="source", tgt_ir="target", verified=False,
                               error_class="Value mismatch")
    parsed = AliveRun(functions=[violation], num_incorrect=1, num_failed_to_prove=1)
    monkeypatch.setattr(runner, "parse_alive_output", lambda text: parsed)
    with pytest.raises(runner.BaselineBuildError, match="not a clean semantic violation"):
        runner.semantic_baseline({"src": "s", "tgt": "t", "log": "mixed report"})
