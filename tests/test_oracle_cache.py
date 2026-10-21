"""Bounded searches must preserve previously verified candidates without new calls."""

from pathlib import Path

from ce.alive import parse_alive_output
from ce.oracle import Oracle, Violation
from ce.reduce_generic import reduce_generic


def verifier(monkeypatch):
    parsed = parse_alive_output((Path(__file__).parent.parent / "data/samples/poison.txt").read_text())
    calls = []

    def check(*args, **kwargs):
        calls.append(args)
        return parsed

    monkeypatch.setattr("ce.oracle.run_alive_tv", check)
    return parsed, calls


def test_final_check_reuses_accepted_pair_at_exhausted_budget(monkeypatch):
    parsed, calls = verifier(monkeypatch)
    oracle = Oracle(Violation.from_result(parsed.first_violation()), max_calls=1)
    assert oracle.check("source", "target")
    assert oracle.exhausted
    assert oracle.check("source", "target")
    assert not oracle.check("different source", "target")
    assert len(calls) == oracle.calls == 1
    assert oracle.stats()["oracle_cache_hits"] == 1


def test_cache_is_scoped_to_verifier_arguments_and_matching_policy(monkeypatch):
    parsed, calls = verifier(monkeypatch)
    oracle = Oracle(Violation.from_result(parsed.first_violation()))
    assert oracle.check("source", "target")
    oracle.extra_args = ("-src-unroll=4",)
    assert oracle.check("source", "target")
    oracle.strictness = "error_class_and_kind"
    assert oracle.check("source", "target")
    assert len(calls) == 3


def test_rejected_or_failed_checks_are_not_cached(monkeypatch):
    parsed, calls = verifier(monkeypatch)
    oracle = Oracle(Violation("different", "different"))
    assert not oracle.check("source", "target")
    assert not oracle.check("source", "target")
    assert len(calls) == 2
    assert oracle.cache_hits == 0


def test_generic_keeps_last_accepted_reduction_on_budget_exhaustion(monkeypatch):
    parsed, calls = verifier(monkeypatch)
    oracle = Oracle(Violation.from_result(parsed.first_violation()), max_calls=1)
    result = reduce_generic("source line one\nsource line two\n", "target line one\ntarget line two\n", oracle)
    assert (result.src, result.tgt) != ("source line one\nsource line two\n", "target line one\ntarget line two\n")
    assert oracle.calls == len(calls) == 1
    assert oracle.cache_hits >= 1
