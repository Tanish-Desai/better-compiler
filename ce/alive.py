"""STEP 1 -- Run Alive2 and read its answer.

WHAT THIS FILE IS FOR
---------------------
``alive-tv`` is a command-line program.  You give it two files:

  * **src** ("source")  -- the LLVM code *before* an optimization
  * **tgt** ("target")  -- the same code *after* the optimization

and it tries to prove that tgt is a valid replacement for src.  If it cannot,
it prints a **counterexample**: a specific input where the two behave
differently.

Alive2 prints all of this as plain text.  This file runs the program and turns
that text into Python objects, so the rest of the package can ask questions
like "which value went wrong?" instead of searching through strings.

WORDS YOU WILL SEE
------------------
**refinement**
    The rule an optimization must obey.  Roughly: "the new code must do the
    same thing as the old code, or be *more* defined -- never less".  If tgt
    breaks that rule, Alive2 says the transformation "doesn't verify".

**poison**
    LLVM's marker for "this value is garbage, because someone broke a
    promise".  It is not a number, and it spreads to anything computed from
    it.  A very common compiler bug is producing poison where the original
    code had a perfectly good value.

**UB (undefined behaviour)**
    "The program did something illegal, so all bets are off."  An optimization
    is not allowed to *introduce* UB into a program that did not already have
    it.

**error class**
    Alive2's one-line summary of *which* rule was broken, for example "Target
    is more poisonous than source".  We treat this as the identity of the bug:
    it is how ``oracle.py`` later checks that shrinking the counterexample has
    not accidentally turned it into a *different* bug.

A NOTE ON FRAGILITY
-------------------
Alive2's output format is not a documented, stable interface -- it is just
what the tool happens to print.  So all parsing here is best-effort: anything
we fail to recognise is kept verbatim in :attr:`AliveRun.raw`, and callers can
always fall back to that raw text instead of crashing.

WHAT THE OUTPUT LOOKS LIKE
--------------------------
A run over a file produces one ``----------------------------------------``
delimited section per function pair, each of which either says the
transformation is correct or reports a single violation::

    ----------------------------------------
    define i8 @f(i8 %x) {          <- source function
    ...
    }
    =>
    define i8 @f(i8 %x) {          <- target function
    ...
    }
    Transformation doesn't verify!

    ERROR: Target is more poisonous than source

    Example:
    i8 %x = #x7e (126)             <- input assignment

    Source:
    i8 %a = #x7f (127)             <- per-value trace
      >> Jump to %f

    Target:
    ...

    Source value: #x82 (130, -126)
    Target value: poison

...followed by a single trailing ``Summary:`` block for the whole run.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

SECTION_SEP = "-" * 40

# "i8 %a = #x7f (127)" / "ptr %p = pointer(non-local, block_id=1, offset=0) ..."
_ASSIGN_RE = re.compile(r"^(?P<type>[^%@]+?)\s+(?P<name>[%@][\w.$-]+)\s*=\s*(?P<value>.*)$")
# "  >> Jump to %join"
_JUMP_RE = re.compile(r"^\s*>>\s*Jump to\s+(?P<label>[%\w.$-]+)\s*$")

#: Error strings alive2 uses, mapped to the semantic property they violate.
#: Used only to label structured feedback; unknown classes pass through as-is.
ERROR_PROPERTY = {
    "Target is more poisonous than source": "poison refinement",
    "Target is more undefined than source": "undef refinement",
    "Source is more defined than target": "UB refinement",
    "Value mismatch": "return-value equality",
    "Mismatch in memory": "memory-state refinement",
    "Mismatch in return memory": "memory-state refinement",
    "Program doesn't type check": "well-formedness",
    "Timeout": "undetermined (solver timeout)",
}


@dataclass
class Assignment:
    """One ``<type> <name> = <value>`` line from an Example or trace block."""

    type: str
    name: str
    value: str
    #: Basic block jumped to immediately after this value, if the trace said so.
    jump_to: Optional[str] = None

    @property
    def is_poison(self) -> bool:
        return self.value.strip() == "poison"

    @property
    def is_undef(self) -> bool:
        return "undef" in self.value

    @property
    def is_ub(self) -> bool:
        return "UB triggered" in self.value

    def __str__(self) -> str:
        return f"{self.type} {self.name} = {self.value}"


@dataclass
class FunctionResult:
    """The verification outcome for a single source/target function pair."""

    name: Optional[str]
    src_ir: str
    tgt_ir: str
    verified: bool
    error_class: Optional[str] = None
    #: Input assignment (function arguments) that triggers the violation.
    example: List[Assignment] = field(default_factory=list)
    src_trace: List[Assignment] = field(default_factory=list)
    tgt_trace: List[Assignment] = field(default_factory=list)
    src_memory: Optional[str] = None
    tgt_memory: Optional[str] = None
    src_value: Optional[str] = None
    tgt_value: Optional[str] = None
    raw: str = ""

    @property
    def violated_property(self) -> str:
        if self.error_class is None:
            return "unknown"
        return ERROR_PROPERTY.get(self.error_class, self.error_class)

    def trace_index(self, target: bool = False) -> dict:
        """Map SSA name -> :class:`Assignment` for one side's trace."""
        return {a.name: a for a in (self.tgt_trace if target else self.src_trace)}

    def executed_blocks(self, target: bool = False) -> List[str]:
        """Labels the counterexample execution actually jumped to, in order."""
        blocks: List[str] = []
        for a in self.tgt_trace if target else self.src_trace:
            if a.jump_to and a.jump_to not in blocks:
                blocks.append(a.jump_to)
        return blocks


@dataclass
class AliveRun:
    """Everything one ``alive-tv`` invocation reported."""

    functions: List[FunctionResult] = field(default_factory=list)
    num_correct: int = 0
    num_incorrect: int = 0
    num_failed_to_prove: int = 0
    num_errors: int = 0
    raw: str = ""
    #: Set when alive-tv could not be run or produced nothing parseable.
    tool_error: Optional[str] = None

    @property
    def verified(self) -> bool:
        """True when the transformation is sound (matches ``alive2_check``)."""
        return (
            self.tool_error is None
            and self.num_incorrect == 0
            and self.num_failed_to_prove == 0
            and self.num_errors == 0
        )

    def first_violation(self) -> Optional[FunctionResult]:
        for fn in self.functions:
            if not fn.verified:
                return fn
        return None

    def violation_for(self, name: Optional[str]) -> Optional[FunctionResult]:
        """The violation in function ``name``, or the first one if unnamed."""
        if name is None:
            return self.first_violation()
        for fn in self.functions:
            if fn.name == name and not fn.verified:
                return fn
        return None


def _parse_assignments(lines: Sequence[str]) -> List[Assignment]:
    out: List[Assignment] = []
    for line in lines:
        if not line.strip():
            continue
        jump = _JUMP_RE.match(line)
        if jump:
            # A jump annotates the value printed just above it.
            if out:
                out[-1].jump_to = jump.group("label")
            continue
        m = _ASSIGN_RE.match(line.strip())
        if m:
            out.append(
                Assignment(
                    type=m.group("type").strip(),
                    name=m.group("name").strip(),
                    value=m.group("value").strip(),
                )
            )
    return out


#: Headers that introduce a chunk inside a violation report.
_REPORT_HEADERS = (
    "Example:",
    "Source:",
    "Target:",
    "SOURCE MEMORY STATE",
    "TARGET MEMORY STATE",
    "Source value:",
    "Target value:",
    "Summary:",
)


def _split_labeled_blocks(body: str) -> dict:
    """Split a violation report into its ``Header:``-introduced chunks.

    Anything before the first recognised header is returned under the ``""``
    key.  Headers that carry an inline payload (``Source value: #x82``) keep
    that payload as the chunk body.
    """
    chunks: dict = {}
    current = ""
    buf: List[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        matched = next(
            (h for h in _REPORT_HEADERS if stripped == h or stripped.startswith(h)), None
        )
        if matched:
            chunks[current] = "\n".join(buf)
            current = matched
            inline = stripped[len(matched):].strip()
            buf = [inline] if inline else []
            continue
        buf.append(line)
    chunks[current] = "\n".join(buf)
    return chunks


def _function_name(ir: str) -> Optional[str]:
    m = re.search(r"^\s*define\b.*?(@[\w.$-]+)\s*\(", ir, re.MULTILINE)
    return m.group(1) if m else None


_VERDICT_BAD = "Transformation doesn't verify!"
_VERDICT_GOOD = "Transformation seems to be correct!"


def _parse_section(section: str) -> Optional[FunctionResult]:
    """Parse one ``---``-delimited src/tgt pair plus its verdict."""
    if "\n=>\n" not in section:
        return None
    head, _, rest = section.partition("\n=>\n")
    src_ir = head.strip("\n")

    idx, marker = len(rest), None
    for m in (_VERDICT_BAD, _VERDICT_GOOD):
        pos = rest.find(m)
        if pos != -1 and pos < idx:
            idx, marker = pos, m
    tgt_ir = rest[:idx].strip("\n")
    verdict_body = rest[idx:] if marker else ""

    name = _function_name(src_ir) or _function_name(tgt_ir)
    if marker != _VERDICT_BAD:
        return FunctionResult(name, src_ir, tgt_ir, verified=True, raw=section)

    err = re.search(r"^ERROR:\s*(.+)$", verdict_body, re.MULTILINE)
    chunks = _split_labeled_blocks(verdict_body)
    return FunctionResult(
        name=name,
        src_ir=src_ir,
        tgt_ir=tgt_ir,
        verified=False,
        error_class=err.group(1).strip() if err else None,
        example=_parse_assignments(chunks.get("Example:", "").splitlines()),
        src_trace=_parse_assignments(chunks.get("Source:", "").splitlines()),
        tgt_trace=_parse_assignments(chunks.get("Target:", "").splitlines()),
        src_memory=(chunks.get("SOURCE MEMORY STATE") or None),
        tgt_memory=(chunks.get("TARGET MEMORY STATE") or None),
        src_value=(chunks.get("Source value:") or "").strip() or None,
        tgt_value=(chunks.get("Target value:") or "").strip() or None,
        raw=section,
    )


def parse_alive_output(text: str) -> AliveRun:
    """Parse the complete stdout of one ``alive-tv`` invocation."""
    run = AliveRun(raw=text)
    for section in text.split(SECTION_SEP):
        parsed = _parse_section(section)
        if parsed is not None:
            run.functions.append(parsed)

    for key, attr in (
        ("correct transformations", "num_correct"),
        ("incorrect transformations", "num_incorrect"),
        ("failed-to-prove transformations", "num_failed_to_prove"),
        ("Alive2 errors", "num_errors"),
    ):
        m = re.search(rf"^\s*(\d+)\s+{re.escape(key)}\s*$", text, re.MULTILINE)
        if m:
            setattr(run, attr, int(m.group(1)))

    if not run.functions and "Summary:" not in text:
        run.tool_error = "alive-tv produced no parseable output"
    return run


def _filter_unsupported(src: str) -> str:
    """Match llvm_helper's pre-pass so our runs agree with the benchmark's."""
    return src.replace(" noalias ", " ").replace(" nofree ", " ")


def run_alive_tv(
    src: str,
    tgt: str,
    extra_args: Sequence[str] = (),
    *,
    alive_tv: Optional[str] = None,
    timeout: float = 120.0,
    disable_undef_input: bool = True,
) -> AliveRun:
    """Verify that ``src`` is refined by ``tgt``, returning a parsed run.

    ``alive_tv`` defaults to ``$LAB_LLVM_ALIVE_TV``, the variable the benchmark
    already requires, so this needs no extra configuration in the container.
    """
    binary = alive_tv or os.environ.get("LAB_LLVM_ALIVE_TV")
    if not binary:
        return AliveRun(tool_error="LAB_LLVM_ALIVE_TV is not set")

    src, tgt = _filter_unsupported(src), _filter_unsupported(tgt)
    src_path = tgt_path = None
    try:
        # delete=False plus a manual unlink: a NamedTemporaryFile that deletes
        # on close cannot be reopened by a child process on Windows hosts.
        with tempfile.NamedTemporaryFile("w", suffix=".src.ll", delete=False) as f:
            f.write(src)
            src_path = f.name
        with tempfile.NamedTemporaryFile("w", suffix=".tgt.ll", delete=False) as f:
            f.write(tgt)
            tgt_path = f.name

        args = [binary]
        if disable_undef_input:
            args.append("--disable-undef-input")
        args += [src_path, tgt_path, *extra_args]
        proc = subprocess.run(args, capture_output=True, timeout=timeout)
        out = proc.stdout.decode(errors="replace") + proc.stderr.decode(errors="replace")
        return parse_alive_output(out)
    except subprocess.TimeoutExpired:
        return AliveRun(tool_error=f"alive-tv timed out after {timeout}s")
    except OSError as e:
        return AliveRun(tool_error=f"failed to run alive-tv: {e}")
    finally:
        for path in (src_path, tgt_path):
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass


def parse_extra_args(additional_args: Optional[str]) -> List[str]:
    """Split the benchmark's ``additional_args`` string the way it does."""
    if not additional_args:
        return []
    return [a for a in additional_args.strip().split(" ") if a]
