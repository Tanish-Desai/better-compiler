"""Tests for ce/irmodel.py -- reading and editing LLVM IR.

The most important test in this file is the *round-trip* test: parse some IR,
print it back out, and check you got exactly what you started with.  That is
the safety property the whole shrinker rests on -- we only understand a
fraction of LLVM IR, so everything we do not understand has to survive
untouched.

The last test in the file runs that check against **all 1462 real reproducers**
in the benchmark dataset, not just the hand-written samples above it.  It is
skipped unless ``$LAB_DATASET_DIR`` is set.  It has already caught three real
bugs (dropped blank lines, dropped comments, and re-indented instructions that
started at column 0).

The rest check the things the shrinker relies on: that we can tell a branch
target apart from a value, that following dependencies pulls in the branch
condition too, and that deleting a block cleans up any phi node pointing at it.
"""

from __future__ import annotations

import json
import os

import pytest

from ce import irmodel
from ce.irmodel import (
    backward_slice,
    dead_names,
    drop_params,
    measure,
    parse_module,
    reachable_blocks,
    remove_blocks,
    remove_instructions,
    substitute,
)

BRANCHY = """\
target datalayout = "e-p:64:64"

define i8 @f(i8 %x, i8 %y, i8 %z) {
entry:
  %a = add nsw i8 %x, 1
  %b = mul i8 %y, 3
  %c = sub i8 %a, %b
  %cmp = icmp slt i8 %x, 0
  br i1 %cmp, label %t, label %f
t:
  %r1 = shl i8 %c, 1
  br label %join
f:
  %r2 = ashr i8 %c, 1
  br label %join
join:
  %p = phi i8 [ %r1, %t ], [ %r2, %f ]
  %out = add nuw i8 %p, %a
  ret i8 %out
}
"""


@pytest.fixture
def fn():
    return parse_module(BRANCHY).functions[0]


def test_round_trip_is_byte_exact(fn):
    # Anything the model does not understand must survive verbatim, or the
    # reducer would silently corrupt IR it merely meant to shrink.
    assert parse_module(BRANCHY).text() == BRANCHY


def test_structure_is_recovered(fn):
    assert fn.name == "@f"
    assert fn.param_names() == ["%x", "%y", "%z"]
    assert [b.ref for b in fn.blocks] == ["%entry", "%t", "%f", "%join"]
    assert fn.entry_ref() == "%entry"


def test_labels_are_not_mistaken_for_value_operands(fn):
    br = fn.blocks[0].terminator()
    assert br.opcode == "br"
    assert br.operands == ["%cmp"]
    assert br.labels == ["%t", "%f"]

    # A phi's incoming blocks have no 'label' keyword; they must still be
    # classified as control flow, not as values.
    phi = fn.def_map()["%p"]
    assert phi.is_phi
    assert phi.operands == ["%r1", "%r2"]
    assert phi.labels == ["%t", "%f"]


def test_flags_are_detected_and_removable(fn):
    add = fn.def_map()["%a"]
    assert add.present_flags() == ["nsw"]
    assert add.without_flags(["nsw"]).raw == "%a = add i8 %x, 1"
    # Removing a flag that is not there is a no-op, not a corruption.
    assert add.without_flags(["nuw"]).raw == add.raw


def test_backward_slice_follows_control_dependence(fn):
    # %out needs %p, which needs both incoming values, which need %c -- and
    # reaching %join at all depends on %cmp, so the branch condition and its
    # operands must be pulled in too.
    s = backward_slice(fn, ["%out"])
    assert {"%out", "%p", "%r1", "%r2", "%c", "%a", "%b", "%x", "%y"} <= s
    assert "%cmp" in s, "control dependence must be part of the slice"


def test_backward_slice_excludes_unrelated_values():
    src = """\
define i8 @f(i8 %x, i8 %y) {
entry:
  %a = add i8 %x, 1
  %junk = mul i8 %y, 7
  ret i8 %a
}
"""
    fn = parse_module(src).functions[0]
    assert "%junk" not in backward_slice(fn, ["%a"])


def test_dead_names_finds_only_unused_results():
    src = """\
define i8 @f(i8 %x, i8 %y) {
entry:
  %a = add i8 %x, 1
  %junk = mul i8 %y, 7
  ret i8 %a
}
"""
    fn = parse_module(src).functions[0]
    assert dead_names(fn) == ["%junk"]
    assert dead_names(remove_instructions(fn, {"%junk"})) == []


def test_substitute_rewrites_uses_but_not_definitions(fn):
    out = substitute(fn, {"%a": "%NEW"})
    assert out.def_map()["%c"].operands == ["%NEW", "%b"]
    assert "%a" in out.def_map(), "the definition itself must keep its name"


def test_remove_blocks_prunes_dangling_phi_incomings(fn):
    pruned = remove_blocks(fn, {"%t"})
    assert [b.ref for b in pruned.blocks] == ["%entry", "%f", "%join"]
    phi = pruned.def_map()["%p"]
    assert "%r1" not in phi.raw and "%r2" in phi.raw


def test_entry_block_is_never_removed(fn):
    assert remove_blocks(fn, {"%entry"}).entry_ref() == "%entry"


def test_reachable_blocks_ignores_orphans():
    src = """\
define void @f(i1 %c) {
entry:
  ret void
orphan:
  br label %entry
}
"""
    fn = parse_module(src).functions[0]
    assert reachable_blocks(fn) == {"%entry"}


def test_drop_params_rewrites_the_signature(fn):
    out = drop_params(fn, {"%y", "%z"})
    assert out.param_names() == ["%x"]
    assert out.signature.startswith("define i8 @f(i8 %x)")


def test_switch_spanning_multiple_lines_stays_one_instruction():
    src = """\
define void @f(i32 %x) {
entry:
  switch i32 %x, label %d [
    i32 0, label %a
    i32 1, label %b
  ]
d:
  ret void
a:
  ret void
b:
  ret void
}
"""
    fn = parse_module(src).functions[0]
    entry = fn.blocks[0]
    assert len(entry.instructions) == 1
    assert entry.terminator().opcode == "switch"
    assert set(entry.successors()) == {"%d", "%a", "%b"}
    assert parse_module(src).text() == src


def test_measure_counts_structure_not_just_text():
    m = measure(BRANCHY)
    assert m.blocks == 4
    assert m.instructions == 12
    assert m.values == 11  # 8 results + 3 parameters


def test_unparseable_text_measures_without_raising():
    m = measure("this is not LLVM IR at all")
    assert m.instructions == 0 and m.chars > 0


@pytest.mark.skipif(
    not os.environ.get("LAB_DATASET_DIR"),
    reason="LAB_DATASET_DIR is not set",
)
def test_every_benchmark_reproducer_round_trips():
    """The model must not corrupt IR it does not understand.

    Hand-written samples cannot cover what real LLVM reproducers contain
    (vectors, metadata, attribute groups, byval/initializes, column-0
    instructions), so this runs the whole dataset through parse-then-print.
    """
    dataset = os.environ["LAB_DATASET_DIR"]
    checked, failures = 0, []
    for name in sorted(os.listdir(dataset)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(dataset, name), encoding="utf-8") as f:
            issue = json.load(f)
        for group in issue.get("tests", []):
            for test in group.get("tests", []):
                body = test.get("test_body")
                if not body:
                    continue
                checked += 1
                if parse_module(body).text().rstrip("\n") != body.rstrip("\n"):
                    failures.append(f"{name}::{test.get('test_name')}")

    assert checked > 100, "expected a substantial corpus"
    assert not failures, f"{len(failures)} reproducers failed to round-trip: {failures[:5]}"
