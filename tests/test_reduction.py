"""Tests for the shrinking algorithm, the oracle's rules, and the formatter.

Covers three things:

  * **ddmin** -- does the shrinking algorithm actually find a minimal subset?
    Tested on plain lists of numbers, where the right answer is obvious.
  * **the oracle's judgement** -- given saved Alive2 output, does it correctly
    accept "same bug" and reject "different bug"?  This is the rule the whole
    project depends on, so it is tested directly rather than only in passing.
  * **the structured formatter** -- are all the sections present, and does the
    explanation correctly name the flag that caused the bug?

None of these run Alive2.  They work off the saved output in ``data/samples/``
and check the oracle's decision logic in isolation.  Tests that genuinely need
the real verifier live in ``test_integration.py``.
"""

from __future__ import annotations

import os

import pytest

from ce.alive import parse_alive_output
from ce.oracle import Oracle, Violation, outcome_kind
from ce.reduction import ddmin
from ce.structured import diff_functions, divergence, render_plain, render_structured
from ce.irmodel import parse_module
from ce.reduce_iraware import value_types, violation_seeds

SAMPLES = os.path.join(os.path.dirname(__file__), os.pardir, "data", "samples")


def sample(name: str) -> str:
    with open(os.path.join(SAMPLES, name), "r", encoding="utf-8") as f:
        return f.read()


@pytest.fixture
def poison():
    return parse_alive_output(sample("poison.txt")).first_violation()


# --------------------------------------------------------------------------
# ddmin
# --------------------------------------------------------------------------

def test_ddmin_isolates_the_single_required_element():
    calls = []

    def test(subset):
        calls.append(tuple(subset))
        return 7 in subset

    assert ddmin(list(range(16)), test) == [7]
    assert calls, "ddmin must actually probe"


def test_ddmin_finds_a_minimal_pair():
    result = ddmin(list(range(12)), lambda s: 3 in s and 9 in s)
    assert set(result) == {3, 9}


def test_ddmin_returns_input_when_everything_is_required():
    units = list(range(6))
    assert ddmin(units, lambda s: len(s) == len(units)) == units


def test_ddmin_stops_when_asked():
    stop = {"now": False}

    def test(subset):
        stop["now"] = True
        return False

    # A should_stop that trips on the first probe must not loop forever.
    assert ddmin(list(range(64)), test, should_stop=lambda: stop["now"]) is not None


# --------------------------------------------------------------------------
# Oracle
# --------------------------------------------------------------------------

def test_outcome_kind_distinguishes_failure_modes():
    assert outcome_kind(parse_alive_output(sample("poison.txt")).first_violation()) == "poison"
    assert outcome_kind(parse_alive_output(sample("ub.txt")).first_violation()) == "ub"
    assert outcome_kind(parse_alive_output(sample("value_mismatch.txt")).first_violation()) == "value"


def test_violation_records_what_must_be_preserved(poison):
    v = Violation.from_result(poison)
    assert v.function == "@f"
    assert v.error_class == "Target is more poisonous than source"
    assert v.kind == "poison"
    assert "@f" in v.describe()


def test_oracle_rejects_an_unknown_strictness(poison):
    with pytest.raises(ValueError):
        Oracle(target=Violation.from_result(poison), strictness="whatever")


def test_oracle_matching_respects_strictness(poison):
    target = Violation.from_result(poison)
    poison_run = parse_alive_output(sample("poison.txt"))
    other_run = parse_alive_output(sample("value_mismatch.txt"))

    strict = Oracle(target=target, strictness="error_class")
    assert strict._match(poison_run) is not None
    # A different error class is a different bug, and must not be accepted.
    assert strict._match(other_run) is None

    loose = Oracle(target=target, strictness="any_failure")
    assert loose._match(other_run) is not None, "any_failure accepts any failure"

    verified = parse_alive_output(sample("correct.txt"))
    assert loose._match(verified) is None, "a passing run is never a match"


def test_oracle_budget_is_enforced(poison):
    oracle = Oracle(target=Violation.from_result(poison), max_calls=0)
    assert oracle.exhausted
    result = oracle.check("define void @f() { ret void }", "define void @f() { ret void }")
    assert not result.ok and "budget" in result.reason
    assert oracle.calls == 0, "an exhausted oracle must not spend a call"


# --------------------------------------------------------------------------
# Seeding and structured rendering
# --------------------------------------------------------------------------

def test_seeds_pick_out_the_diverging_value(poison):
    seeds = violation_seeds(poison)
    assert "%out" in seeds, "the value that becomes poison must be a seed"
    # Values that agree between source and target are not part of the reason.
    assert "%b" not in seeds and "%d" not in seeds


def test_value_types_come_from_the_trace(poison):
    types = value_types(poison)
    assert types["%x"] == "i8"
    assert types["%cmp"] == "i1", "the trace is the type source, not the opcode"


def test_diff_matches_on_ssa_name_not_line_position():
    src = parse_module(sample("poison.src.ll")).function("@f")
    tgt = parse_module(sample("poison.tgt.ll")).function("@f")
    diff = diff_functions(src, tgt)

    assert not diff.added and not diff.removed
    assert [name for name, _, _ in diff.changed] == ["%out"]
    assert diff.added_flags == {"%out": ["nsw"]}


def test_structured_output_has_every_field(poison):
    text = render_structured(poison, sample("poison.src.ll"), sample("poison.tgt.ll"))
    for header in (
        "BUG TYPE:", "VERIFICATION RESULT:", "VIOLATED PROPERTY:",
        "WHAT THE TRANSFORMATION CHANGED:", "CRITICAL VALUES:",
        "DEPENDENCY CHAIN (source):", "CONTROL FLOW", "COUNTEREXAMPLE INPUT:",
        "DIVERGENCE", "INTERPRETATION:",
    ):
        assert header in text, f"missing section {header}"


def test_interpretation_names_the_offending_flag(poison):
    text = render_structured(poison, sample("poison.src.ll"), sample("poison.tgt.ll"))
    assert "'nsw'" in text
    assert "may only make a program *less* poisonous" in text


def test_divergence_reports_both_sides(poison):
    lines = divergence(poison)
    assert any(l.startswith("%out:") and "poison" in l for l in lines)


def test_plain_rendering_keeps_alive2s_own_words(poison):
    text = render_plain(poison, sample("poison.src.ll"), sample("poison.tgt.ll"))
    assert "ERROR: Target is more poisonous than source" in text
    assert "BUG TYPE:" not in text, "plain must not smuggle in structure"
