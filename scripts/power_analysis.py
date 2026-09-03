#!/usr/bin/env python3
"""How much can 24 bugs actually resolve? The arithmetic behind k = 3.

    python3 scripts/power_analysis.py

WHY THIS EXISTS
---------------
``docs/ANALYSIS_PLAN.md`` fixes the repeat count *k* and the statistical test
before the sweep runs. Both choices cost real compute -- every run is an LLVM
rebuild -- so neither should be picked by feel. This script is the calculation
they are picked from, kept runnable so the numbers in that document can be
checked rather than trusted.

It answers two questions.

**1. What does it even take to reach significance here?**
McNemar's exact test looks only at *discordant* bugs -- the ones exactly one
condition fixed -- and asks whether their split is worse than a coin. With
n discordant bugs the p-value is a binomial tail, so there is a hard floor:
fewer than 5 discordant bugs cannot produce p < 0.05 one-sided no matter how
lopsided they are. ``minimum_b`` prints that threshold.

**2. Does repeating the sweep buy power?**
k does *not* add statistical units: pass@k still yields one paired binary per
bug, so n stays 24 however many times each cell is re-run. What k buys is a
less noisy outcome per cell. At k = 1 and temperature 0.8, each cell is a
single Bernoulli draw, so a discordant pair is as likely to be sampling noise
as a real difference -- and noise inflates b and c symmetrically, dragging
their ratio toward 1. That is a bias toward false negatives.

``power`` measures the size of that effect by simulation. Each bug gets a
difficulty multiplier shared by both conditions, which is what the paired
design exploits; the outcome is pass@k over k draws.

WHAT IT SHOWS
-------------
Power roughly triples from k = 1 to k = 3, and gains little from k = 3 to
k = 5 -- and in the highest-rate row it *falls*, because pass@5 pushes both
conditions toward a ceiling where neither is discordant any more. That is the
argument for k = 3: it is where the curve stops paying.
"""

from __future__ import annotations

import argparse
import math
import random
from typing import Optional

ALPHA = 0.05

#: Plausible per-attempt repair rates for (better, worse), from
#: docs/SLM_SELECTION.md section 2.1: frontier models resolve 9-39% of LLVM
#: middle-end bugs with far more scaffolding than this harness gives.
SCENARIOS = (
    (0.20, 0.10), (0.25, 0.10), (0.30, 0.15),
    (0.15, 0.05), (0.35, 0.20), (0.12, 0.08),
)


def binom_sf_inclusive(k: int, n: int) -> float:
    """``P(X >= k)`` for ``X ~ Binomial(n, 1/2)`` -- the exact McNemar tail."""
    if n == 0:
        return 1.0
    return sum(math.comb(n, i) for i in range(k, n + 1)) / (2 ** n)


def minimum_b(n_discordant: int, alpha: float = ALPHA) -> Optional[int]:
    """Smallest one-way count reaching ``alpha``, or None if unreachable."""
    return next((b for b in range(n_discordant + 1)
                 if binom_sf_inclusive(b, n_discordant) < alpha), None)


def power(rate_better: float, rate_worse: float, k: int, *, n_bugs: int = 24,
          sims: int = 20000, heterogeneity: float = 0.6, seed: int = 1,
          alpha: float = ALPHA) -> float:
    """Simulated power of the one-sided exact McNemar test.

    ``heterogeneity`` scales the per-bug difficulty multiplier both conditions
    share. It is what makes the pairing worth having: without it every bug
    would be equally hard and the paired test would have no advantage over an
    unpaired one.
    """
    rng = random.Random(seed)
    hits = 0
    for _ in range(sims):
        b = c = 0
        for _ in range(n_bugs):
            difficulty = rng.uniform(1 - heterogeneity, 1 + heterogeneity)
            p_better = min(0.95, max(0.0, rate_better * difficulty))
            p_worse = min(0.95, max(0.0, rate_worse * difficulty))
            # pass@k: fixed on at least one of k independent attempts.
            fixed_better = rng.random() < 1 - (1 - p_better) ** k
            fixed_worse = rng.random() < 1 - (1 - p_worse) ** k
            if fixed_better and not fixed_worse:
                b += 1
            elif fixed_worse and not fixed_better:
                c += 1
        if b + c and binom_sf_inclusive(b, b + c) < alpha:
            hits += 1
    return hits / sims


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bugs", type=int, default=24)
    parser.add_argument("--sims", type=int, default=20000)
    parser.add_argument("--ks", type=int, nargs="+", default=[1, 3, 5])
    args = parser.parse_args(argv)

    print(f"Exact McNemar, one-sided, alpha = {ALPHA}")
    print("\nHow many discordant bugs are needed at all:")
    for n in range(1, 21):
        need = minimum_b(n)
        print(f"  {n:2d} discordant -> need {need} of them one way"
              if need is not None else
              f"  {n:2d} discordant -> unreachable")

    print(f"\nPower over {args.bugs} paired bugs ({args.sims} simulations)")
    header = "  better/worse per-attempt  " + "  ".join(f"k={k:<4}" for k in args.ks)
    print(header)
    print("  " + "-" * (len(header) - 2))
    for better, worse in SCENARIOS:
        cells = "  ".join(
            f"{power(better, worse, k, n_bugs=args.bugs, sims=args.sims):<6.2f}"
            for k in args.ks
        )
        print(f"       {better:.2f} vs {worse:.2f}         {cells}")

    print("\nPass@k implied by a per-attempt rate:")
    for rate in (0.10, 0.20, 0.30):
        row = "  ".join(f"pass@{k} {1 - (1 - rate) ** k:.2f}" for k in args.ks)
        print(f"  per-attempt {rate:.2f} -> {row}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
