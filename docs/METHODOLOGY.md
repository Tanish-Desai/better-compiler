# Methodology

This document records what the `ce` package actually does, what it assumes,
and what its outputs can and cannot support as a claim. It is the companion to
`context.md`, which states the research intent; this one states the
implementation's commitments.

## 1. What is being varied

The repair loop is held fixed and exactly one thing is varied: the text handed
to the model after a failed verification. `examples/repair_experiment.py` is
`llvm-apr-benchmark/examples/baseline.py` with the feedback call swapped, plus
the three harness changes in the next paragraph.

**The "nothing else changed" claim no longer holds, and saying so is part of
the method.** Upstream's `baseline.py` was written for `deepseek-reasoner`. Run
against `Qwen2.5-Coder-14B-Instruct`, its prompt produced no repair attempt at
all on 64.5% of turns: the model handed the source window straight back
(`docs/IMPLEMENTATION.md` Blocker 16). Three changes were required before the
loop attempts the task, and all three are applied **identically under every
condition**, so they change the task's difficulty without touching the factor
being varied:

1. **A no-op guard.** A reply identical to the window is rejected before
   building, and the model is told it returned the code unchanged. Previously
   such a reply applied cleanly and the turn spent several minutes confirming
   that unmodified source does not fix the bug.
2. **Brace-balanced windows.** `bug_hunk`'s fixed ±30-line margin cut wherever
   it landed, routinely handing the model an excerpt that began mid-statement
   and ended on an unterminated brace. The window now grows until it opens
   everything it closes and closes everything it opens.
3. **A rebalanced format instruction.** Upstream's wording gives three
   directives about reproducing the window faithfully against one about fixing
   it; an explicit "your answer must differ from the code you were given" was
   added.

None of these can favour one condition over another — they are upstream of the
feedback text entirely. But they do mean results are not comparable with the
pre-Blocker-16 sweep, and that sweep's records are kept only as a measurement
of the reducers, never of repair rates.

Two orthogonal factors:

| factor | levels |
| --- | --- |
| reduction | `raw`, `generic`, `llvmreduce`, `iraware` |
| structure | `plain`, `structured` |

giving the 2x3 matrix in `context.md` §16 (`raw`/`generic`/`iraware`), plus a
`baseline` condition that receives no counterexample at all, plus two more
cells — `llvmreduce-plain`/`llvmreduce-structured` — added afterward for
Blocker 5 (`docs/IMPLEMENTATION.md` §9) and not part of either of
`context.md`'s original letter schemes; refer to them only by full name.
`llvmreduce` is a second reduction-only baseline, not a claim about
structure, so the headline Blocker-5 comparison holds structure fixed (at
`plain`) and reads `generic` vs `llvmreduce` vs `iraware`.

`context.md` letters the conditions twice, and the two letterings disagree
(§15 prose vs §16 table). The code implements the §16 factorial, exposes those
letters as `MATRIX_LETTERS`, and keeps the §15 ordering as `LEGACY_LETTERS`.
**Always report full condition names, never bare letters.**

## 2. The semantic-preservation oracle

A reduction is accepted only if `alive-tv` still reports the same refinement
violation. This is the property that distinguishes this work from text
shrinking, and it is a *knob*, not a constant:

| strictness | requires |
| --- | --- |
| `any_failure` | some refinement failure still occurs |
| `error_class` (default) | the same alive2 error class, in the same function |
| `error_class_and_kind` | additionally, the same target-side outcome kind (poison / undef / UB / wrong value) |

Report the level used. A result obtained under `any_failure` is materially
weaker than the same result under `error_class_and_kind`, because the reducer
is permitted to drift onto a different bug in the same function.

## 3. What the IR-aware reducer does

Passes, applied in this order and repeated to a fixpoint (`ce/reduce_iraware.py`):

| pass | effect |
| --- | --- |
| `dce` | delete values nothing reads |
| `fold-branches` | replace a conditional branch with the edge the counterexample took |
| `prune-blocks` | delete blocks unreachable from entry |
| `simplify-cfg` | collapse trivial phis and straight-line block chains |
| `slice` | pin the violation slice, ddmin over the remainder |
| `promote-operands` | replace a computed value with a fresh parameter, then DCE its producers |
| `drop-params` | remove parameters unused on both sides |
| `strip-flags` | ddmin over `nsw`/`nuw`/`inbounds`/… to find a minimal failing set |

Three design commitments distinguish this from generic reduction:

1. **Tandem editing.** Source and target are two versions of one function, so
   every edit is applied to both, keyed by SSA name. A line-level reducer
   cannot express this constraint.
2. **Dependency-closed candidates.** The reducer picks values to *keep* and
   closes that set over def-use edges and control dependence, so candidates are
   well-formed IR by construction rather than by luck. This is what makes the
   search affordable in verifier calls.
3. **Counterexample-seeded search.** The alive2 trace supplies the diverging
   values (slice seeds), the executed blocks (branch-folding direction) and
   every value's type (promotion). None of that is rediscovered by trial.

## 3b. What the `llvm-reduce` baseline does

Added for Blocker 5 (`docs/IMPLEMENTATION.md` §9); numbered "3b" rather than
renumbering §4 onward, since those are referenced by number elsewhere
(e.g. `ce/oracle.py`).

`ce/reduce_llvmreduce.py` runs LLVM's own `llvm-reduce` twice — once per side
— rather than modifying it or teaching it about the src/tgt pairing:

1. reduce `src`, holding `tgt` fixed at its **original** text
2. reduce `tgt`, holding `src` fixed at the **already-reduced** result of
   step 1 (chaining: a smaller `src` can permit removing more of `tgt`)
3. re-verify the pair **together** before accepting it, since steps 1-2 only
   ever checked one side against a fixed partner

Each step is `llvm-reduce`'s own delta-debugging over IR-valid candidates
(functions, blocks, instructions, operands, attributes, flags — its built-in
passes, unmodified), driven by an opaque interestingness test
(`ce/_llvmreduce_test.py`) that runs in a **separate subprocess per
candidate**. `llvm-reduce` never receives more than that test's exit code —
it has no access to the oracle, the violation, or the other file. That
opacity is deliberate: it is what makes this baseline "IR-aware, not
counterexample-aware" rather than a weaker version of `iraware`.

Because those subprocesses call alive-tv independently, their tallies don't
go through the calling `Oracle` directly; `Oracle.record_external()` merges
them in afterward so `.stats()` still reports the true total cost.

## 4. Threats to validity this implementation creates

Beyond those in `context.md` §28:

**Operand promotion generalises the program.** A fresh parameter ranges over
all values of its type, so the reduced witness may describe an execution not
reachable in the original reproducer. The violation is preserved — the oracle
guarantees that — but the *relevance to the original bug* is weakened. Run with
`--no-promotion` as an ablation and report both. `test_reduction_without_promotion_is_more_conservative`
pins the expected relationship.

**The IR model is best-effort.** `ce/irmodel.py` is line-oriented, not a real
LLVM parser. Its safety property is that anything it does not understand
round-trips verbatim; this is checked against all 1462 reproducers in the
dataset (`scripts/check_ir_roundtrip.py`, currently 1462/1462). A model error
produces IR that alive2 rejects, which the oracle discards — it degrades the
reduction, it does not corrupt a result.

**Budget confounding.** Reduction spends verifier calls. Every condition must
get the same LLM iteration budget (`--max-iterations`) and the same oracle
budget (`--oracle-budget`), and both are recorded in each run's `notes`.

**Scope: miscompilations only.** Only miscompilation bugs produce Alive2
counterexamples. The runner skips crash and hang bugs under counterexample
conditions rather than comparing identical prompts across conditions and
diluting the measured effect. Repair rates are therefore over the
miscompilation subset, and must be reported as such — not as a rate over the
whole benchmark.

**Prompt length is a confound, and that is what `generic` is for.** If
`iraware` beats `raw` by the same margin `generic` does, the effect is
compression, not semantics (`context.md` §29). The interesting comparison is
`iraware` vs `generic` at comparable size.

## 5. Reporting

Primary metric: correct repair rate under the benchmark's own criterion
(`env.check_full()` — reproducer plus lit regression suite). A patch that
merely compiles, or that only fixes the reproducer, does not count; this is
enforced by the benchmark, not by this code.

Secondary, and **not all equally weighted as efficiency claims**
(`context.md` RQ4/H5, decided 2026-08-27 — see `docs/IMPLEMENTATION.md`
Blocker 2): one repair iteration is one LLVM rebuild, minutes of wall time, so
a difference of a few hundred prompt tokens or a few seconds is noise next to
that. The efficiency claim is **iterations to fix** (and the build/oracle-call
counts that scale with it) — report those as the "condition X is more
efficient" number. Estimated prompt tokens, LLM tokens, reduction time, and
wall time are still recorded and worth showing, but as descriptive context
beside the repair-rate/iteration numbers, not as the headline efficiency
result. The size metrics before/after reduction are separate again — they
describe the reducer, not the repair loop's cost.

`examples/summarize_results.py` also prints a **paired** table restricted to
bugs attempted under every condition. Prefer it: an unpaired table can credit a
condition for having faced an easier subset.

**The inferential claims are preregistered.** `docs/ANALYSIS_PLAN.md`, dated and
committed while `results/` still held no run records, fixes the repeat count
(*k* = 3, outcome = pass@3), the test (McNemar's exact, one-sided, α = 0.05),
and the exact four comparisons that count as primary, corrected together with
Benjamini-Hochberg. `examples/analyze_significance.py` is the executable form of
that document. Two constraints follow from it and bind any write-up:

- The remaining 32 condition pairs are **exploratory**. Their p-values print
  under `--all-pairs`, uncorrected, and are not evidence.
- Power at n = 24 is roughly 0.33-0.57 even for a large effect. **A p above 0.05
  means this sample cannot resolve the question, not that there is no effect.**
  Report the discordant counts and the paired rate difference alongside every
  p-value, as `agentic_harness` does for the same bug family.

Note that `estimate_tokens` is a 4-chars-per-token approximation. It is
consistent across conditions, which is all a comparison needs, but do not
quote it as an absolute cost — substitute a real tokenizer first.

## 6. Reproducing the artifact-level results

```bash
docker compose up -d

# IR model against every real reproducer in the dataset
docker compose exec better-compiler python3 scripts/check_ir_roundtrip.py

# unit + integration suite (integration needs alive-tv, which the image has)
docker compose exec better-compiler python3 -m pytest tests -q

# the six-condition table for one counterexample pair
docker compose exec better-compiler \
  python3 -m ce.cli compare data/samples/poison.src.ll data/samples/poison.tgt.ll
```

The end-to-end repair experiment additionally needs `opt` built for each bug's
`base_commit` (hours per commit, cached by ccache) and an LLM API key. See
`README.md`.
