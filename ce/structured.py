"""STEP 6 -- Reorganise the counterexample into labelled sections.

WHAT THIS FILE IS FOR
---------------------
This is the **second, completely separate idea** in the research.

The shrinkers change *how much* information the AI gets.  This file changes
*how that information is laid out*.  Those are different things, and the whole
point of the experiment is to find out which one actually matters -- so they
are deliberately kept independent and can be switched on and off separately.

Instead of handing the model Alive2's wall of text, we hand it labelled
sections::

    BUG TYPE:                          miscompilation
    VERIFICATION RESULT:               Invalid refinement in @f -- ...
    VIOLATED PROPERTY:                 poison refinement
    SOURCE / TARGET:                   the two versions of the code
    WHAT THE TRANSFORMATION CHANGED:   a diff, matched by value name
    CRITICAL VALUES:                   which values the bug is about
    DEPENDENCY CHAIN:                  the instructions that feed them
    CONTROL FLOW:                      which blocks actually ran
    COUNTEREXAMPLE INPUT:              the input that triggers it
    DIVERGENCE:                        where src and tgt start disagreeing
    INTERPRETATION:                    which rule was broken, and why

ONE NICE TRICK IN HERE
----------------------
``WHAT THE TRANSFORMATION CHANGED`` compares the two versions **by value name,
not by line number**.  Optimizers love to reorder and renumber instructions,
which makes an ordinary line-by-line diff enormous and useless.  Matching
``%out`` in the source against ``%out`` in the target instead shows you the
one real change.

IMPORTANT: NOTHING HERE IS AI-GENERATED
---------------------------------------
It would be easy (and wrong) to have a model write the explanation, because
then we would be testing *that model* rather than our feedback format.

Everything here is produced mechanically from Alive2's output and the IR.  The
``INTERPRETATION`` section is a fixed template chosen by error class, filled in
with the actual values involved.  **It never states anything the verifier did
not.**
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .alive import Assignment, FunctionResult
from .irmodel import Function, Instruction, parse_module
from .reduce_iraware import violation_seeds

#: Obligation text per alive2 error class, phrased as the rule that was broken.
OBLIGATION = {
    "Target is more poisonous than source": (
        "A transformation may only make a program *less* poisonous. Wherever the "
        "source produces a well-defined value, the target must produce the same "
        "value -- it may not produce poison."
    ),
    "Target is more undefined than source": (
        "A transformation may not widen the set of values a program may return. "
        "Wherever the source is well-defined, the target must be too."
    ),
    "Source is more defined than target": (
        "A transformation may not introduce undefined behaviour. Any execution "
        "that is UB-free in the source must remain UB-free in the target."
    ),
    "Value mismatch": (
        "A transformation must preserve the returned value for every input on "
        "which the source is well-defined."
    ),
    "Mismatch in memory": (
        "A transformation must leave memory in a state the source could also "
        "have produced."
    ),
    "Mismatch in return memory": (
        "A transformation must leave the returned memory block in a state the "
        "source could also have produced."
    ),
}


def _fmt(items: Sequence[str], empty: str = "(none)") -> str:
    return "\n".join(f"    {i}" for i in items) if items else f"    {empty}"


def _instruction_index(fn: Optional[Function]) -> Dict[str, Instruction]:
    return fn.def_map() if fn else {}


@dataclass
class IRDiff:
    """A def-keyed comparison of the source and target functions."""

    added: List[str]
    removed: List[str]
    changed: List[Tuple[str, str, str]]
    #: Flags present in the target but not the source, per value.
    added_flags: Dict[str, List[str]]

    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.changed)


def diff_functions(src_fn: Optional[Function], tgt_fn: Optional[Function]) -> IRDiff:
    """Compare two versions of a function by SSA name rather than by line.

    Matching on names instead of positions is what makes this diff readable:
    an optimizer that reorders or renumbers instructions produces a huge
    textual diff but a tiny semantic one.
    """
    src_defs = _instruction_index(src_fn)
    tgt_defs = _instruction_index(tgt_fn)

    added = [f"{n}: {tgt_defs[n].raw}" for n in tgt_defs if n not in src_defs]
    removed = [f"{n}: {src_defs[n].raw}" for n in src_defs if n not in tgt_defs]
    changed: List[Tuple[str, str, str]] = []
    added_flags: Dict[str, List[str]] = {}
    for name, s_inst in src_defs.items():
        t_inst = tgt_defs.get(name)
        if t_inst is None or t_inst.raw == s_inst.raw:
            continue
        changed.append((name, s_inst.raw, t_inst.raw))
        new_flags = [f for f in t_inst.present_flags() if f not in s_inst.present_flags()]
        if new_flags:
            added_flags[name] = new_flags
    return IRDiff(added, removed, changed, added_flags)


def dependency_chain(fn: Optional[Function], seeds: Sequence[str]) -> List[str]:
    """The instructions that actually feed the implicated values, in order."""
    if fn is None:
        return []
    from .irmodel import backward_slice  # local import: avoids a cycle at module load

    needed = backward_slice(fn, seeds)
    return [
        f"{i.result} = {i.raw.split('=', 1)[1].strip()}" if i.result and "=" in i.raw else i.raw
        for i in fn.instructions()
        if i.result in needed
    ]


def divergence(result: FunctionResult) -> List[str]:
    """Values whose source and target evaluations disagree, with both values."""
    src_vals = {a.name: a.value for a in result.src_trace}
    out: List[str] = []
    for a in result.tgt_trace:
        s = src_vals.get(a.name)
        if s is not None and s != a.value:
            out.append(f"{a.name}: source = {s}   target = {a.value}")
        elif s is None:
            out.append(f"{a.name}: source = (not computed)   target = {a.value}")
    return out


def _interpretation(result: FunctionResult, diff: IRDiff) -> List[str]:
    """A mechanical restatement of why the target fails to refine the source."""
    lines: List[str] = []
    obligation = OBLIGATION.get(result.error_class or "")
    if obligation:
        lines.append(obligation)
    else:
        lines.append(
            f"alive2 reports {result.error_class or 'a refinement failure'}; the "
            "target does not refine the source."
        )

    if result.src_value and result.tgt_value:
        lines.append(
            f"On the input above the source yields {result.src_value} while the "
            f"target yields {result.tgt_value}."
        )

    div = divergence(result)
    if div:
        first = div[0].split(":", 1)[0]
        lines.append(f"The earliest reported divergence is at {first}.")

    if diff.added_flags:
        for name, flags in diff.added_flags.items():
            joined = ", ".join(f"'{f}'" for f in flags)
            lines.append(
                f"The target attaches {joined} to {name}, which the source does "
                f"not. If that guarantee is not implied by the source, attaching "
                f"it is unsound on its own."
            )
    return lines


def render_structured(
    result: FunctionResult,
    src_ir: str,
    tgt_ir: str,
    *,
    bug_type: str = "miscompilation",
    include_ir: bool = True,
    include_raw: bool = False,
) -> str:
    """Render the counterexample into the field layout from ``context.md`` s25.

    ``src_ir``/``tgt_ir`` are the IR the LLM should be shown -- the reduced
    pair under the reduced conditions, the original pair under the raw ones.
    """
    try:
        src_fn = parse_module(src_ir).function(result.name)
        tgt_fn = parse_module(tgt_ir).function(result.name)
    except Exception:  # pragma: no cover - never let rendering fail the loop
        src_fn = tgt_fn = None

    seeds = sorted(violation_seeds(result))
    diff = diff_functions(src_fn, tgt_fn)
    fn_label = result.name or "<unnamed>"

    parts: List[str] = [
        "BUG TYPE:",
        f"    {bug_type}",
        "",
        "VERIFICATION RESULT:",
        f"    Invalid refinement in {fn_label} -- {result.error_class or 'refinement failure'}",
        "",
        "VIOLATED PROPERTY:",
        f"    {result.violated_property}",
        "",
    ]

    if include_ir:
        parts += [
            "SOURCE (pre-transformation IR):",
            src_ir.strip(),
            "",
            "TARGET (post-transformation IR):",
            tgt_ir.strip(),
            "",
        ]

    parts += ["WHAT THE TRANSFORMATION CHANGED:"]
    if diff.is_empty():
        parts.append("    (source and target are structurally identical by SSA name)")
    else:
        if diff.changed:
            parts.append("  modified:")
            for name, s, t in diff.changed:
                parts.append(f"    {name}")
                parts.append(f"      source: {s}")
                parts.append(f"      target: {t}")
        if diff.added:
            parts.append("  only in target:")
            parts.append(_fmt(diff.added))
        if diff.removed:
            parts.append("  only in source:")
            parts.append(_fmt(diff.removed))
    parts.append("")

    parts += [
        "CRITICAL VALUES:",
        _fmt(seeds),
        "",
        "DEPENDENCY CHAIN (source):",
        _fmt(dependency_chain(src_fn, seeds)),
        "",
        "CONTROL FLOW (blocks entered by the failing execution):",
        _fmt(result.executed_blocks() or ([src_fn.entry_ref()] if src_fn and src_fn.entry_ref() else [])),
        "",
        "COUNTEREXAMPLE INPUT:",
        _fmt([str(a) for a in result.example]),
        "",
        "DIVERGENCE (source vs target on that input):",
        _fmt(divergence(result)),
        "",
    ]

    if result.src_memory or result.tgt_memory:
        parts += ["MEMORY STATE:"]
        if result.src_memory:
            parts += ["  source:", result.src_memory.rstrip()]
        if result.tgt_memory:
            parts += ["  target:", result.tgt_memory.rstrip()]
        parts.append("")

    parts += ["INTERPRETATION:"]
    parts += [f"    {line}" for line in _interpretation(result, diff)]

    if include_raw:
        parts += ["", "RAW ALIVE2 OUTPUT:", result.raw.strip()]

    return "\n".join(parts).rstrip() + "\n"


def render_plain(result: FunctionResult, src_ir: str, tgt_ir: str) -> str:
    """Unstructured rendering: the IR pair plus alive2's own verdict text.

    This is what the *unstructured* conditions show.  Under the raw condition
    it is byte-for-byte what ``llvm_helper.alive2_check`` already produces, so
    condition "raw + unstructured" reproduces today's baseline exactly.
    """
    verdict = result.raw
    marker = verdict.find("Transformation doesn't verify!")
    tail = verdict[marker:].strip() if marker != -1 else verdict.strip()
    return (
        "Source:\n"
        f"{src_ir.strip()}\n\n"
        "Target:\n"
        f"{tgt_ir.strip()}\n\n"
        f"{tail}\n"
    )
