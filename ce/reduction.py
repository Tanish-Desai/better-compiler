"""STEP 4 -- Shared machinery used by both shrinkers.

WHAT THIS FILE IS FOR
---------------------
Two things live here:

1. :class:`Reduction` -- the report card.  Both shrinkers fill in the same
   record (how big was it before, how big after, how many Alive2 calls did it
   cost, how long did it take).  Using one shared record is what lets us
   compare the two strategies fairly.

2. :func:`ddmin` -- the classic shrinking algorithm, explained below.

WHAT IS ddmin?
--------------
"ddmin" is short for **delta debugging minimization** (Zeller & Hildebrandt).
It is the standard algorithm for "I have a list of things, some subset of them
causes a failure, find me a small subset that still fails."

The naive approach is to remove items one at a time, which is slow.  ddmin is
smarter: it removes *big chunks* first and only narrows down when a big removal
fails.  Roughly::

    try deleting the first half   -> still fails? great, keep going from there
    try deleting the second half  -> still fails? great, keep going from there
    neither worked?               -> split into smaller chunks and try again

The result is *1-minimal*: you cannot delete any single remaining item and
still have it fail.

It needs one thing from you: a ``test(subset)`` function that answers "does
this subset still fail?"  In our case, that function is the oracle from
``oracle.py`` -- i.e. "does Alive2 still report the same bug?"

Both shrinkers use ddmin, just over different kinds of "items":

  * ``reduce_generic.py`` runs it over **lines of text**
  * ``reduce_iraware.py`` runs it over **instructions** and **flags**

That difference is essentially the whole experiment.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, TypeVar

from .irmodel import measure_pair

T = TypeVar("T")


@dataclass
class Reduction:
    """The outcome of reducing one counterexample."""

    src: str
    tgt: str
    strategy: str
    #: Names of the passes that actually removed something, in order applied.
    passes_applied: List[str] = field(default_factory=list)
    size_before: Dict[str, int] = field(default_factory=dict)
    size_after: Dict[str, int] = field(default_factory=dict)
    oracle_stats: Dict[str, object] = field(default_factory=dict)
    seconds: float = 0.0
    #: Set when reduction could not run at all; src/tgt are then the originals.
    error: Optional[str] = None

    @property
    def reduced(self) -> bool:
        return self.error is None and self.size_after != self.size_before

    def ratio(self, metric: str = "instructions") -> Optional[float]:
        """Fraction of ``metric`` removed, or None if it was zero to begin."""
        before = self.size_before.get(metric, 0)
        if not before:
            return None
        return 1.0 - (self.size_after.get(metric, 0) / before)

    def summary(self) -> dict:
        out = {
            "strategy": self.strategy,
            "passes_applied": self.passes_applied,
            "size_before": self.size_before,
            "size_after": self.size_after,
            "seconds": round(self.seconds, 3),
            **self.oracle_stats,
        }
        for metric in ("instructions", "lines", "chars"):
            r = self.ratio(metric)
            if r is not None:
                out[f"reduction_ratio_{metric}"] = round(r, 4)
        if self.error:
            out["error"] = self.error
        return out


class _Timer:
    def __enter__(self):
        self.start = time.time()
        return self

    def __exit__(self, *exc):
        self.elapsed = time.time() - self.start
        return False


def make_reduction(
    strategy: str, src0: str, tgt0: str, src: str, tgt: str, **kw
) -> Reduction:
    """Build a :class:`Reduction`, filling in the before/after size metrics."""
    return Reduction(
        src=src,
        tgt=tgt,
        strategy=strategy,
        size_before=measure_pair(src0, tgt0),
        size_after=measure_pair(src, tgt),
        **kw,
    )


def ddmin(
    units: Sequence[T],
    test: Callable[[List[T]], bool],
    *,
    should_stop: Callable[[], bool] = lambda: False,
) -> List[T]:
    """Classic Zeller/Hildebrandt ddmin, returning a 1-minimal subset.

    ``test(subset)`` must return True when ``subset`` still reproduces.  The
    input ``units`` is assumed to reproduce; that is not re-checked.
    """
    current = list(units)
    n = 2
    while len(current) >= 2:
        if should_stop():
            break
        chunk = max(1, len(current) // n)
        chunks = [current[i:i + chunk] for i in range(0, len(current), chunk)]

        # Try each chunk alone ("reduce to subset").
        for c in chunks:
            if should_stop():
                return current
            if len(c) < len(current) and test(c):
                current, n = c, 2
                break
        else:
            # Try each complement ("reduce to complement").
            for c in chunks:
                if should_stop():
                    return current
                complement = [u for u in current if u not in c]
                if complement and len(complement) < len(current) and test(complement):
                    current = complement
                    n = max(n - 1, 2)
                    break
            else:
                if n >= len(current):
                    break
                n = min(len(current) * 2, len(current))
    return current
