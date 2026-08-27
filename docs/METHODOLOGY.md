# Methodology

This document records what the `ce` package actually does, what it assumes,
and what its outputs can and cannot support as a claim. It is the companion to
`context.md`, which states the research intent; this one states the
implementation's commitments.

## 1. What is being varied

The repair loop is held fixed and exactly one thing is varied: the text handed
to the model after a failed verification. `examples/repair_experiment.py` is
`llvm-apr-benchmark/examples/baseline.py` with the feedback call swapped and
nothing else changed.

Two orthogonal factors:

| factor | levels |
| --- | --- |
| reduction | `raw`, `generic`, `iraware` |
| structure | `plain`, `structured` |

giving the 2x3 matrix in `context.md` §16, plus a `baseline` condition that
receives no counterexample at all.

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
