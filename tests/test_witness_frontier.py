import json

from ce.alive import AliveRun, FunctionResult
from ce.oracle import Violation
from ce.reduction import make_reduction
from scripts import run_witness_frontier as frontier


SOURCE = "define i32 @f(i32 %x) {\n  %v = add i32 %x, 1\n  ret i32 %v\n}\n"
TARGET = "define i32 @f(i32 %x) {\n  ret i32 %x\n}\n"


def test_frontier_runs_independent_capped_searches_and_rechecks_each_output(monkeypatch):
    settings = json.loads(frontier.PROTOCOL.read_bytes())["comparison"]
    original = FunctionResult(name="@f", src_ir=SOURCE, tgt_ir=TARGET,
                              verified=False, error_class="Value mismatch")
    calls = []

    def verify(src, tgt, extra_args, **kwargs):
        assert extra_args == ("-src-unroll=4",)
        calls.append((src, tgt))
        return AliveRun(functions=[original], num_incorrect=1, num_failed_to_prove=int(src == tgt))

    def paired(src, tgt, oracle, result, *, use_trace_guidance, allow_promotion):
        assert not use_trace_guidance and allow_promotion
        assert oracle.calls == 0
        oracle.record_external(calls=oracle.max_calls)
        return make_reduction("iraware", src, tgt, tgt, tgt)

    def focused(src, tgt, oracle, *, llvm_reduce, delta_passes):
        assert oracle.calls == 0
        assert delta_passes == frontier.guidance.IR_PASSES
        oracle.record_external(calls=oracle.max_calls - 1)
        return make_reduction("llvmreduce", src, tgt, src, tgt)

    monkeypatch.setattr(frontier, "run_alive_tv", verify)
    monkeypatch.setattr(frontier, "reduce_iraware", paired)
    monkeypatch.setattr(frontier, "reduce_llvmreduce", focused)

    witness = frontier.replay.Witness("1", SOURCE, TARGET, ("-src-unroll=4",), "f")
    case = frontier.run_case(witness, Violation.from_result(original), settings, "alive-tv", "llvm-reduce")
    assert calls == [(SOURCE, TARGET)] + [(TARGET, TARGET), (SOURCE, TARGET)] * len(settings["budgets"])
    for budget in settings["budgets"]:
        paired_row, focused_row = case["budgets"][str(budget)].values()
        assert (paired_row["reducer_calls"], focused_row["reducer_calls"]) == (budget, budget - 1)
        assert paired_row["postcheck_calls"] == focused_row["postcheck_calls"] == 1
        assert not paired_row["verified"] and paired_row["removed_fraction"] == 0
        assert focused_row["verified"]
    analysis = frontier.analyze([case], settings)
    assert analysis["8"]["p_two_sided"] == 1.0 and not analysis["8"]["improvement"]
    assert "p_two_sided" not in analysis["32"]
