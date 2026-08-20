"""Tests for ce/alive.py -- reading Alive2's output.

These run against **real Alive2 output**, captured from the container and saved
in ``data/samples/``.  That matters: Alive2's output format is not documented
anywhere, so testing against output we invented ourselves would only prove we
are consistent with our own guesses.

The five fixtures cover the shapes we actually have to handle:

    poison.txt          the optimizer produced poison where the original
                        code had a real value
    value_mismatch.txt  the two versions simply return different numbers
    ub.txt              the optimized version has undefined behaviour;
                        also exercises the memory-state sections
    multi_function.txt  a file with two functions, one fine and one broken
    correct.txt         a transformation that is actually valid

No Alive2 installation is needed to run these -- they read the saved files.
"""

from __future__ import annotations

import os

import pytest

from ce.alive import parse_alive_output

SAMPLES = os.path.join(os.path.dirname(__file__), os.pardir, "data", "samples")


def sample(name: str) -> str:
    with open(os.path.join(SAMPLES, name), "r", encoding="utf-8") as f:
        return f.read()


def test_poison_violation_is_fully_parsed():
    run = parse_alive_output(sample("poison.txt"))

    assert not run.verified
    assert (run.num_correct, run.num_incorrect, run.num_errors) == (0, 1, 0)

    fn = run.first_violation()
    assert fn is not None
    assert fn.name == "@f"
    assert fn.error_class == "Target is more poisonous than source"
    assert fn.violated_property == "poison refinement"

    # The input assignment is the function's three arguments.
    assert [a.name for a in fn.example] == ["%x", "%y", "%z"]
    assert fn.example[0].value.startswith("#x7e")

    # The trace carries every intermediate value, and the target's %out is the
    # poison one -- that is the whole counterexample in one assertion.
    assert fn.trace_index(target=True)["%out"].is_poison
    assert not fn.trace_index()["%out"].is_poison
    assert fn.tgt_value == "poison"

    # "  >> Jump to %f" must attach to the value printed above it, not become
    # a value of its own.
    assert fn.executed_blocks() == ["%f", "%join"]
    assert all(not a.name.startswith(">>") for a in fn.src_trace)


def test_value_mismatch_has_no_memory_sections():
    fn = parse_alive_output(sample("value_mismatch.txt")).first_violation()
    assert fn is not None
    assert fn.error_class == "Value mismatch"
    assert fn.src_memory is None and fn.tgt_memory is None
    assert fn.src_value and fn.tgt_value and fn.src_value != fn.tgt_value


def test_ub_violation_captures_memory_state():
    fn = parse_alive_output(sample("ub.txt")).first_violation()
    assert fn is not None
    assert fn.error_class == "Source is more defined than target"
    assert fn.src_memory is not None
    assert "NON-LOCAL BLOCKS" in fn.src_memory
    assert any(a.is_ub for a in fn.tgt_trace)
    # A pointer's printed value must survive intact, slashes and all.
    ptr = next(a for a in fn.example if a.name == "%p")
    assert "pointer(non-local" in ptr.value and "Address=" in ptr.value


def test_multi_function_run_separates_the_verdicts():
    run = parse_alive_output(sample("multi_function.txt"))
    assert len(run.functions) == 2
    by_name = {f.name: f for f in run.functions}
    assert by_name["@g"].verified
    assert not by_name["@h"].verified
    assert run.first_violation().name == "@h"
    assert run.violation_for("@g") is None


def test_correct_transformation_reports_verified():
    run = parse_alive_output(sample("correct.txt"))
    assert run.verified
    assert run.first_violation() is None
    assert all(f.verified for f in run.functions)


def test_garbage_input_is_flagged_not_crashed():
    run = parse_alive_output("some unrelated text\n")
    assert run.tool_error is not None
    assert not run.verified
