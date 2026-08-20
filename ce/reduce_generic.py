"""STEP 5a -- Shrinker #1: the dumb one (our control group).

WHAT THIS FILE IS FOR
---------------------
This shrinker treats the two ``.ll`` files as **plain text**.  It makes a list
of every line in both files and runs ddmin over that list: delete some lines,
ask the oracle whether the bug is still there, repeat.

It knows nothing about LLVM.  Not what an instruction is, not that ``%a`` on
one line is used on another, not that the two files are two versions of *the
same function*.

WHY WOULD WE DELIBERATELY BUILD SOMETHING BAD?
---------------------------------------------
Because it is the **control group**, and without it the experiment proves
nothing.

Here is the trap.  Suppose our smart shrinker makes the AI fix more bugs.  A
sceptical reviewer immediately asks:

    "Are you sure that is because your shrinking is *semantically clever*?
     Maybe the AI just does better with shorter prompts, and *any* shrinking
     would have worked."

That is a completely fair objection, and prior work (ReduceFix) already showed
that plain generic shrinking helps AI repair.  So we need a version that
shrinks *without* any LLVM knowledge, to separate the two explanations:

  * if generic shrinking helps just as much  -> the benefit is short prompts
  * if IR-aware shrinking helps much more    -> the benefit is understanding

For that comparison to be honest, this baseline must not be crippled: it gets
**the same oracle and the same budget of Alive2 calls** as the smart one.  Its
only handicap is ignorance, which is exactly the variable under test.

WHAT ACTUALLY HAPPENS WHEN YOU RUN IT
-------------------------------------
Badly, and instructively.  Deleting a random line from LLVM IR nearly always
produces code that does not parse -- you delete the line defining ``%a`` while
five other lines still use it.  In our sample run, 176 of its 183 attempts
produced invalid IR and were rejected.  It ends up deleting almost nothing.

That is not a bug in this file.  That is the finding.
"""

from __future__ import annotations

from typing import List, Tuple

from .oracle import Oracle
from .reduction import Reduction, ddmin, make_reduction, _Timer

#: A removable unit: (which file, original line index, text).
Unit = Tuple[int, int, str]

SRC, TGT = 0, 1


def _units(src: str, tgt: str) -> List[Unit]:
    out: List[Unit] = []
    for side, text in ((SRC, src), (TGT, tgt)):
        for i, line in enumerate(text.splitlines()):
            out.append((side, i, line))
    return out


def _render(units: List[Unit]) -> Tuple[str, str]:
    src = [u[2] for u in units if u[0] == SRC]
    tgt = [u[2] for u in units if u[0] == TGT]
    return "\n".join(src) + "\n", "\n".join(tgt) + "\n"


def reduce_generic(src: str, tgt: str, oracle: Oracle) -> Reduction:
    """Line-level ddmin over the pair, with no LLVM knowledge whatsoever."""
    with _Timer() as timer:
        units = _units(src, tgt)

        def test(subset: List[Unit]) -> bool:
            # ddmin can hand back a subset with lines from only one side; that
            # is not special-cased, because noticing it would require knowing
            # the files are related -- which is precisely the knowledge this
            # baseline is defined not to have.  Such candidates simply fail.
            cand_src, cand_tgt = _render(subset)
            return bool(oracle.check(cand_src, cand_tgt))

        kept = ddmin(units, test, should_stop=lambda: oracle.exhausted)
        new_src, new_tgt = _render(kept)

        # ddmin guarantees the last *accepted* subset reproduces, but if the
        # budget ran out mid-pass the final `kept` may never have been tested.
        if not oracle.check(new_src, new_tgt):
            new_src, new_tgt = src, tgt

    return make_reduction(
        "generic",
        src,
        tgt,
        new_src,
        new_tgt,
        passes_applied=["ddmin-lines"] if (new_src, new_tgt) != (src, tgt) else [],
        oracle_stats=oracle.stats(),
        seconds=timer.elapsed,
    )
