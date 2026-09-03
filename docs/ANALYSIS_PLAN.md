# Analysis Plan

**Status: preregistered 2026-09-04, before any repair-rate data existed.**

`results/` contained no run records when this was written — only
`results/build_logs/`, which are `opt` build transcripts. That is what makes
this a preregistration rather than a rationalisation, and it is why it is
committed as its own file with its own date instead of being folded into
`docs/METHODOLOGY.md` afterwards.

Nothing here may be revised once the sweep starts. If something does have to
change, the change goes in as a dated amendment below with its reason, and the
result is reported as exploratory.

---

## 1. The two decisions

| decision | value |
|---|---|
| **Repeat count *k*** | **3** |
| **Statistical test** | **McNemar's exact test**, one-sided, α = 0.05, Benjamini-Hochberg across the primary family |

Sections 2 and 3 give the reasoning. Section 4 fixes what gets tested;
section 5 gives the compute this commits you to.

---

## 2. Why *k* = 3

### The unit of analysis is a bug, not a run

Nine conditions are run over the same 24 bugs. Each bug therefore contributes
**one paired binary observation per condition**, and that stays true however
many times a cell is re-run: *k* does not raise n from 24. It is worth being
blunt about this, because "more runs = more power" is the intuition and it is
wrong here.

What *k* actually buys is a **less noisy outcome per cell**. At *k* = 1 and
temperature 0.8, each cell is a single Bernoulli draw. A discordant bug — one
condition fixed it, the other did not — is then about as likely to be sampling
noise as a real difference between the feedback formats. And noise inflates the
discordant counts *b* and *c* symmetrically, dragging their ratio toward 1.
That is a bias toward false negatives: at *k* = 1 the experiment is set up to
miss an effect that is really there.

The outcome at *k* > 1 is **pass@k**: a bug counts as fixed if at least one of
its *k* trials fixed it.

### The numbers

`scripts/power_analysis.py` simulates the one-sided exact McNemar test over 24
paired bugs, with a per-bug difficulty multiplier shared by both conditions
(which is the thing the paired design exploits). Per-attempt rates are taken
from `docs/SLM_SELECTION.md` §2.1, where frontier models resolve 9–39% of LLVM
middle-end bugs with far more scaffolding than this harness provides.

| per-attempt rate, better vs worse | k=1 | k=3 | k=5 |
|---|--:|--:|--:|
| 0.20 vs 0.10 | 0.11 | **0.33** | 0.43 |
| 0.25 vs 0.10 | 0.23 | **0.57** | 0.67 |
| 0.30 vs 0.15 | 0.21 | **0.45** | 0.48 |
| 0.15 vs 0.05 | 0.12 | **0.45** | 0.64 |
| 0.35 vs 0.20 | 0.20 | **0.36** | 0.33 |
| 0.12 vs 0.08 | 0.03 | **0.10** | 0.15 |

Three things decide it:

1. ***k* = 1 is not a pilot, it is a coin toss.** Power of 0.03–0.23 means that
   even where the effect is real and large, the sweep would usually fail to
   detect it. 216 runs is too much compute to spend on that.
2. ***k* = 3 roughly triples power** across every scenario, for 3× the compute.
3. ***k* = 5 adds little, and can subtract.** In the highest-rate row power
   *falls* from 0.36 to 0.33, because pass@5 pushes both conditions toward a
   ceiling where neither is discordant any more. Repeats stop being free
   information once pass@k approaches 1 — a real risk here, since pass@5 on a
   0.30 per-attempt rate is 0.83.

**Decision: *k* = 3.** It is where the curve stops paying.

### Honest statement of what this buys

Even at *k* = 3, power sits around 0.33–0.57 for a large effect and 0.10 for a
small one. **This is a pilot-scale comparison and must be reported as one.**
Two consequences, both binding:

- A p-value above 0.05 means "24 bugs cannot resolve this", **not** "there is no
  effect". Any write-up that reports a null as evidence of absence is
  misreporting this design.
- The discordant counts *b* and *c* and the paired rate difference are reported
  for every comparison regardless of the p-value, following
  `agentic_harness`'s `#01 #10 #11 #00` table. With samples this small the
  counts carry more information than the p-value does.

### What *k* is not for

If the pilot (`docs/SLM_SELECTION.md` §9) shows the model near 0% under every
condition, the response is a stronger model or a larger `--max-iterations` —
**not** a larger *k*. Repeating a floor gives you a more precise floor.

---

## 3. Why McNemar's exact test, and not Fisher's

### The design is paired

Every condition sees the same 24 bugs. Bug difficulty is therefore *within*
each comparison rather than between, which is the only reason n = 24 is
workable at all. Fisher's exact test is for two **independent** groups; applying
it here discards the pairing and, with a sample this size, most of the power
with it.

McNemar is the paired counterpart. It drops the bugs both conditions fixed and
the bugs neither fixed — those say nothing about which condition is better —
and tests only the discordant ones:

```
        b = fixed by A only        c = fixed by B only
        under H0, each discordant bug is a fair coin:
        b ~ Binomial(b + c, 1/2)
```

### Exact, not chi-squared

McNemar is usually quoted as a χ² statistic. That approximation needs
*b* + *c* ≳ 15, which this sweep will almost certainly not reach. The exact
binomial version is used instead; it costs nothing and is correct at any size.

### Precedent

Every paper in `slm_research_papers/` that tests paired binary repair outcomes
on a shared bug set uses McNemar (`docs/SLM_SELECTION.md` §6):

- **`agentic_harness`** — the closest existing work, same LLVM middle-end bug
  family — uses **one-sided McNemar at α = 0.05** and prints the full 2×2.
- **`repair_llama`** uses McNemar for "the binary outcomes of two
  representations evaluated on the same set of benchmark examples", which is
  this design with the nouns changed.
- **`slm_as_a_judge`** uses **McNemar with Benjamini-Hochberg** adjustment.

Fisher's exact appears once across the whole corpus, in
`llm_software_repair`, for a genuinely unpaired tag/outcome contingency.

### One-sided, and why that is allowed

The direction of every primary comparison is fixed by a hypothesis stated
before the data existed (§4), not read off the results. `agentic_harness` tests
one-sided for the same reason. Two-sided p-values are printed alongside anyway,
in `--json` output and in the record, so nothing is hidden.

### What "fixed" means

Unchanged from `docs/METHODOLOGY.md` §5: `env.check_full()` passes — the patch
builds, fixes the reproducer, and breaks none of LLVM's existing lit tests.
Enforced by the benchmark, not by us.

---

## 4. The family of tests, fixed in advance

Nine conditions make 36 pairs. Running all 36 and reporting the smallest
p-value is how a null gets laundered into a finding. So **four comparisons are
primary**, each the direct operationalisation of a question that was already
written down, and everything else is exploratory.

These are `PRIMARY` in `examples/analyze_significance.py`; the file is the
executable form of this section.

| # | hypothesised better | worse | question | source |
|---|---|---|---|---|
| P1 | `iraware-plain` | `generic-plain` | Does IR-aware reduction beat generic text reduction? | the research question (`context.md`) |
| P2 | `iraware-plain` | `llvmreduce-plain` | Is it counterexample-awareness, or merely IR-validity? | `docs/IMPLEMENTATION.md` Blocker 5 |
| P3 | `iraware-structured` | `iraware-plain` | Does structured layout add anything on top of reduction? | the second factor |
| P4 | `raw-plain` | `baseline` | Sanity anchor: does the counterexample help at all? | — |

- All four are **one-sided in the stated direction** and corrected together
  with **Benjamini-Hochberg**.
- P1 and P2 hold structure fixed at `plain`, per `docs/METHODOLOGY.md` §1:
  `llvmreduce` is a reduction-only baseline and makes no claim about structure.
- **P4 is a validity check, not a finding.** If the counterexample does not beat
  no-counterexample, no comparison among counterexample formats means anything.
- The remaining 32 pairs print under `--all-pairs`, uncorrected and labelled
  exploratory. Their p-values are **not** evidence and must not be quoted as
  such.

### Reported alongside, for every comparison

Repair rate per condition (pass@3 headline, pass@1 beside it), *b*, *c*, the
paired rate difference, and mean iterations-to-fix —
`docs/IMPLEMENTATION.md` Blocker 2 makes iterations the efficiency claim, since
one iteration is one LLVM rebuild and a few hundred prompt tokens are noise
against that.

### The promotion ablation

`--no-promotion` is a **separate axis, not a tenth condition**
(`docs/METHODOLOGY.md` §4, Blocker 7). Its test is preregistered here too, and
it is a different question — not "which condition wins" but "does disabling
`promote-operands` change *this* condition's outcomes":

| # | comparison | question |
|---|---|---|
| A1 | `iraware-structured` vs `iraware-structured [no-promotion]` | Does operand promotion carry the result? |
| A2 | `iraware-plain` vs `iraware-plain [no-promotion]` | Same, without the structure factor |

Same test, same α, corrected within their own family — **not** pooled with the
primary four. Run via `analyze_significance.py --ablation`. If compute forces a
cut, drop A2 and keep A1.

Promotion generalises the program (`docs/METHODOLOGY.md` §4), so this is
reported whichever way it comes out. A null here is the *good* outcome: it means
the reduction's value did not depend on the pass that weakens the witness's
relevance to the original bug.

---

## 5. What this commits you to

| | runs |
|---|--:|
| Main sweep: 24 bugs × 9 conditions × k=3 | **648** |
| Ablation: 24 bugs × 2 iraware conditions × k=3 | **144** |
| **Total** | **792** |

Each run is up to `--max-iterations` (4) LLM turns, and **each turn is an LLVM
rebuild plus the bug's lit directory**. The dominant cost is not the model.

Two things follow, and both are already built in:

- **Run bug-major.** All conditions and trials for one bug share a
  `base_commit`, so ninja and ccache only redo the patched translation unit and
  a relink. Moving to the next bug is a near-full rebuild — Blocker 9 measured
  18 minutes to 2h49m for each of the 24. Iterating condition-major, as
  `README.md` used to show, pays that switch 216k times instead of 24.
  `repair_experiment.py` now loops bug-major when given several `--condition`
  values.
- **Raise `ccache -M` to 250G first.** It is at 40G and has already run 44
  cleanups holding 24 `base_commit`s, so it is evicting entries it is about to
  need. This is the single cheapest change to the sweep's wall time.

**Minutes per iteration is currently unmeasured** — the only hard datum is
~20 hours for 24 from-scratch builds. Measure it on the nine-run pilot
(`docs/SLM_SELECTION.md` §9) and multiply; do not schedule from a guess.

If the measured cost makes 792 runs impossible on the available hardware, the
preregistered fallback, in this order:

1. Drop ablation A2 (−72 runs).
2. Drop `--repeat` to 2 for the four non-primary condition pairs only — never
   for a condition appearing in P1–P4, since unequal *k* across a compared pair
   makes pass@k incomparable.
3. Reduce `--max-iterations` from 4 to 3, **uniformly across all conditions**,
   and record it. Budget parity across conditions is not negotiable
   (`docs/METHODOLOGY.md` §4).

Dropping bugs from the 24-bug sample is **not** on this list. The sample is
stratified and n = 24 is already the binding constraint on power.

---

## 6. Commands

```bash
# 1. preflight: the endpoint answers before a multi-day sweep starts
python3 scripts/check_llm_endpoint.py

# 2. pilot, on the excluded bootstrap bug -- not sample data
python3 examples/repair_experiment.py --out results/pilot 115575 \
    --condition baseline raw-plain generic-plain llvmreduce-plain iraware-plain \
                raw-structured generic-structured llvmreduce-structured \
                iraware-structured

# 3. the sweep (bug-major, resumable -- rerun the same line after any interruption)
python3 examples/repair_experiment.py --sample --repeat 3 --out results/ \
    --condition baseline raw-plain generic-plain llvmreduce-plain iraware-plain \
                raw-structured generic-structured llvmreduce-structured \
                iraware-structured

# 4. the ablation
python3 examples/repair_experiment.py --sample --repeat 3 --out results/ \
    --no-promotion --condition iraware-plain iraware-structured

# 5. read it
python3 examples/summarize_results.py results/
python3 examples/analyze_significance.py results/
python3 examples/analyze_significance.py results/ --ablation
```

Every cell writes its own file and is skipped if that file exists, so an
interrupted sweep resumes by rerunning the identical command.

---

## 7. Amendments

*(none — append dated entries here if anything above has to change after the
sweep begins, with the reason, and demote the affected result to exploratory)*
