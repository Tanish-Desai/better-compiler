from ce.alive import AliveRun, FunctionResult
from ce.oracle import Violation
from ce.reduction import make_reduction
from scripts import replay_reducer_guidance as guidance


ORIGINAL = "define i32 @f(i32 %x) {\n  %v = add i32 %x, 1\n  ret i32 %v\n}\n"
SMALL = "define i32 @f(i32 %x) {\n  ret i32 %x\n}\n"


def test_stalled_guided_prefix_passes_only_remaining_calls_to_retry(monkeypatch):
    violation = FunctionResult(name="@f", src_ir=ORIGINAL, tgt_ir=SMALL, verified=False,
                               error_class="Value mismatch")

    def verify(src, tgt, extra_args, **kwargs):
        assert extra_args == ("-src-unroll=4",)
        return AliveRun(functions=[violation], num_incorrect=1)

    def paired(src, tgt, oracle, result, *, use_trace_guidance):
        if use_trace_guidance:
            oracle.record_external(calls=8)
            return make_reduction("iraware", src, tgt, src, tgt)
        assert (oracle.calls, oracle.max_calls) in ((0, 32), (8, 32))
        oracle.record_external(calls=oracle.max_calls - oracle.calls)
        return make_reduction("iraware", src, tgt, SMALL, tgt)

    monkeypatch.setattr(guidance, "run_alive_tv", verify)
    monkeypatch.setattr(guidance, "reduce_iraware", paired)
    monkeypatch.setattr(guidance, "reduce_llvmreduce", lambda src, tgt, oracle, **kwargs:
                        make_reduction("llvmreduce", src, tgt, src, tgt))

    witness = guidance.replay.Witness("1", ORIGINAL, SMALL, ("-src-unroll=4",), "f")
    rows = guidance.replay_case(witness, Violation.from_result(violation), "alive-tv", "llvm-reduce")["methods"]
    assert rows["relax-if-stalled"]["prefix_calls"] == 8
    assert rows["relax-if-stalled"]["search_calls"] == 32
    assert rows["relax-if-stalled"]["retried_without_guidance"]
    assert rows["relax-if-stalled"]["verified"]
    assert rows["relax-if-stalled"]["removed_fraction"] > rows["trace-guided"]["removed_fraction"]


def test_postcheck_rejects_unresolved_verification_and_a_changed_error_class():
    violation = FunctionResult(name="@f", src_ir=ORIGINAL, tgt_ir=SMALL, verified=False,
                               error_class="Value mismatch")
    target = Violation("@f", "Target is more poisonous than source")
    assert guidance.clean_violation(AliveRun(functions=[violation], num_incorrect=1), target) is None
    unresolved = AliveRun(functions=[violation], num_incorrect=1, num_failed_to_prove=1)
    assert guidance.clean_violation(unresolved) is None
