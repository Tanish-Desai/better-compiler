"""STEP 5b -- Shrinker #2: the smart one. THIS IS THE RESEARCH CONTRIBUTION.

WHAT THIS FILE IS FOR
---------------------
Same job as ``reduce_generic.py`` -- make the counterexample smaller without
losing the bug -- but this one actually understands LLVM IR.

WHAT IT ACHIEVES, CONCRETELY
----------------------------
Given this (28 instructions, most of them irrelevant)::

    define i8 @f(i8 %x, i8 %y, i8 %z) {
    entry:
      %a = add nsw i8 %x, 1
      %b = mul i8 %y, 3            <- nothing to do with the bug
      %c = sub i8 %a, %b           <- nothing to do with the bug
      %d = xor i8 %z, 7            <- nothing to do with the bug
      %e = and i8 %c, %d           <- nothing to do with the bug
      %cmp = icmp slt i8 %x, 0
      br i1 %cmp, label %t, label %f
      ... three more blocks ...
      %out = add nuw i8 %p, %a
    }

it produces this::

    define i8 @f(i8 %ce.arg0, i8 %ce.arg5) {
    entry:
      %out = add i8 %ce.arg5, %ce.arg0        <- source
      ret i8 %out
    }
                        vs.
      %out = add nsw i8 %ce.arg5, %ce.arg0    <- target

The entire bug is now one visible difference: the optimizer attached ``nsw``
(a promise that the addition never overflows) when it had no right to.

THE THREE IDEAS THAT MAKE IT WORK
---------------------------------
1. **The two files are one function, twice.**
   src and tgt are before/after versions of the same code.  So every edit is
   applied to *both* at once, matched up by SSA name.  Delete ``%b`` from the
   source, delete ``%b`` from the target too.  A text-based shrinker has no
   way to even express this idea.

2. **Only propose edits that produce valid code.**
   The generic shrinker deletes a line and hopes.  This one works backwards:
   it picks the set of values it wants to *keep*, then automatically adds
   everything those values depend on (following def-use links, plus the branch
   conditions needed to reach them).  The result is always valid IR *by
   construction*.

   This is why it needs ~17 Alive2 calls where the generic one burns 183:
   it is not wasting attempts on code that does not parse.

3. **Let the counterexample guide the search.**
   Alive2 already told us which values disagree between src and tgt, which
   blocks the failing run actually executed, and the type of every value.  We
   use all three instead of rediscovering them by trial and error:
   - disagreeing values  -> what to protect from deletion ("seeds")
   - executed blocks     -> which branch to fold away
   - value types         -> how to turn a value into a function parameter

THE PASSES
----------
Each pass below is tried in order, and the whole sequence repeats until
nothing more can be removed:

    dce               delete values that nothing reads
    fold-branches     the failing run took one path; delete the other one
    prune-blocks      delete blocks that are now unreachable
    simplify-cfg      collapse leftover trivial blocks and phi nodes
    slice             protect the bug-related values, ddmin everything else
    promote-operands  replace a computed value with a plain function argument,
                      which lets the whole chain that computed it be deleted
    drop-params       remove function arguments nobody uses any more
    strip-flags       find the smallest set of nsw/nuw/inbounds that still fails

**Every one of these is checked by the oracle.**  Understanding LLVM only lets
us *suggest* good edits; it never lets us *assume* one was safe.  If a pass
cannot get an edit past the oracle it simply makes no progress and we move on.
Smaller is not automatically better.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace as dc_replace
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .alive import FunctionResult
from .irmodel import (
    Function,
    Instruction,
    Module,
    POISON_FLAGS,
    FASTMATH_FLAGS,
    append_params,
    backward_slice,
    dead_names,
    drop_params,
    parse_instruction,
    parse_module,
    reachable_blocks,
    remove_blocks,
    remove_instructions,
    substitute,
)
from .oracle import Oracle
from .reduction import Reduction, ddmin, make_reduction, _Timer

#: Prefix for parameters the reducer introduces when promoting a value.
PROMOTED_PREFIX = "%ce.arg"


# --------------------------------------------------------------------------
# Working state
# --------------------------------------------------------------------------

@dataclass
class _State:
    """The src/tgt pair, parsed, with the function under study identified."""

    src_mod: Module
    tgt_mod: Module
    src_fn: Function
    tgt_fn: Function
    #: The module chunk lists hold the *original* Function objects. Keep a
    #: stable handle to each so replace_function can find the slot to
    #: overwrite after src_fn/tgt_fn have been replaced by edited copies.
    src_slot: Optional[Function] = None
    tgt_slot: Optional[Function] = None

    def __post_init__(self) -> None:
        if self.src_slot is None:
            self.src_slot = self.src_fn
        if self.tgt_slot is None:
            self.tgt_slot = self.tgt_fn

    def render(self) -> Tuple[str, str]:
        return (
            self.src_mod.replace_function(self.src_slot, self.src_fn).text(),
            self.tgt_mod.replace_function(self.tgt_slot, self.tgt_fn).text(),
        )

    def with_functions(self, src_fn: Function, tgt_fn: Function) -> "_State":
        return _State(
            self.src_mod, self.tgt_mod, src_fn, tgt_fn, self.src_slot, self.tgt_slot
        )


@dataclass
class _Context:
    """Everything the passes need besides the state itself."""

    oracle: Oracle
    #: SSA names that must survive: the values the violation is *about*.
    seeds: Set[str]
    #: Value name -> LLVM type, harvested from the alive2 trace.
    types: Dict[str, str]
    #: Blocks the failing execution actually entered, source side.
    executed: List[str]
    allow_promotion: bool = True
    applied: List[str] = field(default_factory=list)
    #: Monotonic across the whole reduction, so promoted parameter names stay
    #: unique when the pass runs again in a later round.
    next_arg: int = 0

    def accept(self, state: _State) -> Optional[_State]:
        """Return ``state`` if the oracle still sees the target violation."""
        src, tgt = state.render()
        return state if self.oracle.check(src, tgt) else None

    def fresh_arg(self) -> str:
        name = f"{PROMOTED_PREFIX}{self.next_arg}"
        self.next_arg += 1
        return name


# --------------------------------------------------------------------------
# Seeding from the counterexample
# --------------------------------------------------------------------------

def violation_seeds(result: FunctionResult) -> Set[str]:
    """SSA names the counterexample implicates in the refinement failure.

    Three sources, in decreasing directness: values that disagree between the
    source and target traces, values that became poison/undef/UB on the target
    side only, and -- always -- whatever the function returns, since that is
    what refinement is ultimately quantified over.
    """
    src_vals = {a.name: a.value for a in result.src_trace}
    tgt_vals = {a.name: a.value for a in result.tgt_trace}

    seeds: Set[str] = set()
    for name, tgt_val in tgt_vals.items():
        src_val = src_vals.get(name)
        if src_val is None or src_val != tgt_val:
            seeds.add(name)
    for a in result.tgt_trace:
        if a.is_poison or a.is_ub or a.is_undef:
            seeds.add(a.name)
    # Values named only on the source side that vanished from the target are
    # equally implicated -- the transformation removed or replaced them.
    for name in src_vals:
        if name not in tgt_vals:
            seeds.add(name)
    return seeds


def value_types(result: FunctionResult) -> Dict[str, str]:
    """Value name -> LLVM type, as alive2 printed it in the counterexample."""
    types: Dict[str, str] = {}
    for a in (*result.example, *result.src_trace, *result.tgt_trace):
        types.setdefault(a.name, a.type)
    return types


def _returned_values(fn: Function) -> Set[str]:
    out: Set[str] = set()
    for inst in fn.instructions():
        if inst.opcode == "ret":
            out.update(inst.operands)
    return out


def _terminator_operands(fn: Function) -> Set[str]:
    out: Set[str] = set()
    for block in fn.blocks:
        term = block.terminator()
        if term:
            out.update(term.operands)
    return out


# --------------------------------------------------------------------------
# Pass: dependency-closed instruction elimination
# --------------------------------------------------------------------------

def _closed_keep(fn: Function, keep: Set[str]) -> Set[str]:
    """Close ``keep`` under everything the function still needs to be valid."""
    roots = set(keep) | _terminator_operands(fn) | _returned_values(fn)
    return backward_slice(fn, roots) | roots


def _apply_keep(fn: Function, keep: Set[str]) -> Function:
    closed = _closed_keep(fn, keep)
    removable = {i.result for i in fn.instructions() if i.result and not i.is_terminator}
    return remove_instructions(fn, removable - closed)


def _slice_pass(state: _State, ctx: _Context) -> Optional[_State]:
    """Keep the violation slice; ddmin over everything else.

    The slice is a hypothesis about relevance, not a proof, so the values it
    selects are pinned and the *remainder* is searched -- if the seeds were
    wrong the pass just fails to shrink anything rather than producing a
    counterexample that no longer demonstrates the bug.
    """
    src_pool = sorted(
        {i.result for i in state.src_fn.instructions() if i.result and not i.is_terminator}
        - ctx.seeds
    )
    if not src_pool:
        return None

    def build(keep_extra: Sequence[str]) -> _State:
        keep = ctx.seeds | set(keep_extra)
        return state.with_functions(
            _apply_keep(state.src_fn, keep),
            _apply_keep(state.tgt_fn, keep),
        )

    def test(subset: List[str]) -> bool:
        return ctx.accept(build(subset)) is not None

    kept = ddmin(src_pool, test, should_stop=lambda: ctx.oracle.exhausted)
    if len(kept) == len(src_pool):
        return None
    candidate = build(kept)
    return ctx.accept(candidate)


def _dce_pass(state: _State, ctx: _Context) -> Optional[_State]:
    """Delete values nothing reads, on both sides, to a fixpoint."""
    src_fn, tgt_fn = state.src_fn, state.tgt_fn
    changed = False
    for _ in range(16):  # bounded: each round strictly shrinks or stops
        src_dead = set(dead_names(src_fn)) - ctx.seeds
        tgt_dead = set(dead_names(tgt_fn)) - ctx.seeds
        if not src_dead and not tgt_dead:
            break
        src_fn = remove_instructions(src_fn, src_dead)
        tgt_fn = remove_instructions(tgt_fn, tgt_dead)
        changed = True
    if not changed:
        return None
    return ctx.accept(state.with_functions(src_fn, tgt_fn))


# --------------------------------------------------------------------------
# Pass: operand promotion (SSA chain shortening)
# --------------------------------------------------------------------------

def _promotable(state: _State, ctx: _Context) -> List[str]:
    """Values defined in *both* functions that could become a parameter.

    Restricting to values present on both sides keeps the two signatures in
    lockstep, which alive2 requires.  Seeds are excluded: promoting the value
    the violation is about would replace the bug with an assumption.
    """
    src_defs = state.src_fn.def_map()
    tgt_defs = state.tgt_fn.def_map()
    out = []
    for name, inst in src_defs.items():
        if name in ctx.seeds or name not in tgt_defs:
            continue
        if inst.is_phi:
            continue  # a phi's value is its control flow; promoting erases that
        if name in ctx.types:
            out.append(name)
    return out


def _promote_pass(state: _State, ctx: _Context) -> Optional[_State]:
    """Replace a computed value with a fresh argument, then DCE its producers.

    This is what collapses a long SSA dependency chain into a minimal witness:
    if ``%e = and i8 %c, %d`` only matters as "some i8", the chain computing it
    is noise.

    It is also the reducer's most aggressive move, because a fresh parameter
    ranges over *all* values of its type, which is a generalisation of the
    original program.  The oracle still guarantees the same violation, but the
    witness may no longer correspond to an execution reachable in the original
    reproducer -- so this pass is separately switchable and separately
    reported.
    """
    if not ctx.allow_promotion:
        return None
    candidates = _promotable(state, ctx)
    if not candidates:
        return None

    current = state
    promoted = 0
    for name in candidates:
        if ctx.oracle.exhausted:
            break
        if name not in current.src_fn.def_map() or name not in current.tgt_fn.def_map():
            continue  # already eliminated by an earlier promotion's DCE
        ty = ctx.types.get(name)
        if not ty:
            continue
        arg = ctx.fresh_arg()
        mapping = {name: arg}

        def rebuild(fn: Function) -> Function:
            fn = substitute(fn, mapping)
            fn = append_params(fn, [f"{ty} {arg}"])
            for _ in range(16):
                dead = set(dead_names(fn)) - ctx.seeds
                if not dead:
                    break
                fn = remove_instructions(fn, dead)
            return fn

        candidate = current.with_functions(rebuild(current.src_fn), rebuild(current.tgt_fn))
        accepted = ctx.accept(candidate)
        if accepted is not None:
            current = accepted
            promoted += 1

    return current if promoted else None


# --------------------------------------------------------------------------
# Pass: control-flow reduction
# --------------------------------------------------------------------------

_COND_BR_RE = re.compile(
    r"^br\s+i1\s+(?P<cond>[^,]+),\s*label\s+(?P<t>%[\w.$-]+),\s*label\s+(?P<f>%[\w.$-]+)\s*$"
)


def _fold_branches_pass(state: _State, ctx: _Context) -> Optional[_State]:
    """Replace conditional branches with the edge the counterexample took.

    The trace records which block the failing execution entered, so the
    untaken side of every conditional on that path is, for this witness, dead
    weight.  Folding is unsound in general -- the condition may be poison, and
    branching on poison is immediate UB -- which is exactly why the result goes
    through the oracle rather than being assumed.
    """
    executed = set(ctx.executed)
    if not executed:
        return None

    current = state
    folded = 0
    for block in list(current.src_fn.blocks):
        if ctx.oracle.exhausted:
            break
        term = block.terminator()
        if term is None:
            continue
        m = _COND_BR_RE.match(term.raw)
        if not m:
            continue
        taken = [lbl for lbl in (m.group("t"), m.group("f")) if lbl in executed]
        if len(taken) != 1:
            continue  # both or neither ran: no unambiguous edge to keep
        new_term = parse_instruction(f"br label {taken[0]}", term.indent)

        def fold(fn: Function) -> Function:
            blocks = []
            for b in fn.blocks:
                t = b.terminator()
                if t is not None and t.raw == term.raw:
                    b = dc_replace(b, instructions=b.instructions[:-1] + [new_term])
                blocks.append(b)
            return dc_replace(fn, blocks=blocks)

        candidate = current.with_functions(fold(current.src_fn), fold(current.tgt_fn))
        accepted = ctx.accept(candidate)
        if accepted is not None:
            current = accepted
            folded += 1

    return current if folded else None


def _simplify_cfg(fn: Function) -> Function:
    """Collapse trivial phis and straight-line block chains.

    Branch folding leaves behind exactly this shape -- a chain of blocks joined
    by unconditional branches, and phis with a single remaining incoming edge --
    which is pure noise in a witness. Both rewrites are semantics-preserving on
    their own; the caller still oracle-checks the combined result.
    """
    for _ in range(32):  # bounded; each iteration removes a phi or a block
        # 1. A phi with one incoming edge is just its incoming value.
        trivial = {}
        for inst in fn.instructions():
            if inst.is_phi and inst.result and len(re.findall(r"\[[^\[\]]*\]", inst.raw)) == 1:
                m = re.search(r"\[\s*([^,\]]+?)\s*,", inst.raw)
                if m:
                    trivial[inst.result] = m.group(1).strip()
        if trivial:
            fn = substitute(fn, trivial)
            fn = remove_instructions(fn, set(trivial))
            continue

        # 2. A block whose sole predecessor branches to it unconditionally can
        #    be spliced into that predecessor.
        preds: Dict[Optional[str], Set[Optional[str]]] = {}
        for b in fn.blocks:
            for succ in b.successors():
                preds.setdefault(succ, set()).add(b.ref)
        bmap = fn.block_map()
        merged = False
        for block in fn.blocks[1:]:
            owners = preds.get(block.ref, set())
            if len(owners) != 1:
                continue
            pred = bmap.get(next(iter(owners)))
            if pred is None or pred.ref == block.ref:
                continue
            term = pred.terminator()
            if term is None or term.raw.strip() != f"br label {block.ref}":
                continue
            # A phi elsewhere may still name this block as an incoming edge.
            if any(block.ref in i.labels for i in fn.instructions() if i.is_phi):
                continue
            blocks = []
            for b in fn.blocks:
                if b.ref == block.ref:
                    continue
                if b.ref == pred.ref:
                    b = dc_replace(b, instructions=b.instructions[:-1] + list(block.instructions))
                blocks.append(b)
            fn = dc_replace(fn, blocks=blocks)
            merged = True
            break
        if not merged:
            break
    return fn


def _simplify_cfg_pass(state: _State, ctx: _Context) -> Optional[_State]:
    """Apply :func:`_simplify_cfg` to both sides, oracle-gated."""
    src_fn = _simplify_cfg(state.src_fn)
    tgt_fn = _simplify_cfg(state.tgt_fn)
    if (len(src_fn.blocks), len(tgt_fn.blocks)) == (
        len(state.src_fn.blocks), len(state.tgt_fn.blocks)
    ) and src_fn.text() == state.src_fn.text() and tgt_fn.text() == state.tgt_fn.text():
        return None
    return ctx.accept(state.with_functions(src_fn, tgt_fn))


def _prune_blocks_pass(state: _State, ctx: _Context) -> Optional[_State]:
    """Delete basic blocks no longer reachable from entry."""
    def prune(fn: Function) -> Function:
        live = reachable_blocks(fn)
        dead = {b.ref for b in fn.blocks if b.ref is not None and b.ref not in live}
        return remove_blocks(fn, dead) if dead else fn

    src_fn, tgt_fn = prune(state.src_fn), prune(state.tgt_fn)
    if len(src_fn.blocks) == len(state.src_fn.blocks) and len(tgt_fn.blocks) == len(state.tgt_fn.blocks):
        return None
    return ctx.accept(state.with_functions(src_fn, tgt_fn))


# --------------------------------------------------------------------------
# Pass: signature and flag cleanup
# --------------------------------------------------------------------------

def _drop_params_pass(state: _State, ctx: _Context) -> Optional[_State]:
    """Remove formal parameters unused on both sides.

    Parameters must be dropped from both signatures together or alive2 will
    refuse the pair, so a parameter still read by either side stays.
    """
    src_used = {op for i in state.src_fn.instructions() for op in i.operands}
    tgt_used = {op for i in state.tgt_fn.instructions() for op in i.operands}
    unused = {
        p.name for p in state.src_fn.params
        if p.name and p.name not in src_used and p.name not in tgt_used and p.name not in ctx.seeds
    }
    if not unused:
        return None
    return ctx.accept(state.with_functions(
        drop_params(state.src_fn, unused),
        drop_params(state.tgt_fn, unused),
    ))


def _flag_units(fn: Function, side: str) -> List[Tuple[str, str, str]]:
    return [
        (side, inst.result or f"@{idx}", flag)
        for idx, inst in enumerate(fn.instructions())
        for flag in inst.present_flags()
    ]


def _strip_flags_pass(state: _State, ctx: _Context) -> Optional[_State]:
    """Find a minimal set of poison-generating flags that still fails.

    For a large share of LLVM middle-end miscompilations the bug *is* a flag
    the optimizer was not entitled to attach, so this pass tends to isolate the
    exact ``nsw``/``nuw``/``inbounds`` that makes the transformation unsound --
    which is the single most useful thing the feedback can point at.
    """
    units = _flag_units(state.src_fn, "src") + _flag_units(state.tgt_fn, "tgt")
    if not units:
        return None

    def build(keep: Sequence[Tuple[str, str, str]]) -> _State:
        keep_set = set(keep)

        def strip(fn: Function, side: str) -> Function:
            blocks = []
            for b in fn.blocks:
                insts = []
                for idx, inst in enumerate(b.instructions):
                    key = inst.result or f"@{idx}"
                    drop = [f for f in inst.present_flags() if (side, key, f) not in keep_set]
                    insts.append(inst.without_flags(drop) if drop else inst)
                blocks.append(dc_replace(b, instructions=insts))
            return dc_replace(fn, blocks=blocks)

        return state.with_functions(strip(state.src_fn, "src"), strip(state.tgt_fn, "tgt"))

    def test(subset: List[Tuple[str, str, str]]) -> bool:
        return ctx.accept(build(subset)) is not None

    kept = ddmin(units, test, should_stop=lambda: ctx.oracle.exhausted)
    if len(kept) == len(units):
        return None
    return ctx.accept(build(kept))


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

#: Applied in this order, repeatedly, until nothing changes.  Cheap and highly
#: selective passes come first so the expensive searches run on smaller input.
PASSES = (
    ("dce", _dce_pass),
    ("fold-branches", _fold_branches_pass),
    ("prune-blocks", _prune_blocks_pass),
    ("simplify-cfg", _simplify_cfg_pass),
    ("slice", _slice_pass),
    ("promote-operands", _promote_pass),
    ("drop-params", _drop_params_pass),
    ("strip-flags", _strip_flags_pass),
)


def reduce_iraware(
    src: str,
    tgt: str,
    oracle: Oracle,
    result: FunctionResult,
    *,
    allow_promotion: bool = True,
    max_rounds: int = 4,
) -> Reduction:
    """Reduce a counterexample using LLVM IR structure, oracle-gated throughout.

    ``result`` is the parsed alive2 violation for this pair; it supplies the
    slice seeds, the executed path and the value types.
    """
    with _Timer() as timer:
        try:
            src_mod, tgt_mod = parse_module(src), parse_module(tgt)
            src_fn = src_mod.function(result.name)
            tgt_fn = tgt_mod.function(result.name)
        except Exception as e:  # pragma: no cover - parser is best-effort
            return make_reduction("iraware", src, tgt, src, tgt,
                                  error=f"IR parse failed: {e}")
        if src_fn is None or tgt_fn is None:
            return make_reduction(
                "iraware", src, tgt, src, tgt,
                error=f"function {result.name!r} not found in both modules",
            )

        state = _State(src_mod, tgt_mod, src_fn, tgt_fn, src_fn, tgt_fn)
        ctx = _Context(
            oracle=oracle,
            seeds=violation_seeds(result) | _returned_values(src_fn),
            types=value_types(result),
            executed=result.executed_blocks(),
            allow_promotion=allow_promotion,
        )

        for _ in range(max_rounds):
            progress = False
            for name, fn in PASSES:
                if oracle.exhausted:
                    break
                new_state = fn(state, ctx)
                if new_state is not None:
                    state = new_state
                    ctx.applied.append(name)
                    progress = True
            if not progress or oracle.exhausted:
                break

        new_src, new_tgt = state.render()
        # Never hand back something the oracle has not signed off on.
        if ctx.applied and not oracle.check(new_src, new_tgt):
            new_src, new_tgt, ctx.applied = src, tgt, []

    return make_reduction(
        "iraware",
        src,
        tgt,
        new_src,
        new_tgt,
        passes_applied=ctx.applied,
        oracle_stats=oracle.stats(),
        seconds=timer.elapsed,
    )
