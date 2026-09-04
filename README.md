# better-compiler

Research code for the question in [`context.md`](context.md):

> Can LLVM-IR-aware semantic minimization and structured representation of
> Alive2 counterexamples improve LLM-based repair of real LLVM compiler bugs?

> **New to the project? Start with [docs/IMPLEMENTATION.md](docs/IMPLEMENTATION.md).**
> It explains the whole thing from scratch, assumes no compiler background, and
> includes a glossary, a worked example, and the current list of blockers.

The compiler repair loop already exists — [llvm-apr-benchmark](llvm-apr-benchmark/)
provides the bugs, the build/test harness and the Alive2 integration. This repo
adds the one stage that loop does not have: a layer between Alive2 and the LLM
that reduces and restructures the counterexample, plus the controlled
comparison needed to find out whether that helps.

```
        Alive2  ──►  raw counterexample  ──►  [ ce ]  ──►  LLM
                                               │
                                    reduction  ×  structure
```

## Layout

| path | what it is |
| --- | --- |
| [`ce/`](ce/) | the counterexample toolkit (see below) |
| [`examples/repair_experiment.py`](examples/repair_experiment.py) | the repair loop, parameterised by condition |
| [`examples/summarize_results.py`](examples/summarize_results.py) | aggregates run records into the experiment table |
| [`examples/analyze_significance.py`](examples/analyze_significance.py) | the preregistered McNemar test over those records |
| [`scripts/check_ir_roundtrip.py`](scripts/check_ir_roundtrip.py) | validates the IR model against every dataset reproducer |
| [`scripts/check_llm_endpoint.py`](scripts/check_llm_endpoint.py) | preflight for the model endpoint, before a multi-day sweep |
| [`scripts/power_analysis.py`](scripts/power_analysis.py) | the calculation *k* = 3 was chosen from |
| [`scripts/select_experiment_sample.py`](scripts/select_experiment_sample.py) | picks the stratified bug sample for the real sweep (Blocker 3) |
| [`tests/`](tests/) | unit tests plus `alive-tv` integration tests |
| [`data/samples/`](data/samples/) | real `alive-tv` outputs, used as parser fixtures |
| [`data/experiment_sample.json`](data/experiment_sample.json) | the picked 24-bug sample, stratified by complexity and component |
| [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md) | **start here** — full walkthrough, glossary, blockers |
| [`docs/METHODOLOGY.md`](docs/METHODOLOGY.md) | what the results do and do not support |
| [`docs/ANALYSIS_PLAN.md`](docs/ANALYSIS_PLAN.md) | preregistered *k* and statistical test, fixed before the sweep |
| [`docs/SLM_SELECTION.md`](docs/SLM_SELECTION.md) | which open model drives the sweep, and what the papers say |
| [`docs/RUNBOOK.md`](docs/RUNBOOK.md) | step-by-step: bare H100 server to results table |
| [`docs/RUNBOOK_NATIVE.md`](docs/RUNBOOK_NATIVE.md) | same, for a server where you can't start Docker |
| [`docs/OPERATING.md`](docs/OPERATING.md) | **day-to-day: start it, watch it, fix it when it breaks** |
| [`llvm-apr-benchmark/`](llvm-apr-benchmark/) | upstream benchmark, unmodified |

### The `ce` package

| module | responsibility |
| --- | --- |
| [`alive.py`](ce/alive.py) | run `alive-tv`; parse its output into a structured record |
| [`irmodel.py`](ce/irmodel.py) | edit-oriented model of textual LLVM IR: blocks, def-use, slicing, edits |
| [`oracle.py`](ce/oracle.py) | "is this still the same violation?", with strictness levels and cost accounting |
| [`reduce_generic.py`](ce/reduce_generic.py) | IR-blind line-level ddmin — the baseline that controls for prompt length |
| [`reduce_llvmreduce.py`](ce/reduce_llvmreduce.py) | second baseline: LLVM's own `llvm-reduce` — IR-valid, but counterexample-blind |
| [`reduce_iraware.py`](ce/reduce_iraware.py) | the proposed reducer: tandem, dependency-closed, counterexample-seeded |
| [`structured.py`](ce/structured.py) | the structured feedback rendering |
| [`feedback.py`](ce/feedback.py) | the experimental conditions; one call produces one LLM message |
| [`benchmark.py`](ce/benchmark.py) | drop-in for the benchmark's `normalize_feedback`, plus run records |
| [`cli.py`](ce/cli.py) | `check` / `reduce` / `feedback` / `compare` |

## Getting started

The container has `alive-tv` and the dataset already:

```bash
docker compose up -d
docker compose exec better-compiler python3 -m pytest tests -q
```

Reduce a counterexample and see the six conditions side by side:

```bash
docker compose exec better-compiler python3 -m ce.cli \
    compare data/samples/poison.src.ll data/samples/poison.tgt.ll
```

On the bundled sample (an `nsw` propagated without justification, buried in
28 instructions of unrelated arithmetic and control flow):

```
condition               prompt_tok  shown_in  shown_l  reduction_  oracle_c  seconds
raw-plain               378         28        40       -           -         -
generic-plain           375         28        38       0.000       183       3.503
iraware-plain           148         4         10       0.857       17        0.769
raw-structured          550         28        40       -           -         -
generic-structured      539         28        38       0.000       183       3.483
iraware-structured      353         4         10       0.857       17        0.696
```

The IR-aware reducer leaves a two-instruction function whose only difference
from its source is the offending flag. Line-level ddmin removes nothing
structural in ten times the verifier calls, because almost every candidate it
proposes is invalid IR — it has no way to know the two files are one function.

**Is that just because `generic` is a strawman?** Add `llvm-reduce` — a real,
IR-valid-by-construction reducer with zero counterexample awareness
(`ce/reduce_llvmreduce.py`, docs/IMPLEMENTATION.md Blocker 5):

```bash
docker compose exec better-compiler python3 -m ce.cli \
    compare data/samples/poison.src.ll data/samples/poison.tgt.ll \
    --conditions raw-plain generic-plain llvmreduce-plain iraware-plain
```

```
condition               prompt_tok  shown_in  shown_l  reduction_  oracle_c  seconds
raw-plain               378         28        40       -           -         -
generic-plain           375         28        38       0.000       183       4.744
llvmreduce-plain        173         5         13       0.821       351       32.545
iraware-plain           148         4         10       0.857       17        0.919
```

`llvmreduce` closes almost all of `generic`'s gap (0.821 vs 0.000 reduction —
IR-validity alone buys a lot), which is the honest answer to "wouldn't any
real reducer have done this?" But `iraware` still reaches a smaller result
using **20x fewer oracle calls**, showing counterexample-awareness adds real
value beyond IR-validity, not just repeating what `llvmreduce` already shows.

**This is one hand-built example, not a result.** It shows the mechanism works;
it says nothing yet about repair rates on real bugs.

## Using it inside a repair loop

The integration point is one function. Wherever the loop pastes Alive2 output
into a prompt:

```python
from ce.benchmark import normalize_feedback

res, log = env.check_full()
if not res:
    feedback = normalize_feedback(log, condition="iraware-structured")
    messages.append({"role": "user", "content": feedback.text})
    run.record(Iteration(..., feedback=feedback.summary()))
```

`normalize_feedback` accepts the benchmark's `check_fast`/`check_full` result
directly and passes non-Alive2 failures (build errors, crashes, lit failures)
through unchanged, so a loop can route all its feedback through it.

## Running the experiment

Needs `opt` built for each bug's `base_commit` and a model endpoint. The
runner is an OpenAI-compatible client, so a local vLLM server is a drop-in.
See [`docs/SLM_SELECTION.md`](docs/SLM_SELECTION.md) for which model and how
to serve it.

```bash
export LAB_LLM_URL=http://<h100-host>:8000/v1   # LAB_LLVM_* are set in the image
export LAB_LLM_TOKEN=... LAB_LLM_MODEL=... LAB_LLM_TEMP=0.8

python3 scripts/check_llm_endpoint.py           # preflight, seconds not hours

python3 examples/repair_experiment.py --sample --repeat 3 --out results/ \
    --condition baseline raw-plain generic-plain llvmreduce-plain iraware-plain \
                raw-structured generic-structured llvmreduce-structured \
                iraware-structured

python3 examples/repair_experiment.py --sample --repeat 3 --out results/ \
    --no-promotion --condition iraware-plain iraware-structured

python3 examples/summarize_results.py results/
python3 examples/analyze_significance.py results/
```

Passing every condition to one invocation runs the sweep **bug-major**: all
conditions and trials for one bug before the next. That is not cosmetic. The
conditions of a bug share a `base_commit`, so only the patched translation unit
gets rebuilt, while moving to the next bug is a near-full rebuild.
Condition-major would pay that switch 216*k times instead of 24.

Every cell writes its own file and is skipped if it exists, so an interrupted
sweep resumes by rerunning the identical command.

Every condition gets the same iteration and oracle budget; both are recorded
per run so the comparison can be checked rather than assumed. k = 3 and the
significance test are fixed in advance in
[`docs/ANALYSIS_PLAN.md`](docs/ANALYSIS_PLAN.md).

## Status

Built and tested: the parser, IR model, both reducers, the oracle, the
structured renderer, the condition matrix, the CLI, and the benchmark adapter.
The IR model round-trips all 1462 dataset reproducers byte-exactly.

Decided before running, so the analysis cannot be fitted to the results:
k = 3 (pass@3) and McNemar's exact test over four preregistered comparisons.
See [`docs/ANALYSIS_PLAN.md`](docs/ANALYSIS_PLAN.md), with the power
calculation in `scripts/power_analysis.py`. The model is chosen in
[`docs/SLM_SELECTION.md`](docs/SLM_SELECTION.md).

Not yet run: the end-to-end repair experiment. `opt` is now built for all 24
sample bugs (on one machine only: the build volumes do not transfer), so what
is left is a model endpoint. Until the sweep runs there are no repair-rate
numbers — only the mechanism.

`feat/e2e-bootstrap` adds the scripts to actually attempt this for one bug —
`scripts/bootstrap_first_repair.py` (build `opt`, confirm the bug reproduces,
optionally run one real repair) and `scripts/select_bootstrap_bug.py` (how the
bootstrap bug was picked). **As of 2026-08-27, phase 1 has actually been run**:
`opt` built successfully for bug `115575` in ~1h53m and the bug reproduces as
expected — `/workspace/llvm-build` is no longer empty. See
`docs/IMPLEMENTATION.md`'s Blocker 1 for the full transcript. Phase 2 (an
actual LLM repair attempt) still needs `LAB_LLM_TOKEN` and hasn't run yet.

Read [`docs/METHODOLOGY.md`](docs/METHODOLOGY.md) before writing any of this up.
