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
| [`scripts/check_ir_roundtrip.py`](scripts/check_ir_roundtrip.py) | validates the IR model against every dataset reproducer |
| [`tests/`](tests/) | unit tests plus `alive-tv` integration tests |
| [`data/samples/`](data/samples/) | real `alive-tv` outputs, used as parser fixtures |
| [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md) | **start here** — full walkthrough, glossary, blockers |
| [`docs/METHODOLOGY.md`](docs/METHODOLOGY.md) | what the results do and do not support |
| [`llvm-apr-benchmark/`](llvm-apr-benchmark/) | upstream benchmark, unmodified |

### The `ce` package

| module | responsibility |
| --- | --- |
| [`alive.py`](ce/alive.py) | run `alive-tv`; parse its output into a structured record |
| [`irmodel.py`](ce/irmodel.py) | edit-oriented model of textual LLVM IR: blocks, def-use, slicing, edits |
| [`oracle.py`](ce/oracle.py) | "is this still the same violation?", with strictness levels and cost accounting |
| [`reduce_generic.py`](ce/reduce_generic.py) | IR-blind line-level ddmin — the baseline that controls for prompt length |
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

Needs `opt` built for each bug's `base_commit` and an LLM API key:

```bash
export LAB_LLM_TOKEN=...            # LAB_LLVM_* are already set in the image
for c in raw-plain generic-plain iraware-plain \
         raw-structured generic-structured iraware-structured; do
    python3 examples/repair_experiment.py --condition "$c" --all --out results/
done
python3 examples/summarize_results.py results/
```

Every condition gets the same iteration and oracle budget; both are recorded
per run so the comparison can be checked rather than assumed.

## Status

Built and tested: the parser, IR model, both reducers, the oracle, the
structured renderer, the condition matrix, the CLI, and the benchmark adapter.
The IR model round-trips all 1462 dataset reproducers byte-exactly.

Not yet run: the end-to-end repair experiment. It needs a built `opt` per
`base_commit`, which is hours of compute per commit and has not been done yet
(`/workspace/llvm-build` is empty). Until it runs, there are no repair-rate
numbers — only the mechanism.

`feat/e2e-bootstrap` adds the scripts to actually attempt this for one bug —
`scripts/bootstrap_first_repair.py` (build `opt`, confirm the bug reproduces,
optionally run one real repair) and `scripts/select_bootstrap_bug.py` (how the
bootstrap bug was picked). They still need to be run inside the real container
to mean anything; see `docs/IMPLEMENTATION.md`'s Blocker 1.

Read [`docs/METHODOLOGY.md`](docs/METHODOLOGY.md) before writing any of this up.
