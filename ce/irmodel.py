"""STEP 2 -- Read LLVM IR into Python objects we can edit.

WHAT THIS FILE IS FOR
---------------------
To shrink a counterexample intelligently we have to actually *understand* the
code, not just treat it as lines of text.  This file reads LLVM IR and builds
a small model of it that the rest of the package can inspect and modify.

A 60-SECOND INTRO TO LLVM IR
----------------------------
LLVM IR is the intermediate language LLVM optimizes.  A tiny example::

    define i8 @f(i8 %x, i8 %y) {   <- a function taking two 8-bit integers
    entry:                         <- a "basic block" label
      %a = add nsw i8 %x, 1        <- an instruction
      %b = mul i8 %y, 3
      %c = add i8 %a, %b
      ret i8 %c                    <- a "terminator": ends the block
    }

Things worth knowing:

**SSA (Static Single Assignment)**
    Every named value (``%a``, ``%b``, ...) is assigned **exactly once** and
    never changes.  This is enormously convenient for us: the name ``%a``
    unambiguously identifies one instruction forever.  There is no "what was
    %a at this point in the program?" question to answer.

**def-use**
    "def" = the instruction that creates a value.  "use" = an instruction that
    reads it.  Above, ``%a`` is *defined* by the ``add`` and *used* by ``%c``.
    Following these links is how we work out what depends on what.

**basic block**
    A straight-line run of instructions with a label (``entry:``) and exactly
    one terminator at the end (``ret``, ``br``, ...).  Control flow only
    happens at the ends of blocks.

**phi node**
    When two paths merge, ``phi`` picks a value based on *which block you came
    from*::

        %p = phi i8 [ %r1, %then ], [ %r2, %else ]

    It means "if we arrived from %then use %r1, if from %else use %r2".

**flags** (``nsw``, ``nuw``, ``inbounds``, ``exact``, ...)
    Promises attached to an instruction.  ``nsw`` on an ``add`` means "I
    promise this addition never overflows a signed integer."  If the promise
    turns out to be false, the result becomes *poison*.  Optimizers attach
    these to enable further optimizations -- and **attaching one that isn't
    justified is one of the most common miscompilation bugs there is.**

WHAT THIS FILE DELIBERATELY IS NOT
----------------------------------
This is *not* a real LLVM parser.  It is line-oriented and understands only as
much structure as the shrinker needs:

* module preamble (datalayout, triple, type/global/declare, attribute groups,
  metadata) versus function definitions,
* a function's signature, parameter list and basic blocks,
* an instruction's result name, opcode, value operands and label operands.

That is enough to follow def-use links, delete blocks, drop parameters and
strip flags.

THE ONE SAFETY RULE
-------------------
**Anything we do not understand must survive parse-then-print unchanged, byte
for byte.**  We only "understand" a fraction of LLVM IR, and real reproducers
are full of vectors, metadata, attribute groups and other things we do not
model.  If we silently dropped or reformatted those, we would corrupt code we
only meant to shrink.

This rule is checked automatically against all 1462 real reproducers in the
benchmark dataset -- see ``scripts/check_ir_roundtrip.py``.

If the model *does* get an edit wrong, the result is IR that Alive2 rejects,
and the oracle (``oracle.py``) throws that candidate away.  So the model's job
is to *propose* good edits; it never has to *guarantee* them.

The IR read here is the *original* ``.ll`` text from the benchmark
(``alive2_check``'s ``src``/``tgt``), not Alive2's own re-printed version of
it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

#: An SSA/local name: %foo, %1, %foo.bar, or the quoted form %"a b".
NAME_RE = re.compile(r'%(?:"[^"]*"|[\w.$-]+)')
#: A global name: @foo or @"a b".
GLOBAL_RE = re.compile(r'@(?:"[^"]*"|[\w.$-]+)')
#: "label %foo" -- a control-flow operand rather than a value operand.
LABEL_OPERAND_RE = re.compile(r'\blabel\s+(%(?:"[^"]*"|[\w.$-]+))')

_DEFINE_RE = re.compile(r"^\s*define\b")
_BLOCK_LABEL_RE = re.compile(r'^\s*(?P<label>[\w.$-]+|"[^"]*"):')
_RESULT_RE = re.compile(r'^\s*(?P<name>%(?:"[^"]*"|[\w.$-]+))\s*=\s*(?P<rhs>.*)$')

TERMINATORS = frozenset(
    {"ret", "br", "switch", "indirectbr", "invoke", "callbr", "resume",
     "catchswitch", "catchret", "cleanupret", "unreachable"}
)

#: Flags and attributes whose presence or absence is frequently the *substance*
#: of a middle-end miscompilation (LLVM propagating a guarantee it has not
#: earned).  The reducer may try removing them, but only ever oracle-gated.
POISON_FLAGS = ("nsw", "nuw", "exact", "inbounds", "nneg", "disjoint", "samesign")
FASTMATH_FLAGS = ("nnan", "ninf", "nsz", "arcp", "contract", "afn", "reassoc", "fast")


def _strip_comment(line: str) -> str:
    """Drop a trailing ``;`` comment, respecting string literals."""
    out, in_str = [], False
    for ch in line:
        if ch == '"':
            in_str = not in_str
        elif ch == ";" and not in_str:
            break
        out.append(ch)
    return "".join(out)


@dataclass
class Instruction:
    """One instruction line (or bracket-continued group of lines)."""

    raw: str
    indent: str = "  "
    result: Optional[str] = None
    opcode: str = ""
    #: Local value operands referenced on the right-hand side.
    operands: List[str] = field(default_factory=list)
    #: Block labels referenced (terminators, and phi incoming edges).
    labels: List[str] = field(default_factory=list)
    #: Blank and comment-only lines that preceded this instruction. Carried so
    #: that parse-then-print is byte-exact; deleting the instruction correctly
    #: takes its own trivia with it.
    leading: List[str] = field(default_factory=list)

    @property
    def is_terminator(self) -> bool:
        return self.opcode in TERMINATORS

    @property
    def is_phi(self) -> bool:
        return self.opcode == "phi"

    def text(self) -> str:
        return "\n".join([*self.leading, self.indent + self.raw])

    def rename_operands(self, mapping: Dict[str, str]) -> "Instruction":
        """Return a copy with value operands substituted per ``mapping``.

        The result name is never rewritten -- only uses are.
        """
        if not mapping:
            return self
        if self.result is not None:
            head, sep, rhs = self.raw.partition("=")
        else:
            head, sep, rhs = "", "", self.raw

        def sub(m: re.Match) -> str:
            return mapping.get(m.group(0), m.group(0))

        return self._rebuild(head + sep + NAME_RE.sub(sub, rhs))

    def without_flags(self, flags: Sequence[str]) -> "Instruction":
        """Return a copy with the named flags removed from the opcode part."""
        new = self.raw
        for flag in flags:
            new = re.sub(rf"(?<=[\s(]){re.escape(flag)}\s+", "", new)
        return self._rebuild(new)

    def _rebuild(self, raw: str) -> "Instruction":
        """Re-parse edited text while keeping this instruction's trivia."""
        out = parse_instruction(raw, self.indent)
        out.leading = list(self.leading)
        return out

    def present_flags(self) -> List[str]:
        """Which known poison/fast-math flags this instruction carries."""
        return [f for f in (*POISON_FLAGS, *FASTMATH_FLAGS)
                if re.search(rf"(?<![\w.]){re.escape(f)}(?![\w.])", self.raw)]


def parse_instruction(line: str, indent: Optional[str] = None) -> Instruction:
    """Parse a single instruction line into an :class:`Instruction`."""
    if indent is None:
        # Take the indentation exactly as written -- real reproducers include
        # instructions at column 0, and normalising them breaks round-tripping.
        indent = line[: len(line) - len(line.lstrip())]
    body = line.strip()
    code = _strip_comment(body)

    m = _RESULT_RE.match(code)
    if m:
        result, rhs = m.group("name"), m.group("rhs")
    else:
        result, rhs = None, code

    opcode_m = re.match(r"^([\w.]+)", rhs.strip())
    opcode = opcode_m.group(1) if opcode_m else ""

    labels = LABEL_OPERAND_RE.findall(rhs)
    # phi incoming blocks are written "[ %val, %block ]" with no 'label'
    # keyword, so recover them positionally.
    if opcode == "phi":
        labels += [b for _, b in re.findall(
            r"\[\s*([^,\]]+?)\s*,\s*(%(?:\"[^\"]*\"|[\w.$-]+))\s*\]", rhs)]
    label_set = set(labels)
    operands = [n for n in NAME_RE.findall(rhs) if n not in label_set]

    return Instruction(
        raw=body,
        indent=indent,
        result=result,
        opcode=opcode,
        operands=list(dict.fromkeys(operands)),
        labels=list(dict.fromkeys(labels)),
    )


@dataclass
class Block:
    """A basic block: an optional label line plus its instructions."""

    label: Optional[str]
    instructions: List[Instruction] = field(default_factory=list)
    #: The verbatim label line, so trailing "; preds = ..." comments survive.
    label_line: Optional[str] = None
    #: Blank and comment-only lines preceding the label line.
    leading: List[str] = field(default_factory=list)

    @property
    def ref(self) -> Optional[str]:
        """The block's name as operands spell it, e.g. ``%entry``."""
        return None if self.label is None else "%" + self.label

    def defs(self) -> List[str]:
        return [i.result for i in self.instructions if i.result]

    def terminator(self) -> Optional[Instruction]:
        return self.instructions[-1] if self.instructions and self.instructions[-1].is_terminator else None

    def successors(self) -> List[str]:
        term = self.terminator()
        return list(term.labels) if term else []

    def lines(self) -> List[str]:
        out = list(self.leading)
        if self.label_line is not None:
            out.append(self.label_line)
        out.extend(i.text() for i in self.instructions)
        return out


@dataclass
class Param:
    """One formal parameter, split into its type/attribute prefix and name."""

    raw: str

    @property
    def name(self) -> Optional[str]:
        m = NAME_RE.search(self.raw)
        return m.group(0) if m else None

    @property
    def type(self) -> str:
        return self.raw.split()[0] if self.raw.split() else ""


@dataclass
class Function:
    """A ``define``-d function."""

    signature: str
    name: str
    params: List[Param] = field(default_factory=list)
    blocks: List[Block] = field(default_factory=list)
    closing: str = "}"
    #: Blank and comment-only lines between the last instruction and "}".
    trailing: List[str] = field(default_factory=list)

    def instructions(self) -> Iterable[Instruction]:
        for b in self.blocks:
            yield from b.instructions

    def def_map(self) -> Dict[str, Instruction]:
        return {i.result: i for i in self.instructions() if i.result}

    def block_map(self) -> Dict[str, Block]:
        return {b.ref: b for b in self.blocks if b.ref}

    def param_names(self) -> List[str]:
        return [p.name for p in self.params if p.name]

    def uses(self) -> Dict[str, Set[str]]:
        """Value name -> set of instruction results that consume it."""
        out: Dict[str, Set[str]] = {}
        for inst in self.instructions():
            for op in inst.operands:
                out.setdefault(op, set()).add(inst.result or f"<{inst.opcode}>")
        return out

    def entry_ref(self) -> Optional[str]:
        return self.blocks[0].ref if self.blocks else None

    def text(self) -> str:
        lines = [self.signature]
        for b in self.blocks:
            lines.extend(b.lines())
        lines.extend(self.trailing)
        lines.append(self.closing)
        return "\n".join(lines)


@dataclass
class Module:
    """A parsed ``.ll`` file: opaque preamble/trailer around parsed functions."""

    #: Chunks in source order. Each is either a raw string or a Function.
    chunks: List[object] = field(default_factory=list)

    @property
    def functions(self) -> List[Function]:
        return [c for c in self.chunks if isinstance(c, Function)]

    def function(self, name: Optional[str]) -> Optional[Function]:
        fns = self.functions
        if name is None:
            return fns[0] if fns else None
        for fn in fns:
            if fn.name == name:
                return fn
        return None

    def replace_function(self, old: Function, new: Function) -> "Module":
        return Module([new if c is old else c for c in self.chunks])

    def text(self) -> str:
        # Empty chunks are meaningful: they are the blank lines that separated
        # two functions, and dropping them breaks byte-exact round-tripping.
        out: List[str] = [
            chunk.text() if isinstance(chunk, Function) else chunk
            for chunk in self.chunks
        ]
        return "\n".join(out) + "\n"


def _split_params(sig: str) -> List[Param]:
    """Split the parameter list out of a ``define`` line, respecting nesting."""
    start = sig.find("(")
    if start == -1:
        return []
    depth, end = 0, -1
    for i in range(start, len(sig)):
        if sig[i] in "(<[{":
            depth += 1
        elif sig[i] in ")>]}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end == -1:
        return []
    inner, params, depth, buf = sig[start + 1:end], [], 0, []
    for ch in inner:
        if ch in "(<[{":
            depth += 1
        elif ch in ")>]}":
            depth -= 1
        if ch == "," and depth == 0:
            params.append(Param(("".join(buf)).strip()))
            buf = []
            continue
        buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        params.append(Param(tail))
    return params


def _balanced(text: str) -> bool:
    """True when brackets in ``text`` are balanced (ignoring strings)."""
    depth, in_str = 0, False
    for ch in _strip_comment(text):
        if ch == '"':
            in_str = not in_str
        elif not in_str:
            if ch in "[<":
                depth += 1
            elif ch in "]>":
                depth -= 1
    return depth <= 0


def parse_module(text: str) -> Module:
    """Parse a ``.ll`` file into a :class:`Module`."""
    lines = text.splitlines()
    chunks: List[object] = []
    preamble: List[str] = []
    i, n = 0, len(lines)

    while i < n:
        line = lines[i]
        if not _DEFINE_RE.match(line):
            preamble.append(line)
            i += 1
            continue

        if preamble:
            chunks.append("\n".join(preamble))
            preamble = []

        # Signature may wrap before the opening brace.
        sig_lines = [line]
        while "{" not in sig_lines[-1] and i + 1 < n:
            i += 1
            sig_lines.append(lines[i])
        signature = "\n".join(sig_lines)
        name_m = GLOBAL_RE.search(signature)
        fn = Function(
            signature=signature,
            name=name_m.group(0) if name_m else "@<anon>",
            params=_split_params(signature),
        )

        # Body: an implicit entry block until the first explicit label.
        current = Block(label=None, label_line=None)
        trivia: List[str] = []
        i += 1
        while i < n and lines[i].strip() != "}":
            raw = lines[i]
            stripped = raw.strip()
            # Blank and comment-only lines belong to whatever comes next, so
            # they survive printing without becoming pseudo-instructions.
            if not stripped or stripped.startswith(";"):
                trivia.append(raw)
                i += 1
                continue
            lbl = _BLOCK_LABEL_RE.match(raw)
            if lbl and not raw.startswith((" ", "\t")):
                if current.instructions or current.label is not None:
                    fn.blocks.append(current)
                current = Block(
                    label=lbl.group("label").strip('"'), label_line=raw, leading=trivia
                )
                trivia = []
                i += 1
                continue
            # Join bracket-continued instructions (switch, long phi lists).
            body = raw
            while not _balanced(body) and i + 1 < n and lines[i + 1].strip() != "}":
                i += 1
                body += "\n" + lines[i]
            inst = parse_instruction(body)
            inst.leading = trivia
            trivia = []
            current.instructions.append(inst)
            i += 1
        fn.blocks.append(current)
        fn.trailing = trivia
        fn.closing = lines[i] if i < n else "}"
        chunks.append(fn)
        i += 1

    if preamble:
        chunks.append("\n".join(preamble))
    return Module(chunks)


# --------------------------------------------------------------------------
# Dependency analysis
# --------------------------------------------------------------------------

def backward_slice(fn: Function, seeds: Iterable[str]) -> Set[str]:
    """Names transitively required to compute ``seeds``.

    Follows SSA def-use edges backwards and, for every instruction pulled in,
    also pulls in the branch conditions of the blocks that can reach it -- a
    value's meaning depends on the path taken to it, so a slice that keeps the
    computation but drops the control flow is not a valid witness.
    """
    defs = fn.def_map()
    block_of = {i.result: b for b in fn.blocks for i in b.instructions if i.result}
    preds: Dict[Optional[str], Set[Optional[str]]] = {b.ref: set() for b in fn.blocks}
    for b in fn.blocks:
        for succ in b.successors():
            preds.setdefault(succ, set()).add(b.ref)

    needed: Set[str] = set()
    live_blocks: Set[Optional[str]] = set()
    work = list(seeds)
    while work:
        name = work.pop()
        if name in needed:
            continue
        needed.add(name)
        inst = defs.get(name)
        if inst is None:
            continue  # a parameter, global or constant: nothing to expand
        work.extend(inst.operands)
        blk = block_of.get(name)
        if blk is not None and blk.ref not in live_blocks:
            live_blocks.add(blk.ref)
            # Reaching this block is part of the witness: keep the conditions
            # of every block that branches into it, transitively.
            frontier = [blk.ref]
            while frontier:
                cur = frontier.pop()
                for pred_ref in preds.get(cur, ()):  # noqa: B007
                    pred_blk = fn.block_map().get(pred_ref)
                    if pred_blk is None or pred_ref in live_blocks:
                        continue
                    live_blocks.add(pred_ref)
                    term = pred_blk.terminator()
                    if term:
                        work.extend(term.operands)
                    frontier.append(pred_ref)
    return needed


def forward_cone(fn: Function, roots: Iterable[str]) -> Set[str]:
    """Names transitively computed *from* ``roots`` (the def-use closure)."""
    uses = fn.uses()
    seen: Set[str] = set()
    work = list(roots)
    while work:
        name = work.pop()
        if name in seen:
            continue
        seen.add(name)
        work.extend(uses.get(name, ()))
    return seen


def dead_names(fn: Function) -> List[str]:
    """Results that nothing else in the function reads."""
    uses = fn.uses()
    return [i.result for i in fn.instructions()
            if i.result and not i.is_terminator and not uses.get(i.result)]


# --------------------------------------------------------------------------
# Edits.  Each returns a new Function; none mutate in place.
# --------------------------------------------------------------------------

def remove_instructions(fn: Function, names: Set[str]) -> Function:
    """Delete the instructions defining ``names``.

    Callers are responsible for ensuring nothing still uses them (see
    :func:`dead_names`); if something does, the result is invalid IR and the
    oracle will reject it.
    """
    blocks = [
        Block(
            label=b.label,
            label_line=b.label_line,
            instructions=[i for i in b.instructions if i.result not in names],
        )
        for b in fn.blocks
    ]
    return replace(fn, blocks=blocks)


def substitute(fn: Function, mapping: Dict[str, str]) -> Function:
    """Rewrite every *use* of the mapped names, leaving definitions alone."""
    blocks = [
        Block(
            label=b.label,
            label_line=b.label_line,
            instructions=[i.rename_operands(mapping) for i in b.instructions],
        )
        for b in fn.blocks
    ]
    return replace(fn, blocks=blocks)


def append_params(fn: Function, new_params: Sequence[str]) -> Function:
    """Add formal parameters, rewriting the ``define`` line's argument list."""
    if not new_params:
        return fn
    sig = fn.signature
    close = sig.rfind(")", 0, sig.find("{") if "{" in sig else len(sig))
    if close == -1:
        return fn
    existing = sig[:close].rstrip()
    sep = "" if existing.endswith("(") else ", "
    new_sig = existing + sep + ", ".join(new_params) + sig[close:]
    return replace(fn, signature=new_sig, params=_split_params(new_sig))


def drop_params(fn: Function, names: Set[str]) -> Function:
    """Remove formal parameters by name; uses must already be gone."""
    keep = [p for p in fn.params if p.name not in names]
    if len(keep) == len(fn.params):
        return fn
    sig = fn.signature
    open_paren = sig.find("(")
    close = sig.rfind(")", 0, sig.find("{") if "{" in sig else len(sig))
    if open_paren == -1 or close == -1:
        return fn
    new_sig = sig[:open_paren + 1] + ", ".join(p.raw for p in keep) + sig[close:]
    return replace(fn, signature=new_sig, params=keep)


def remove_blocks(fn: Function, refs: Set[str]) -> Function:
    """Delete whole basic blocks, pruning phi incomings that referenced them.

    The entry block is never removed.
    """
    entry = fn.entry_ref()
    refs = {r for r in refs if r != entry and r is not None}
    if not refs:
        return fn
    kept: List[Block] = []
    for b in fn.blocks:
        if b.ref in refs:
            continue
        kept.append(Block(
            label=b.label,
            label_line=b.label_line,
            instructions=[_prune_phi(i, refs) for i in b.instructions],
        ))
    return replace(fn, blocks=kept)


def _prune_phi(inst: Instruction, dead_refs: Set[str]) -> Instruction:
    if not inst.is_phi:
        return inst
    entries = re.findall(r"\[[^\[\]]*\]", inst.raw)
    live = [e for e in entries
            if not any(re.search(rf",\s*{re.escape(r)}\s*\]", e) for r in dead_refs)]
    if not live or len(live) == len(entries):
        return inst
    head = inst.raw[: inst.raw.find("[")]
    return parse_instruction(head + ", ".join(live), inst.indent)


def reachable_blocks(fn: Function) -> Set[Optional[str]]:
    """Blocks reachable from the entry block by any path."""
    seen: Set[Optional[str]] = set()
    work = [fn.entry_ref()]
    bmap = fn.block_map()
    while work:
        ref = work.pop()
        if ref in seen:
            continue
        seen.add(ref)
        blk = bmap.get(ref) if ref else (fn.blocks[0] if fn.blocks else None)
        if blk:
            work.extend(blk.successors())
    return seen


# --------------------------------------------------------------------------
# Size metrics
# --------------------------------------------------------------------------

@dataclass
class IRSize:
    """Structural size of an IR text, for reporting reduction ratios."""

    lines: int
    instructions: int
    blocks: int
    values: int
    chars: int

    def as_dict(self) -> dict:
        return {
            "lines": self.lines,
            "instructions": self.instructions,
            "blocks": self.blocks,
            "values": self.values,
            "chars": self.chars,
        }


def measure(text: str) -> IRSize:
    """Structural size of an IR module, tolerating unparseable input."""
    try:
        mod = parse_module(text)
        insts = sum(1 for fn in mod.functions for _ in fn.instructions())
        blocks = sum(len(fn.blocks) for fn in mod.functions)
        values = sum(len(fn.def_map()) + len(fn.param_names()) for fn in mod.functions)
    except Exception:  # pragma: no cover - parser is best-effort by design
        insts = blocks = values = 0
    return IRSize(
        lines=len([l for l in text.splitlines() if l.strip()]),
        instructions=insts,
        blocks=blocks,
        values=values,
        chars=len(text),
    )


def measure_pair(src: str, tgt: str) -> Dict[str, int]:
    """Combined size of a src/tgt pair, which is what the LLM actually reads."""
    s, t = measure(src), measure(tgt)
    return {
        "lines": s.lines + t.lines,
        "instructions": s.instructions + t.instructions,
        "blocks": s.blocks + t.blocks,
        "values": s.values + t.values,
        "chars": s.chars + t.chars,
    }
