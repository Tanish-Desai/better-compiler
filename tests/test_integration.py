"""End-to-end tests that really do run Alive2.

These are the tests that prove the shrinkers work, rather than merely that
their parts are wired together.  The important ones:

  * shrinking preserves the bug -- confirmed by re-running Alive2 on the
    result, independently of the shrinker's own bookkeeping
  * the smart shrinker isolates the offending ``nsw`` flag
  * the smart shrinker beats the dumb one **on the same budget** (this is the
    core research comparison, in miniature)
  * every one of the six conditions produces a readable message
  * the structured output only mentions values that exist in the code it is
    showing -- easy to get wrong, since shrinking renames and deletes things

**These are skipped automatically** if ``$LAB_LLVM_ALIVE_TV`` does not point at
a working ``alive-tv``, so the rest of the suite still runs on a laptop without
the container.  If you see them skipped, that is why.

The tests at the bottom of the file cover the benchmark adapter and need no
Alive2 at all.
"""

from __future__ import annotations

import json
import os

import pytest

from ce.alive import run_alive_tv
from ce.benchmark import RunLog, Iteration, normalize_feedback, run_record_path, summarize
from ce.feedback import MATRIX_LETTERS, build_feedback, resolve_condition
from ce.oracle import establish
from ce.reduce_generic import reduce_generic
from ce.reduce_iraware import reduce_iraware
from ce.reduce_llvmreduce import default_llvm_reduce, reduce_llvmreduce

ALIVE_TV = os.environ.get("LAB_LLVM_ALIVE_TV")
requires_alive = pytest.mark.skipif(
    not (ALIVE_TV and os.access(ALIVE_TV, os.X_OK)),
    reason="LAB_LLVM_ALIVE_TV is not set to an executable alive-tv",
)

LLVM_REDUCE = default_llvm_reduce()
requires_llvm_reduce = pytest.mark.skipif(
    not (LLVM_REDUCE and os.access(LLVM_REDUCE, os.X_OK)),
    reason="llvm-reduce not found under $LAB_LLVM_BUILD_DIR/bin (or $LAB_LLVM_LLVM_REDUCE)",
)

SAMPLES = os.path.join(os.path.dirname(__file__), os.pardir, "data", "samples")


def sample(name: str) -> str:
    with open(os.path.join(SAMPLES, name), "r", encoding="utf-8") as f:
        return f.read()


@pytest.fixture
def pair():
    return sample("poison.src.ll"), sample("poison.tgt.ll")


@requires_alive
def test_alive_tv_round_trip(pair):
    src, tgt = pair
    assert not run_alive_tv(src, tgt).verified
    assert run_alive_tv(src, src).verified, "a function must refine itself"


@requires_alive
def test_iraware_reduction_preserves_the_violation_and_shrinks(pair):
    src, tgt = pair
    _, violation, oracle = establish(src, tgt)
    assert oracle is not None

    result = reduce_iraware(src, tgt, oracle, violation)
    assert result.error is None
    assert result.size_after["instructions"] < result.size_before["instructions"]

    # The whole point: the output must still be a counterexample for the same
    # reason, verified independently of the reducer's own bookkeeping.
    rerun = run_alive_tv(result.src, result.tgt)
    again = rerun.first_violation()
    assert again is not None
    assert again.error_class == violation.error_class


@requires_alive
def test_iraware_isolates_the_offending_flag(pair):
    """The reduced pair should differ only by the flag that causes the bug."""
    src, tgt = pair
    _, violation, oracle = establish(src, tgt)
    result = reduce_iraware(src, tgt, oracle, violation)

    from ce.irmodel import parse_module
    from ce.structured import diff_functions

    diff = diff_functions(
        parse_module(result.src).function("@f"),
        parse_module(result.tgt).function("@f"),
    )
    assert diff.added_flags, "the surviving difference should be a flag"
    assert "nsw" in sum(diff.added_flags.values(), [])


@requires_alive
def test_iraware_beats_generic_on_the_same_budget(pair):
    """RQ3, in miniature: IR-awareness is not just compression.

    Both reducers get the same oracle strictness and the same call budget.
    A line-level reducer cannot express "these two files are one function", so
    almost every candidate it proposes is invalid IR.
    """
    src, tgt = pair
    _, violation, o1 = establish(src, tgt, max_calls=400)
    ir_aware = reduce_iraware(src, tgt, o1, violation)

    _, _, o2 = establish(src, tgt, max_calls=400)
    generic = reduce_generic(src, tgt, o2)

    assert ir_aware.size_after["instructions"] < generic.size_after["instructions"]
    assert ir_aware.oracle_stats["oracle_calls"] < generic.oracle_stats["oracle_calls"]


@requires_alive
@requires_llvm_reduce
def test_llvmreduce_reduction_preserves_the_violation_and_shrinks(pair):
    src, tgt = pair
    _, violation, oracle = establish(src, tgt)
    assert oracle is not None

    result = reduce_llvmreduce(src, tgt, oracle)
    assert result.error is None
    assert result.size_after["instructions"] < result.size_before["instructions"]

    rerun = run_alive_tv(result.src, result.tgt)
    again = rerun.first_violation()
    assert again is not None
    assert again.error_class == violation.error_class


@requires_alive
@requires_llvm_reduce
def test_llvmreduce_has_far_higher_oracle_acceptance_than_generic(pair):
    """Blocker 5's whole point: llvm-reduce is IR-aware, so almost every
    candidate it proposes is valid IR -- unlike generic's line-level ddmin,
    where 176 of 183 attempts were invalid in the README's worked example."""
    src, tgt = pair
    _, _, o1 = establish(src, tgt, max_calls=400)
    llvmreduce = reduce_llvmreduce(src, tgt, o1)

    _, _, o2 = establish(src, tgt, max_calls=400)
    generic = reduce_generic(src, tgt, o2)

    def acceptance_rate(stats):
        calls = stats["oracle_calls"]
        return stats["oracle_accepted"] / calls if calls else 0.0

    assert acceptance_rate(llvmreduce.oracle_stats) > acceptance_rate(generic.oracle_stats)
    assert llvmreduce.size_after["instructions"] < generic.size_after["instructions"]


@requires_alive
@requires_llvm_reduce
def test_iraware_still_beats_llvmreduce_on_the_same_budget(pair):
    """The comparison Blocker 5 exists to enable: llvm-reduce closes most of
    the gap to generic (it IS IR-aware), but counterexample-awareness still
    adds real value beyond that -- iraware reaches a smaller (or equal) result
    using far fewer oracle calls."""
    src, tgt = pair
    _, violation, o1 = establish(src, tgt, max_calls=400)
    ir_aware = reduce_iraware(src, tgt, o1, violation)

    _, _, o2 = establish(src, tgt, max_calls=400)
    llvmreduce = reduce_llvmreduce(src, tgt, o2)

    assert ir_aware.size_after["instructions"] <= llvmreduce.size_after["instructions"]
    assert ir_aware.oracle_stats["oracle_calls"] < llvmreduce.oracle_stats["oracle_calls"]


@requires_alive
@requires_llvm_reduce
@pytest.mark.parametrize("condition", ["llvmreduce-plain", "llvmreduce-structured"])
def test_llvmreduce_conditions_render(pair, condition):
    src, tgt = pair
    fb = build_feedback(src, tgt, condition, oracle_budget=400)
    assert fb.error is None
    assert fb.text.strip()
    assert fb.condition == condition


@requires_alive
def test_reduction_without_promotion_is_more_conservative(pair):
    src, tgt = pair
    _, violation, o1 = establish(src, tgt)
    with_promo = reduce_iraware(src, tgt, o1, violation, allow_promotion=True)
    _, _, o2 = establish(src, tgt)
    without = reduce_iraware(src, tgt, o2, violation, allow_promotion=False)

    assert with_promo.size_after["instructions"] <= without.size_after["instructions"]
    assert "promote-operands" not in without.passes_applied


@requires_alive
@pytest.mark.parametrize("letter", sorted(MATRIX_LETTERS))
def test_every_matrix_condition_renders(pair, letter):
    src, tgt = pair
    fb = build_feedback(src, tgt, letter, oracle_budget=300)
    assert fb.error is None
    assert fb.text.strip()
    assert fb.condition == MATRIX_LETTERS[letter]

    cond = resolve_condition(letter)
    if cond.structured:
        assert "VIOLATED PROPERTY:" in fb.text
    else:
        assert "VIOLATED PROPERTY:" not in fb.text


@requires_alive
def test_reduced_conditions_are_smaller_than_raw(pair):
    src, tgt = pair
    raw = build_feedback(src, tgt, "raw-plain")
    reduced = build_feedback(src, tgt, "iraware-plain", oracle_budget=300)
    assert reduced.metrics["prompt_tokens_est"] < raw.metrics["prompt_tokens_est"]


@requires_alive
def test_structured_feedback_describes_the_ir_it_shows(pair):
    """Structured output must cite values that exist in the IR it displays.

    Reduction renames and deletes values, so the rendering has to be driven by
    a re-verification of the *reduced* pair, not the original counterexample.
    """
    src, tgt = pair
    fb = build_feedback(src, tgt, "iraware-structured", oracle_budget=300)
    shown_ir = fb.text.split("WHAT THE TRANSFORMATION CHANGED:")[0]

    critical = fb.text.split("CRITICAL VALUES:")[1].split("DEPENDENCY CHAIN")[0]
    for name in [w.strip() for w in critical.split() if w.strip().startswith("%")]:
        assert name in shown_ir, f"{name} is cited but not present in the shown IR"


# --------------------------------------------------------------------------
# Benchmark adapter (no alive-tv needed)
# --------------------------------------------------------------------------

def test_non_alive_feedback_passes_through_unchanged():
    log = [{"name": "t", "result": False, "log": "error: build failed\n"}]
    fb = normalize_feedback(log, "iraware-structured")
    assert fb.text == "error: build failed\n"
    assert fb.metrics["counterexample"] is False


def test_oversized_plain_feedback_is_truncated():
    log = [{"name": "t", "result": False, "log": "x" * 5000}]
    fb = normalize_feedback(log, "raw-plain", max_log_size=100)
    assert fb.text.endswith("<Truncated>...")
    assert len(fb.text) < 200


def test_run_log_totals_and_summary(tmp_path):
    run = RunLog(bug_id="121459", condition="iraware-structured", max_iterations=4)
    run.record(Iteration(0, "iraware-structured", fixed=False,
                         feedback={"prompt_tokens_est": 100, "oracle_calls": 12},
                         llm={"prompt_tokens": 900, "completion_tokens": 80}))
    run.record(Iteration(1, "iraware-structured", fixed=True,
                         feedback={"prompt_tokens_est": 60, "oracle_calls": 5},
                         llm={"prompt_tokens": 700, "completion_tokens": 40}))

    totals = run.totals()
    assert totals["iterations"] == 2 and totals["fixed"] is True
    assert totals["prompt_tokens_est"] == 160
    assert totals["oracle_calls"] == 17
    assert totals["llm_completion_tokens"] == 120

    path = run.write(str(tmp_path))
    assert path.endswith("121459.iraware-structured.json")

    table = summarize([run.as_dict()])
    assert table["iraware-structured"]["repair_rate"] == 1.0
    assert table["iraware-structured"]["mean_iterations"] == 2.0


def test_no_promotion_ablation_does_not_collide_with_the_default_run(tmp_path):
    """Blocker 7: running the --no-promotion ablation for a bug/condition
    already run must not silently overwrite (or get skipped in favor of) the
    paired result -- the two need distinct paths."""
    assert run_record_path(str(tmp_path), "121459", "iraware-structured") == \
        run_record_path(str(tmp_path), "121459", "iraware-structured", allow_promotion=True)
    assert run_record_path(str(tmp_path), "121459", "iraware-structured") != \
        run_record_path(str(tmp_path), "121459", "iraware-structured", allow_promotion=False)

    default_run = RunLog(bug_id="121459", condition="iraware-structured",
                         notes={"allow_promotion": True})
    default_run.record(Iteration(0, "iraware-structured", fixed=True))
    ablation_run = RunLog(bug_id="121459", condition="iraware-structured",
                          notes={"allow_promotion": False})
    ablation_run.record(Iteration(0, "iraware-structured", fixed=False))

    default_path = default_run.write(str(tmp_path))
    ablation_path = ablation_run.write(str(tmp_path))

    assert default_path != ablation_path
    assert default_path.endswith("121459.iraware-structured.json")
    assert ablation_path.endswith("121459.iraware-structured.no-promotion.json")

    # Both files actually exist and hold their own (different) content --
    # the old scheme would have had the second write clobber the first.
    with open(default_path, encoding="utf-8") as f:
        assert json.load(f)["totals"]["fixed"] is True
    with open(ablation_path, encoding="utf-8") as f:
        assert json.load(f)["totals"]["fixed"] is False


def test_summary_separates_conditions():
    a = RunLog(bug_id="1", condition="raw-plain")
    a.record(Iteration(0, "raw-plain", fixed=False))
    b = RunLog(bug_id="1", condition="iraware-structured")
    b.record(Iteration(0, "iraware-structured", fixed=True))

    table = summarize([a.as_dict(), b.as_dict()])
    assert table["raw-plain"]["repair_rate"] == 0.0
    assert table["iraware-structured"]["repair_rate"] == 1.0


def test_unknown_condition_is_rejected_loudly():
    with pytest.raises(KeyError):
        resolve_condition("iraware-fancy")


def test_legacy_letters_resolve_to_the_proposal_ordering():
    # context.md s15 and s16 disagree on lettering; both must be addressable.
    assert resolve_condition("A", legacy=True).name == "baseline"
    assert resolve_condition("A").name == "raw-plain"
    assert resolve_condition("F", legacy=True).name == "iraware-structured"
    assert resolve_condition("F").name == "iraware-structured"
