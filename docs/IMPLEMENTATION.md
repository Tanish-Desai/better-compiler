# Implementation Guide

**Audience:** our team. Assumes no compiler background. Read top to bottom.

**Last updated:** 2026-09-04 (model chosen, *k* and the statistical test preregistered — see Blockers 10 and 11)

---

## Table of Contents

1. [Current status — where we are right now](#1-current-status)
2. [What needs doing next](#2-what-needs-doing-next)
3. [Commands to run and verify](#3-commands-to-run-and-verify)
4. [The problem, in plain terms](#4-the-problem-in-plain-terms)
5. [What our research actually claims](#5-what-our-research-actually-claims)
6. [Glossary](#6-glossary)
7. [The system in one picture](#7-the-system-in-one-picture)
8. [A worked example](#8-a-worked-example)
9. [The codebase, file by file](#9-the-codebase-file-by-file)
10. [The experiment design](#10-the-experiment-design)
11. [Decisions made (log)](#11-decisions-made)
12. [Nice-to-haves](#12-nice-to-haves)

---

## 1. Current Status

### What works

- ✅ **The entire `ce/` toolkit** — parser, IR model, oracle, all 3 reducers (generic, llvm-reduce, IR-aware), structured renderer, feedback grid, CLI
- ✅ **63 tests passing** — unit tests (no Alive2 needed) + integration tests (need Alive2, which the Docker image has)
- ✅ **IR model validated** against all 1,462 real reproducers in the dataset (byte-exact round-trip)
- ✅ **`opt` built for all 24 sample bugs**, plus `115575` (the bootstrap bug, excluded from the sample) — every bug confirmed to reproduce at its `base_commit`. Done on this machine at `--build-jobs 4` (see Blocker 9); per-bug logs in `results/build_logs/` (gitignored — regenerate, don't rely on this history persisting)
- ✅ **24-bug sample selected** and committed at `data/experiment_sample.json`
- ✅ **Experiment runner written** (`examples/repair_experiment.py`) — ready to go
- ✅ **Result aggregator written** (`examples/summarize_results.py`) — reads run records into a comparison table
- ✅ **Scripts** — `scripts/check_ir_roundtrip.py` (IR safety check), `scripts/bootstrap_first_repair.py` (build `opt` + reproduce a bug), `scripts/select_experiment_sample.py` (picked the 24-bug sample), `scripts/smoke_reduce_dataset.py` (stress-tests the shrinker on real dataset IR), `scripts/select_bootstrap_bug.py` (picked the bootstrap candidate)
- ✅ **Tests** — 4 test files in `tests/`: `test_alive.py` (parser), `test_irmodel.py` (round-trip, def-use, slicing), `test_reduction.py` (ddmin, oracle rules, structured output), `test_integration.py` (end-to-end shrinking with real Alive2 + llvm-reduce)
- ✅ **llvm-apr-benchmark/** — the upstream benchmark repo, checked out and **unmodified**. Provides the 491 real LLVM bugs, the build/test harness (`lab_env.py`, `llvm_helper.py`), and the `baseline.py` repair loop we forked
- ✅ **All 8 blockers resolved** (decisions documented in [§11](#11-decisions-made))

### What doesn't work yet

- ❌ **No repair-rate numbers exist.** The AI experiment has never run. It needs a model endpoint (`LAB_LLM_URL` + `LAB_LLM_TOKEN`).
- ❌ **`results/` holds no run records.** Only `results/build_logs/`, which are `opt` build transcripts.
- ✅ **Repeat count decided: *k* = 3** (Blocker 10, [`ANALYSIS_PLAN.md`](ANALYSIS_PLAN.md)).
- ✅ **Statistical test decided: McNemar's exact**, one-sided, Benjamini-Hochberg over four preregistered comparisons (Blocker 10).
- ✅ **Model chosen:** `Qwen3-Coder-30B-A3B-Instruct` on the H100, served by vLLM (Blocker 11, [`SLM_SELECTION.md`](SLM_SELECTION.md)).

### The one result we have (mechanism demo, not science)

On the bundled sample (`data/samples/poison.src.ll` / `poison.tgt.ll`):

| condition | instructions after | reduction | oracle calls | seconds |
|---|--:|--:|--:|--:|
| `raw` | 28 | — | — | — |
| `generic` | 28 | 0.000 | 183 | ~4.7 |
| `llvmreduce` | 5 | 0.821 | 351 | ~32.5 |
| `iraware` | 4 | 0.857 | 17 | ~0.9 |

This shows the mechanism works. It says nothing about whether the AI fixes more bugs.

---

## 2. What Needs Doing Next

### Critical (blocking the experiment)

#### Step 1: Stand up the model endpoint and run the nine-run pilot

Serve the model on the H100 ([`SLM_SELECTION.md`](SLM_SELECTION.md) §8), point the runner at it, and check it answers before committing days of rebuilds:

```bash
export LAB_LLM_URL=http://<h100-host>:8000/v1
export LAB_LLM_TOKEN=local-sweep
export LAB_LLM_MODEL=qwen3-coder-30b
export LAB_LLM_TEMP=0.8            # must be > 0, or k = 3 buys three copies

python3 scripts/check_llm_endpoint.py --repeat 3
```

Then the pilot, on bug `115575` — build-verified, and deliberately **excluded** from the 24-bug sample, so it costs no sample data:

```bash
python3 examples/repair_experiment.py --out results/pilot 115575 \
    --condition baseline raw-plain generic-plain llvmreduce-plain iraware-plain \
                raw-structured generic-structured llvmreduce-structured \
                iraware-structured
```

This validates the full pipeline: LLM → patch → LLVM build → lit → Alive2 → reduced feedback → retry. Read four things off it: does the loop complete; how often `apply_patch` fails (LLVM-Bench found patch invalidity to be a dominant failure mode); **minutes per iteration**, which is the only way to schedule the sweep honestly; and whether the model engages with the counterexample at all.

#### Step 2: Decide repeat count (*k*) — ✅ DECIDED (2026-09-04): ***k* = 3**

*k* does not add statistical units — pass@k still yields one paired binary per bug, so n stays 24. What it buys is a less noisy outcome per cell. `scripts/power_analysis.py` simulates the difference: power roughly triples from *k* = 1 to *k* = 3, and *k* = 5 adds little (and in the highest-rate scenario *falls*, as pass@5 pushes both conditions toward a ceiling where nothing is discordant).

| per-attempt rate, better vs worse | k=1 | k=3 | k=5 |
|---|--:|--:|--:|
| 0.20 vs 0.10 | 0.11 | 0.33 | 0.43 |
| 0.25 vs 0.10 | 0.23 | 0.57 | 0.67 |
| 0.35 vs 0.20 | 0.20 | 0.36 | 0.33 |

Full reasoning and the fallbacks if compute runs out: [`ANALYSIS_PLAN.md`](ANALYSIS_PLAN.md) §2 and §5.

#### Step 3: Decide the statistical test — ✅ DECIDED (2026-09-04): **McNemar's exact**

One-sided at α = 0.05, Benjamini-Hochberg across four preregistered comparisons, implemented in `examples/analyze_significance.py`.

Not Fisher's exact: the nine conditions run over the *same* 24 bugs, so the data are paired, and Fisher would discard the pairing that makes n = 24 workable at all. Every paper in `slm_research_papers/` testing paired binary repair outcomes uses McNemar — including `agentic_harness`, the closest existing work on this exact bug family. See [`ANALYSIS_PLAN.md`](ANALYSIS_PLAN.md) §3 and [`SLM_SELECTION.md`](SLM_SELECTION.md) §6.

#### Step 4: Build `opt` for the rest of the sample — ✅ DONE (2026-09-03)

All 24 sample bugs (plus `115575`) now have a build-verified `opt` on this
machine. See Blocker 9 for how, and the `--build-jobs` gotcha to avoid
repeating this. The experiment runner still rebuilds `opt` per bug on
whatever machine it runs on — this step doesn't carry across machines
(each teammate's `llvm-build`/`ccache` volumes are local), so re-run it
first on any new machine:

```bash
# The experiment runner handles the build automatically per bug,
# but each first build is ~2 hours (less with warm ccache for nearby
# commits). Pass --build-jobs 4 (or lower) under the default 10g
# container memory cap — see Blocker 9.
```

#### Step 5: Run the full sweep

```bash
python3 examples/repair_experiment.py --sample --repeat 3 --out results/ \
    --condition baseline raw-plain generic-plain llvmreduce-plain iraware-plain \
                raw-structured generic-structured llvmreduce-structured \
                iraware-structured
```

`--sample` reads the 24 bug ids from `data/experiment_sample.json`, so the sweep and the committed sample can never drift apart.

Passing all nine conditions to **one** invocation is what makes this affordable. The runner then iterates **bug-major** — every condition and trial for one bug before moving to the next — because those share a `base_commit` and only need the patched translation unit rebuilt, whereas moving to the next bug is a near-full rebuild (Blocker 9 measured 18 min to 2h49m each). The old condition-major loop paid that switch 216·*k* times instead of 24.

Before starting, raise `ccache -M` to 250G. It is at 40G holding 24 different `base_commit`s and has already run 44 cleanups — it is evicting entries it is about to need again, which is the single largest avoidable cost in this sweep.

Every cell writes its own file and is skipped if that file exists, so an interrupted sweep resumes by rerunning the identical command.

#### Step 6: Run the `--no-promotion` ablation

Tests whether `promote-operands` — which generalizes the program, weakening the witness's relevance to the original bug (METHODOLOGY.md §4) — is carrying the result:

```bash
python3 examples/repair_experiment.py --sample --repeat 3 --out results/ \
    --no-promotion --condition iraware-plain iraware-structured
```

The filenames won't collide (Blocker 7 for the promotion axis, Blocker 10 for the trial index).

#### Step 7: Read the results

```bash
python3 examples/summarize_results.py results/     # rates and costs, all + paired
python3 examples/analyze_significance.py results/  # the four preregistered tests
python3 examples/analyze_significance.py results/ --ablation
```

Report the discordant counts and rate differences whatever the p-values say — at n = 24 the counts carry more information than the p-value does ([`ANALYSIS_PLAN.md`](ANALYSIS_PLAN.md) §2).

---

## 3. Commands to Run and Verify

### Start the container

```bash
docker compose up -d
```

Code is live-mounted — edits on your machine take effect immediately.

### Run the test suite

```bash
docker compose exec better-compiler python3 -m pytest tests -q
# Expected: 63 passed (the integration tests need alive-tv, which the image has)
```

### Run the IR round-trip safety check

```bash
docker compose exec better-compiler python3 scripts/check_ir_roundtrip.py
# Expected: 1462 ok, 0 mismatched, 0 crashed
```

### See all conditions side by side (quick demo)

```bash
docker compose exec better-compiler python3 -m ce.cli \
    compare data/samples/poison.src.ll data/samples/poison.tgt.ll
```

### See what the AI would receive under a specific condition

```bash
docker compose exec better-compiler python3 -m ce.cli \
    feedback data/samples/poison.src.ll data/samples/poison.tgt.ll \
    --condition iraware-structured
```

### Shrink a counterexample (verbose)

```bash
docker compose exec better-compiler python3 -m ce.cli \
    reduce data/samples/poison.src.ll data/samples/poison.tgt.ll --strategy iraware
```

### Include `llvm-reduce` in the comparison

```bash
docker compose exec better-compiler python3 -m ce.cli \
    compare data/samples/poison.src.ll data/samples/poison.tgt.ll \
    --conditions raw-plain generic-plain llvmreduce-plain iraware-plain
```

---

## 4. The Problem, in Plain Terms

A **compiler** turns source code into machine code. On the way it **optimizes** — rewrites your code to be faster while (supposedly) keeping the behaviour identical.

Sometimes it gets that wrong. The rewritten code is faster *and different*. Your program now does the wrong thing, and nothing warned you. This is called a **miscompilation**.

LLVM is one of the most widely used compilers in the world (Clang, Rust, Swift all build on it). It has these bugs. Fixing them is slow, expert work.

There is a tool called **Alive2** that can *mathematically prove* a specific optimization is wrong. When it finds a problem it prints a **counterexample** — a specific input where the before-code and after-code disagree.

So a natural idea: give an AI the buggy compiler code plus Alive2's counterexample, and ask it to fix the bug. **People are already doing this.** That is the `llvm-autofix` project.

---

## 5. What Our Research Actually Claims

**We are NOT claiming any of these:**

| ❌ Not our claim | Who already did it |
|---|---|
| "AI can fix LLVM bugs" | `llvm-autofix` |
| "Shrinking failing inputs helps AI repair" | `ReduceFix` |
| "Minimal feedback helps AI" | `PGS` |

**What we are actually asking:**

> Alive2's counterexample is cluttered. If we clean it up **using knowledge of how LLVM code works**, does the AI fix more bugs than if we clean it up with a generic, compiler-unaware method?

Two separate ideas, tested independently:

- **Idea 1 — shrink it.** Less clutter, less for the model to wade through.
- **Idea 2 — organise it.** Same information, laid out in labelled sections.

The experiment grid separates them (see [§10](#10-the-experiment-design)).

---

## 6. Glossary

### Compiler terms

| Term | Meaning |
|---|---|
| **LLVM IR** | The intermediate language LLVM optimizes. Lower-level than C, higher-level than assembly. |
| **`opt`** | LLVM's command-line optimizer. Feed it IR, name a pass, get optimized IR out. |
| **pass** | One optimization. `InstCombine`, `LoopVectorize`, `SLPVectorizer` show up most in our dataset. |
| **middle-end** | The optimizer part of a compiler — the bit between the language frontend and machine-code generation. |

### LLVM IR structure

| Term | Meaning |
|---|---|
| **SSA** | Every named value is assigned **exactly once**. `%a` refers to one specific computation, forever. |
| **basic block** | A straight run of instructions with a label, ending in exactly one jump/return. |
| **terminator** | The last instruction of a block — `ret`, `br`, `switch`. |
| **def-use** | "def" = the instruction that creates a value. "use" = an instruction that reads it. |
| **phi node** | At a point where two paths merge, `phi` picks a value based on which block you came from. |
| **flags** (`nsw`, `nuw`, `inbounds`, `exact`) | Promises attached to an instruction. `nsw` = "this addition never overflows". Attaching one that isn't justified is a common bug. |

### Correctness concepts

| Term | Meaning |
|---|---|
| **poison** | LLVM's marker for "this value is garbage because a promise was broken". Spreads to anything computed from it. |
| **UB** | Undefined behaviour. An optimization may never *introduce* UB. |
| **refinement** | The rule an optimization must obey: the new code must behave the same, or be *more* defined — never less. |
| **counterexample** | A concrete input where before-code and after-code disagree. Alive2's output when it finds a bug. |
| **src / tgt** | "source" and "target" — the code before and after the optimization. |

### Our terms

| Term | Meaning |
|---|---|
| **oracle** | The referee. After every edit, the oracle re-runs Alive2 and answers "is this still the same bug?" |
| **ddmin** | The classic shrinking algorithm. Removes big chunks first, then narrows down. |
| **slice** | The set of instructions a particular value depends on. Everything outside the slice is probably irrelevant. |
| **condition** | One cell of our experiment grid, e.g. `iraware-structured`. |

---

## 7. The System in One Picture

```
   ┌───────────────────────────────────────────────────────────────┐
   │  llvm-apr-benchmark  (already existed, we did not write it)   │
   │                                                               │
   │   real LLVM bug  →  AI writes patch  →  build LLVM  →  test   │
   │                          ↑                              │     │
   └──────────────────────────│──────────────────────────────│─────┘
                              │                              │
                     the message we send            test failed, and it
                     back to the AI                 was a miscompilation
                              │                              │
                              │                              ▼
                              │                        ┌───────────┐
                              │                        │  Alive2   │
                              │                        └─────┬─────┘
                              │                              │
                              │                    raw counterexample
                              │                    (cluttered, verbose)
                              │                              │
   ┌──────────────────────────│──────────────────────────────│─────┐
   │  ce/  ← EVERYTHING WE BUILT LIVES HERE                  ▼     │
   │                                                               │
   │   ┌───────────────────────┐      ┌──────────────────────┐     │
   │   │  KNOB 1: shrink it    │      │ KNOB 2: organise it  │     │
   │   │                       │      │                      │     │
   │   │  raw       (no change)│ then │  plain  (as-is)      │     │
   │   │  generic   (text)     │  ──► │  structured (labels) │     │
   │   │  llvmreduce(IR-valid, │      │                      │     │
   │   │             blind)    │      │                      │     │
   │   │  iraware   (smart)    │      │                      │     │
   │   └───────────────────────┘      └──────────────────────┘     │
   │                                                               │
   │        every edit checked by the ORACLE: still same bug?      │
   └───────────────────────────────────────────────────────────────┘
```

**The key point:** we did not build a repair agent. One already exists. We built the box in the middle and a way to A/B test what comes out of it.

---

## 8. A Worked Example

### Step 1 — Alive2 finds a bug

Two versions of the same function. Before and after an optimization. Alive2 says:

```
ERROR: Target is more poisonous than source

Example:
i8 %x = #x7e (126)
...
Source:  i8 %out = #x82 (130, -126)
Target:  i8 %out = poison
```

Translation: *with x=126, the original code computes 130. The "optimized" code produces poison — garbage.*

### Step 2 — Our shrinker goes to work

It notices most of the 28 instructions have nothing to do with why `%out` became poison. It deletes them — **checking with Alive2 after every single deletion** — and gets:

```llvm
; source
%out = add i8 %ce.arg5, %ce.arg0

; target
%out = add nsw i8 %ce.arg5, %ce.arg0
```

**28 instructions → 4.** The entire bug is one visible difference: the optimizer attached `nsw` without justification.

### Step 3 — We format it for the AI

```
VIOLATED PROPERTY:
    poison refinement

WHAT THE TRANSFORMATION CHANGED:
  modified: %out
      source: %out = add i8 %ce.arg5, %ce.arg0
      target: %out = add nsw i8 %ce.arg5, %ce.arg0

INTERPRETATION:
    A transformation may only make a program *less* poisonous...
    The target attaches 'nsw' to %out, which the source does not.
```

### Step 4 — Compare against baselines

| | instructions after | Alive2 calls | reduction |
|---|--:|--:|--:|
| **generic (text)** | 28 | 183 | 0.000 |
| **llvmreduce (IR-valid)** | 5 | 351 | 0.821 |
| **iraware (smart)** | 4 | 17 | 0.857 |

Generic deletes lines and hopes — most become invalid IR. llvm-reduce does much better (it knows IR structure) but still uses 20× the oracle calls because it doesn't know about the counterexample. iraware uses the counterexample to guide its search.

> ⚠️ **This is one hand-built example.** It shows the mechanism works. It says nothing about whether the AI actually fixes more bugs.

---

## 9. The Codebase, File by File

```
better-compiler/
├── ce/                    ← the library (everything we built)
├── tests/                 ← 63 tests
├── scripts/               ← build/validation/selection scripts
├── examples/              ← the experiment runner + results aggregator
├── data/                  ← experiment sample + test fixtures
├── docs/                  ← this file + METHODOLOGY.md
├── context.md             ← research framing (research questions, hypotheses, related work)
└── llvm-apr-benchmark/    ← upstream, untouched
```

### The library: `ce/`

Files in dependency order — each builds on the ones above.

---

#### [`ce/alive.py`](../ce/alive.py) — talk to Alive2

Runs `alive-tv` and turns its text output into Python objects.

| Thing | What it is |
|---|---|
| `run_alive_tv(src, tgt)` | Run the verifier, get a parsed result |
| `parse_alive_output(text)` | Parse text we already have |
| `AliveRun` | The whole run: verified or not, how many failures |
| `FunctionResult` | One function's verdict: error class, counterexample, traces |
| `Assignment` | One `type %name = value` line |

**Why it's tricky:** Alive2's format isn't documented — it's just what the tool prints. So parsing is best-effort, and anything unrecognised is kept as raw text rather than crashing.

---

#### [`ce/irmodel.py`](../ce/irmodel.py) — read and edit LLVM IR

The foundation of everything "IR-aware". Reads `.ll` text into functions → blocks → instructions.

| Thing | What it does |
|---|---|
| `parse_module(text)` | Text → objects |
| `Module.text()` | Objects → text (must be byte-identical) |
| `backward_slice(fn, values)` | What do these values depend on? |
| `dead_names(fn)` | Which values does nothing read? |
| `remove_instructions`, `substitute`, `drop_params`, `remove_blocks` | The edits |
| `measure(text)` | Size: lines, instructions, blocks, values |

**Safety rule:** anything we don't understand must survive parse-then-print **byte for byte**. Verified against all 1,462 real reproducers.

`backward_slice` also follows *control* dependence — if you need a value, you also need the branch condition that got you to it.

---

#### [`ce/oracle.py`](../ce/oracle.py) — the referee ⭐

**The most important file.** Every proposed edit goes through:

```
propose edit → re-run Alive2 → still the same bug?
                                 yes → keep it
                                 no  → throw it away
```

Three definitions of "the same bug":

| Setting | Requires |
|---|---|
| `any_failure` | Some failure still happens (weakest — can drift) |
| `error_class` ← default | Same error type, same function |
| `error_class_and_kind` | Also same failure kind (poison stays poison, UB stays UB) |

Counts its own cost (`.calls`, `.seconds`) — that's experiment data.

---

#### [`ce/reduction.py`](../ce/reduction.py) — shared machinery

`Reduction` (the report card both shrinkers fill in) and `ddmin` (the shrinking algorithm). Both shrinkers use ddmin but over different items:

- generic → over **lines of text**
- iraware → over **instructions** and **flags**

---

#### [`ce/reduce_generic.py`](../ce/reduce_generic.py) — the dumb shrinker (control group)

~70 lines. Treats the two files as plain text, ddmin over the lines. Knows nothing about LLVM.

**Why build something bad on purpose?** Without it, if our smart shrinker helps, a reviewer will ask: *"Is that because your shrinking is clever, or just because shorter prompts help?"* This baseline answers that.

---

#### [`ce/reduce_llvmreduce.py`](../ce/reduce_llvmreduce.py) — the second baseline

Wires in LLVM's own `llvm-reduce` binary. Runs it twice (once per side of the src/tgt pair), then re-verifies together. Produces IR-valid reductions, but has **no idea the two files are related** or what the counterexample says.

Added to answer: *"Wouldn't any real reducer have done this?"* — IR-validity alone buys a lot (0.821 reduction), but iraware still wins outright with 20× fewer oracle calls.

---

#### [`ce/_llvmreduce_test.py`](../ce/_llvmreduce_test.py) — the interestingness test

`llvm-reduce` needs an external "is this candidate still interesting?" script; this is it. Runs as a **separate subprocess per candidate** — so `llvm-reduce` never receives more than that process's exit code, nothing about the violation itself. Its own `alive-tv` calls don't go through the calling `Oracle` directly; `Oracle.record_external()` merges them back in afterward so `.stats()` still reports the true total cost.

---

#### [`ce/reduce_iraware.py`](../ce/reduce_iraware.py) — the smart shrinker ⭐

**The research contribution.** Three design ideas:

1. **The two files are one function, twice.** Every edit applies to *both*, matched by SSA name.
2. **Only propose valid code.** Pick the values to *keep*, then add everything they depend on. Valid IR by construction.
3. **Let the counterexample guide the search.** Alive2 already told us which values disagree, which blocks ran, and every value's type.

The passes, in order (repeated until nothing more can go):

| Pass | What it does |
|---|---|
| `dce` | Delete values nothing reads |
| `fold-branches` | The failing run took one path — delete the other |
| `prune-blocks` | Delete now-unreachable blocks |
| `simplify-cfg` | Collapse leftover trivial blocks and phis |
| `slice` | Protect bug-related values, ddmin the rest |
| `promote-operands` | Replace a computed value with a plain argument (**see warning**) |
| `drop-params` | Remove unused arguments |
| `strip-flags` | Find the smallest set of `nsw`/`nuw`/`inbounds` that still fails |

> ⚠️ **`promote-operands` generalises the program.** The bug is still provably there, but the counterexample might describe a situation that couldn't actually happen in the original program. Run `--no-promotion` as an ablation and report both.

---

#### [`ce/structured.py`](../ce/structured.py) — the second idea

Reorganises the counterexample into labelled sections: `VIOLATED PROPERTY`, `WHAT THE TRANSFORMATION CHANGED`, `CRITICAL VALUES`, `DEPENDENCY CHAIN`, `DIVERGENCE`, `INTERPRETATION`.

**Nothing here is AI-generated.** `INTERPRETATION` is a fixed template chosen by error class, filled with real values. It never states anything Alive2 didn't.

---

#### [`ce/feedback.py`](../ce/feedback.py) — the experiment grid

Combines both knobs into nine conditions. One call: bug in, AI message out.

> ⚠️ `context.md` letters the conditions two different, contradictory ways (§15 prose vs §16 table). We implement the §16 grid. `llvmreduce` conditions have no letter — always use full names.

---

#### [`ce/benchmark.py`](../ce/benchmark.py) — the plug

`normalize_feedback()` — a drop-in replacement for the benchmark's feedback line. Non-miscompilation failures pass through unchanged. Also holds `RunLog` / `Iteration` (the experiment record).

---

#### [`ce/cli.py`](../ce/cli.py) — command-line access

`check` · `reduce` · `feedback` · `compare`. See [§3](#3-commands-to-run-and-verify).

### Tests — [`tests/`](../tests/)

| File | Covers | Needs Alive2? |
|---|---|---|
| `test_alive.py` | Parsing, against real saved output | No |
| `test_irmodel.py` | Round-trip, def-use, slicing, edits | No* |
| `test_reduction.py` | ddmin, oracle rules, formatting | No |
| `test_integration.py` | Real shrinking end-to-end | Yes |

\* the 1462-reproducer corpus test needs `LAB_DATASET_DIR`.

---

#### [`tests/test_alive.py`](../tests/test_alive.py) — parser tests

6 tests. Parses real Alive2 output saved in `data/samples/` (poison, value-mismatch, UB, multi-function, correct, garbage). No Alive2 binary needed — it tests parsing of already-captured text. If you change `ce/alive.py`'s parsing logic, these catch regressions.

---

#### [`tests/test_irmodel.py`](../tests/test_irmodel.py) — IR model tests

~16 tests (some parametrized). Covers: byte-exact round-trip on sample IR, structure recovery (blocks, instructions), label vs value-operand distinction, flag detection/removal, backward slicing (including control dependence), dead-name detection, substitution, block removal (phi pruning), entry-block protection, reachability, parameter dropping, switch instructions, and the `measure()` function. Also includes the 1462-reproducer corpus round-trip check (needs `LAB_DATASET_DIR`).

---

#### [`tests/test_reduction.py`](../tests/test_reduction.py) — reduction + structured output tests

~16 tests. Covers: ddmin algorithm (isolation, minimal pair, everything-required, budget enforcement), oracle violation matching at all three strictness levels, seed extraction (diverging values, value types), structured-output formatting (all fields present, interpretation names the flag, divergence shows both sides), and plain rendering. No Alive2 binary needed — uses mock oracles.

---

#### [`tests/test_integration.py`](../tests/test_integration.py) — end-to-end tests ⭐

~18 tests. Runs the full pipeline against real `alive-tv` and `llvm-reduce` (both must be in the container). Covers: Alive2 round-trip verification, IR-aware reduction (preserves violation, shrinks, isolates the flag), generic-vs-iraware comparison on the same budget, llvm-reduce reduction (preserves violation, higher acceptance rate than generic, still loses to iraware), all matrix conditions rendering, structured feedback accuracy, non-Alive2 feedback passthrough, truncation, run-log totals, `--no-promotion` filename non-collision, summary separation, unknown-condition rejection, and legacy letter resolution.

**If you touch any reducer or the feedback pipeline, run these tests first.**

---

### Scripts — [`scripts/`](../scripts/)

---

#### [`scripts/check_ir_roundtrip.py`](../scripts/check_ir_roundtrip.py) — IR safety check

Parses every `.ll` reproducer in the dataset through `ce/irmodel.py` and confirms the output is **byte-identical** to the input. Expected result: `1462 ok, 0 mismatched, 0 crashed`. If this ever reports a mismatch, the IR model has a parsing bug that could corrupt counterexamples during reduction. Run this after changing `irmodel.py`.

---

#### [`scripts/smoke_reduce_dataset.py`](../scripts/smoke_reduce_dataset.py) — robustness stress test

Runs the IR-aware shrinker on real dataset reproducers with a **faked "after" version** (copies the src and tweaks it). This doesn't test scientific correctness — it tests that the shrinker doesn't crash on the variety of real-world IR in the dataset (vectors, metadata, attribute groups, intrinsics). Useful for catching parser limitations before a long experiment run.

---

#### [`scripts/select_bootstrap_bug.py`](../scripts/select_bootstrap_bug.py) — picked the bootstrap candidate

Scans all `llvm-apr-benchmark/dataset/*.json` files and filters for bugs that: (a) are miscompilations, (b) are checked by Alive2, (c) have a single-function fix. Ranks by simplicity (hint-region size + reproducer size). Picked `115575` (VectorCombine, 3-instruction reproducer, one lit dir). Already run; its output informed the bootstrap choice. You'd re-run this only to pick a different bootstrap candidate.

---

#### [`scripts/bootstrap_first_repair.py`](../scripts/bootstrap_first_repair.py) — build `opt` + reproduce

The script that actually builds LLVM. Phase 1 (no API key needed): resets `llvm-project` to the bug's `base_commit`, runs `cmake` + `ninja` to build `opt`, then runs the bug's reproducer to confirm it fails as expected. Phase 2 (`--full`, needs `LAB_LLM_TOKEN`): runs one real repair attempt via `examples/repair_experiment.py`'s `repair()` function. Phase 1 was successfully run on 2026-08-27 for bug `115575` (~1h53m at `--build-jobs 4`), then for all 24 sample bugs between 2026-09-01 and 2026-09-03 (see Blocker 9 — `--build-jobs` must be capped well below `os.cpu_count()` or the container OOM-kills the compiler). Phase 2 has not been run.

---

#### [`scripts/select_experiment_sample.py`](../scripts/select_experiment_sample.py) — picked the 24-bug sample

Stratified sampling from the 100 usable bugs. Splits into 3 tiers (easy/medium/hard) by hint-region size + reproducer size, then picks 8 per tier, maximizing distinct `hints.components` within each tier before repeating any (since InstCombine alone is 45% of the pool). Selection is fully deterministic (score → component → bug_id as tiebreak). Output committed at `data/experiment_sample.json`. Re-run with `--n <larger>` to pick a bigger sample.

---

### Examples — [`examples/`](../examples/)

---

#### [`examples/repair_experiment.py`](../examples/repair_experiment.py) — the experiment runner ⭐

**This is the main script you run to get results.** It is `llvm-apr-benchmark/examples/baseline.py` with exactly one line changed: the line that turns a failure into text for the next prompt now calls `ce.benchmark.normalize_feedback()` instead of pasting the raw log.

What it does for one bug: show the AI the buggy C++ code → AI replies with a patch → build LLVM → run tests + Alive2 → fixed? stop : feed failure back → repeat up to `--max-iterations` times.

Key arguments: `--condition` (which of the 9 conditions), `--max-iterations` (default 4), `--oracle-budget` (default 400), `--strictness`, `--no-promotion`, `--out` (directory for run records).

Writes `<bug_id>.<condition>.json` per run. Skips bugs that already have a record (use `--overwrite` to redo).

---

#### [`examples/summarize_results.py`](../examples/summarize_results.py) — result aggregation

Reads all `*.json` run records from a directory, groups by condition, and prints two tables:

1. **Unpaired table** — all bugs each condition attempted (can be misleading if conditions faced different subsets).
2. **Paired table** — only bugs attempted under *every* condition (the one you should read — nobody gets credit for an easier subset).

Also separates `--no-promotion` ablation results into their own table when present.

---

### Upstream benchmark — [`llvm-apr-benchmark/`](../llvm-apr-benchmark/)

This is the upstream benchmark repo, checked out **unmodified**. We do not edit it. The key files:

---

#### [`llvm-apr-benchmark/scripts/llvm_helper.py`](../llvm-apr-benchmark/scripts/llvm_helper.py) — LLVM build/test plumbing

The workhorse. Provides `git_execute()` (run git commands on the llvm-project checkout), `build_opt()` (cmake + ninja), `alive2_check()` (run `alive-tv` on src/tgt IR and return `{"src", "tgt", "log"}`), `verify_dispatch()` / `verify_test_group()` (run the bug's reproducer + regression tests). Reads `LAB_LLVM_DIR`, `LAB_LLVM_BUILD_DIR`, `LAB_LLVM_ALIVE_TV`, `LAB_DATASET_DIR` from environment at import time.

---

#### [`llvm-apr-benchmark/scripts/lab_env.py`](../llvm-apr-benchmark/scripts/lab_env.py) — the Environment class

`Environment(bug_id, cutoff)` loads a bug's JSON from the dataset and provides: `reset()` (git-reset to base_commit), `check_fast()` (reproducer only), `check_full()` (reproducer + regression tests), `get_bug_type()`, `get_hint_components()`, `get_hint_line_level_bug_locations()`, `is_single_func_fix()`, `use_knowledge()` (knowledge-cutoff bookkeeping), `dump()` (save the full run). Our `repair_experiment.py` instantiates this class directly.

---

#### [`llvm-apr-benchmark/examples/baseline.py`](../llvm-apr-benchmark/examples/baseline.py) — the original repair loop

The upstream's own repair agent. A single OpenAI-compatible chat loop with two tool calls (`get_source` and optional bisection). Our `examples/repair_experiment.py` is a simplified fork of this with one change: the feedback call. We do **not** run `baseline.py` directly — it's here as the reference for what we forked from.

---

#### [`llvm-apr-benchmark/dataset/`](../llvm-apr-benchmark/dataset/) — the 491 bug JSONs

One `.json` file per bug. Each contains: `bug_id`, `bug_type` (miscompilation/crash/hang), `base_commit`, `knowledge_cutoff`, reproducer paths, `hints` (components, files, functions, line ranges), `check_with` (alive2 or lli), `is_single_func_fix`, and the reference patch. Our scripts (`select_bootstrap_bug.py`, `select_experiment_sample.py`) scan this directory directly — they never hardcode counts.

---

## 10. The Experiment Design

### The 9 conditions

|  | plain | structured |
|---|---|---|
| **raw** (no shrinking) | `raw-plain` | `raw-structured` |
| **generic** (text shrink) | `generic-plain` | `generic-structured` |
| **llvmreduce** (IR-valid, CE-blind) | `llvmreduce-plain` | `llvmreduce-structured` |
| **iraware** (smart shrink) | `iraware-plain` | **`iraware-structured`** |

Plus `baseline` (no counterexample at all).

### What the grid tells you

- Does shrinking help? → compare **rows**
- Does layout help? → compare **columns**
- Does LLVM knowledge beat generic shrinking? → **generic row vs iraware row** ← *the actual research question*
- Is it just IR-validity, not counterexample-awareness? → **`llvmreduce` vs `iraware`**
- Do they help more together? → `iraware-structured` vs each alone

### What counts as a fix

The patch builds, fixes the bug, **and** breaks none of LLVM's existing regression tests. Enforced by the benchmark.

### Fairness

Every condition gets the same model, prompt, iteration budget, and Alive2 budget. All recorded per run.

### Dataset numbers (counted live, not from README)

| | count |
|---|---|
| Total issues | 491 |
| Crash bugs (no counterexample) | 340 |
| Hang bugs (no counterexample) | 9 |
| Miscompilations | 142 |
| — checked by Alive2 | 135 |
| — checked only by `lli` | 7 |
| — Alive2 + single-function fix | **100 ← what we can actually use** |

The 24-bug sample was drawn from the pool of 100 (8 easy / 8 medium / 8 hard, stratified by component and complexity).

---

## 11. Decisions Made

A condensed log of every blocker that was identified and resolved. These are recorded here so future debugging has the decision context.

### Blocker 1: `opt` wasn't built (2026-08-27) → RESOLVED FOR ONE BUG

`/workspace/llvm-build` was empty — nothing could run end-to-end.

**What was done:** `scripts/select_bootstrap_bug.py` picked bug `115575` (VectorCombine, 3-instruction reproducer). `scripts/bootstrap_first_repair.py` built `opt` at `--build-jobs 4` inside the Docker container. Took ~1h53m. The bug reproduces as expected.

**What wasn't done:** Phase 2 (`--full`, an actual LLM repair) — needs `LAB_LLM_TOKEN`.

### Blocker 2: Build time dwarfs token savings (2026-08-27) → DECIDED

One repair iteration = one LLVM rebuild (minutes). Saving 200 prompt tokens is noise.

**Decision:** Efficiency claim is about **iterations to fix**, not tokens or wall-clock time. Tokens and time are still recorded but aren't the headline metric.

### Blocker 3: Scale — how many bugs? (2026-08-27) → SAMPLE PICKED

100 usable bugs × 9 conditions × *k* repeats = too many builds for "all."

**Decision:** 24-bug stratified sample. Selection is fully deterministic (reproducible from the dataset alone). Committed at `data/experiment_sample.json`. Bug `115575` excluded (already build-tested). **Still undecided:** repeat count (*k*).

### Blocker 4: Model knowledge-cutoff legality (2026-08-27) → DECIDED

The 24 bugs span 2024-02-24 to 2026-02-11. No current model can honestly claim a training cutoff before those dates.

**Decision:** No benchmark-legal claims, ever. All claims are **relative comparisons between conditions** under the same model. State the model and release date plainly; never quote against the benchmark leaderboard.

### Blocker 5: Generic baseline is a strawman (2026-08-28) → FIXED

176 of 183 `generic` attempts produced invalid IR — it's attackable.

**Fix:** Added `ce/reduce_llvmreduce.py` — wraps LLVM's own `llvm-reduce` as a second baseline. IR-valid-by-construction but counterexample-blind. On the bundled sample: `llvmreduce` gets 0.821 reduction (vs `generic`'s 0.000) but `iraware` still wins with 20× fewer oracle calls.

### Blocker 6: Claimed baseline ≠ implemented one (2026-08-28) → RESTATED

`context.md` named llvm-autofix as *the* baseline. Our code builds on `llvm-apr-benchmark/examples/baseline.py`, which is much simpler.

**Fix:** Restated. llvm-autofix is cited as related work. Our repair loop is `baseline.py` with one line changed.

### Blocker 7: `promote-operands` generalisation (2026-08-28) → FIXED

Running with and without `--no-promotion` would silently overwrite each other's results.

**Fix:** `run_record_path()` gives `--no-promotion` runs a `.no-promotion` suffix. `summarize_results.py` separates the ablation into its own table.

### Blocker 8: Stale dataset numbers (2026-08-28) → FIXED

Benchmark README says 295 issues; the dataset actually has 491.

**Fix:** `context.md` corrected to the live count (491 total: 142 miscompilation, 340 crash, 9 hang). Scripts count from the dataset directory, not from docs.

### Blocker 9: `opt` built for only 1 of 24 bugs (2026-09-01 – 2026-09-03) → RESOLVED

Blocker 1 only ever covered `115575`. The other 23 sample bugs had never been
built, and `bootstrap_first_repair.py --bug-id <id>` (default
`--build-jobs=os.cpu_count()`) OOM-kills partway through the very first
attempt: the container's `mem_limit: 10g` (`docker-compose.yml`, chosen
deliberately to protect the host — see its comments) can't hold one compile
job per core for LLVM's heaviest translation units (`SelectionDAGBuilder.cpp`,
`DAGCombiner.cpp` in particular). Symptom: `ninja` fails ~10-20 minutes in
with `c++: fatal error: Killed signal terminated program cc1plus` — easy to
mistake for a real build failure since `bootstrap_first_repair.py` reports it
identically (`SystemExit` with a build-log tail). All 10 bugs in a first
batch failed exactly this way.

**Fix:** `--build-jobs 4` keeps peak container memory around 3.5GB of the
10GB cap (confirmed by watching `docker stats` live during a rebuild) —
comfortable headroom without touching the shared `mem_limit`. Also bumped
`ccache -M` from the 5GB default to 40GB, since the default was too small to
hold much across builds for 24 different `base_commit`s.

**Result:** all 24 sample bugs now have a build-verified `opt` on this
machine (built in five batches: positions 15-24, 10-14, 5-9, 1-4 of
`experiment_sample.json`'s `bug_ids` list). Per-bug wall time ranged
~18 minutes to ~2h49m depending on ccache hit rate for that `base_commit`;
total sequential compute was roughly 20 hours across all 24. Logs are at
`results/build_logs/<bug_id>.log` (gitignored — this is a local record, not
committed history; regenerate on any other machine).

**Still true:** the build itself doesn't transfer between machines — the
`llvm-build`/`ccache_data` volumes are per-machine
(`docker-compose.yml`'s own comments say so explicitly). Anyone running the
actual repair experiment on a different machine will trigger their own
`opt` build per bug regardless of this history, and should pass
`--build-jobs` sized to their own container's memory cap from the start.

### Blocker 10: *k* and the statistical test were undecided (2026-09-04) → DECIDED

Both were listed as "must choose before running" since Blocker 3, and both
change what the sweep costs, so neither could be left to the moment the numbers
land. Preregistered in [`ANALYSIS_PLAN.md`](ANALYSIS_PLAN.md), written and dated
while `results/` still held no run records.

***k* = 3.** The key correction to the original framing: *k* is not "more data".
Nine conditions over 24 bugs give one paired binary observation per bug, and
pass@k keeps n at 24 however many times a cell is re-run. What *k* buys is a
less noisy outcome per cell — at *k* = 1 and temperature 0.8 each cell is a
single coin flip, so discordant pairs are mostly sampling noise, which inflates
*b* and *c* symmetrically and biases the test toward false negatives.
`scripts/power_analysis.py` puts numbers on it: power roughly triples from
*k* = 1 (0.03–0.23) to *k* = 3 (0.10–0.57), while *k* = 5 adds little and in the
highest-rate scenario *reduces* power, because pass@5 pushes both conditions
toward a ceiling where nothing is discordant.

**McNemar's exact, one-sided, BH-corrected over four preregistered
comparisons.** Not Fisher's: the conditions share the same 24 bugs, so the data
are paired, and Fisher discards exactly the structure that makes n = 24
workable. `agentic_harness` (same LLVM middle-end bug family), `repair_llama`
and `slm_as_a_judge` all use McNemar for this shape of data; Fisher appears
once across the corpus, on genuinely unpaired contingency data
([`SLM_SELECTION.md`](SLM_SELECTION.md) §6). Exact rather than χ², since
*b* + *c* will not reach the ~15 the approximation needs.

**Stated plainly in the plan:** even at *k* = 3 this is a pilot-scale
comparison, power 0.33–0.57 for a large effect. A p above 0.05 means 24 bugs
cannot resolve it, not that there is no effect.

**Code changes this forced.** `--repeat` did not exist, and
`run_record_path()` had no trial component — three trials of a cell would have
written to one path, so the "already done" check would have stopped after the
first and *k* would have silently collapsed to 1. `summarize()` counted
records as bugs, which at *k* = 3 would have reported `bugs_attempted: 72` and
a per-run rate labelled as a repair rate; it now reports `pass_at_k` and
`pass_at_1` separately over distinct bugs. `repair_experiment.py` also stopped
writing a run record when the provider fails before the model ever answers —
that filed an infrastructure outage as a failed repair.

### Blocker 11: no model chosen (2026-09-04) → DECIDED

`LAB_LLM_MODEL` still defaulted to `deepseek-reasoner`, a paid API, against a
requirement for free open weights on a single H100.

**Decision:** `Qwen/Qwen3-Coder-30B-A3B-Instruct` (Apache 2.0), served by vLLM
in FP8 so it coexists with other jobs on a shared card. Full reasoning,
alternatives, and the serving configuration in
[`SLM_SELECTION.md`](SLM_SELECTION.md).

**The finding that drove it**, from reading `slm_research_papers/`: the SLM
literature's headline — Phi-3 3.8B matching Codex — is measured on QuixBugs,
40 one-line bugs in toy programs. On *this* task, `agentic_harness` measures
frontier models losing 35–83% of their resolution rate when moved from
SWE-bench Verified to LLVM middle-end bugs, and `llvm_bench` measures
retrieval-augmented LLMs below 5%. A 3B model on LLVM `InstCombine` would sit
on the floor — and a floor is not a null result, it is a failed experiment:
nine identical all-zero cells give McNemar no discordant pairs to test. The
selection rule is therefore "strongest open model that fits and runs fast
enough", not "smallest that might work".

Two supporting decisions: temperature stays at 0.8 (repeats at 0 would be three
copies of one answer, so *k* = 3 would cost 3× for nothing — `scripts/check_llm_endpoint.py --repeat 3`
checks this), and contamination is survivable because it inflates all nine
conditions equally, leaving the between-condition differences — the entire
result — intact. It threatens only the absolute rate, which Blocker 4 already
forbids claiming.

### Blocker 12: the "shared card" in Blocker 11 has ~20GB free, not ~50GB (2026-09-04) → DECIDED

Blocker 11's FP8 sizing ("leaves half the card free") assumed the kind of
sharing `SLM_SELECTION.md` §4 anticipated in the abstract — other jobs coming
and going, tens of GB free at any moment. The actual H100 turned out to have
a standing tenant: `nvidia-smi` showed 60531 MiB in use at 23% utilization
with **zero processes listed**, which is PID-namespace isolation hiding
another container's job, not a stale allocation anything on this side could
kill. Free memory: ~19.6–20.5GB, fluctuating, not ours to control.

`Qwen/Qwen3-Coder-30B-A3B-Instruct` needs ~31GB in FP8 for weights alone
(`SLM_SELECTION.md` §4) — a MoE model's full parameter set has to be resident
regardless of how few experts activate per token. No `--gpu-memory-utilization`
value closes a 31GB-vs-20GB gap; the first attempt to serve it failed exactly
this way (`ValueError: Free memory on device cuda:0 (19.62/79.18 GiB) ... is
less than desired GPU memory utilization (0.9, 71.26 GiB)`).

**Decision:** `Qwen/Qwen2.5-Coder-14B-Instruct` (Apache 2.0), FP8, ~14GB of
weights — already tabulated in `SLM_SELECTION.md` §4, just not the primary
pick there because that table's ranking assumed near-exclusive card access.
Same family as the rejected-fallback `Qwen2.5-Coder-32B-Instruct`, several
size classes above the 1–4B tier §3 rejected for the floor-effect risk — this
is a smaller pick forced by memory, not a retreat toward that floor.

`docker-compose.h100.yml`, `.env.h100`, `RUNBOOK.md`, and `RUNBOOK_NATIVE.md`
now default to it. Launch parameters were chosen from the live numbers above,
not the original 0.9 (headroom kept deliberately wide since the other
tenant's usage is outside anyone's control here and this has to run
unattended for days):

```bash
vllm serve Qwen/Qwen2.5-Coder-14B-Instruct \
    --served-model-name qwen2.5-coder-14b \
    --quantization fp8 \
    --max-model-len 8192 \
    --gpu-memory-utilization 0.22 \
    --host 0.0.0.0 --port 8000 \
    --api-key local-sweep
```

`--max-model-len` dropped from 32768 to 8192: `SLM_SELECTION.md` §8 already
noted the longest condition is only a few thousand tokens, and vLLM's startup
check requires enough KV cache headroom for one full-length sequence — a
lower cap needs less of that headroom to satisfy, which matters when the
headroom itself is thin. `--gpu-memory-utilization 0.22` targets ~17.4GB
(14GB weights + ~3.4GB KV/overhead), leaving a ~2-3GB buffer under the
lowest free reading observed. Re-check `nvidia-smi` before every restart —
this number is a snapshot, not a guarantee, and if free memory has dropped
further the value needs to come down with it.

**Not yet done:** re-running the nine-run pilot (Blocker-11-era instructions,
now against this model) to confirm repair rates aren't at the floor.
`SLM_SELECTION.md` §9's screening criteria apply unchanged.

### Blocker 13: Blocker 12's launch command OOM'd on KV cache, not weights (2026-09-04) → FIXED

Blocker 12's command failed with `ValueError: No available memory for the
cache blocks` and `Available KV cache memory: -0.49 GiB`. Not a repeat of
Blocker 12's problem — this was *within* the 17.4GB budget
`--gpu-memory-utilization 0.22` requested, which vLLM's upfront free-memory
check accepted (17.4GB < the ~19GB actually free at the time).

The budget just didn't add up the way Blocker 12 assumed. Weight loading
alone took 15.39GB — a bit over the ~14GB estimate — and default CUDA graph
capture (49 compiled variants, sizes up to 512) consumed the remaining ~2GB
before KV cache saw any of it.

**First fix:** add `--enforce-eager`, which skips CUDA graph capture and
`torch.compile` entirely. Costs some inference throughput; free, in effect,
since `SLM_SELECTION.md` already established inference is under 1% of this
sweep's wall time. This alone flipped `Available KV cache memory` from
`-0.49 GiB` to `+0.1 GiB` — real progress, but 0.1GB isn't enough to serve
even one request at `--max-model-len 8192` (needs 1.5GB): `ValueError: ...
the estimated maximum model length is 544`.

**Second fix, same launch attempt:** KV cache requirement scales linearly
with `--max-model-len` (vLLM's own error confirms it: `1.5GB × 544/8192 ≈
0.1GB`, matching exactly). Weights (~15.4GB) plus ~2GB fixed overhead —
present even under `--enforce-eager` — leaves only what's left of the
utilization budget for KV cache, so `--max-model-len` and
`--gpu-memory-utilization` have to move together, not separately.
`--gpu-memory-utilization 0.23` (~18.3GB budget) leaves ~0.9GB for KV cache;
`--max-model-len 4096` needs ~0.75GB, fitting with a small margin.
`--max-model-len` stayed well short of the original 32768 (and 8192)
deliberately: `SLM_SELECTION.md` §8 already measured the longest real
condition, `raw-structured`, at "a few thousand tokens", so 4096 stays
generous for the actual task rather than trimmed to the memory limit's edge.

`--gpu-memory-utilization` moved only from 0.22 to 0.23, not further, on the
same reasoning as before: once vLLM successfully starts, its allocation is
locked in and the neighboring tenant's growth afterward can't evict it — the
real risk is only at the *next restart*, when free memory is re-checked
against whatever's requested. Buying KV cache room from `--max-model-len`
preserves that margin; buying it from `--gpu-memory-utilization` would spend
it. `RUNBOOK.md` and `RUNBOOK_NATIVE.md` now carry both parameters.

**Watch for during the pilot:** whether 4096 truncates `raw-structured`'s
accumulated multi-turn context before `--max-iterations` (4) turns complete.
Not yet observed either way — the pilot (Blocker-11-era instructions, this
model, these parameters) hasn't been run.

### Blocker 14: `--max-model-len 4096` silently truncated every pilot condition (2026-09-04) → FIXED

The prediction at the end of Blocker 13 was right, but understated: the
nine-run pilot didn't just risk truncating `raw-structured` — **it truncated
all nine conditions**, at between 1 and 3 of the 4 designed turns. None of
them errored loudly. Each one wrote a normal-looking run record with
`fixed: false`, because `repair_experiment.py`'s loop catches the model-call
exception, records it to `run.notes["llm_error"]`, and breaks — and every
iteration *before* the break had already succeeded and been recorded. A
partial run looks identical to a genuine repair failure unless something
goes and reads `notes.llm_error`.

**Root cause, confirmed by grepping every pilot record's `llm_error`:**
identically-shaped `400`s — `"prompt contains at least 4097 input tokens"` —
at `--max-model-len 4096`. Not a per-condition prompt-size problem:
`repair_experiment.py`'s multi-turn loop appends the model's *entire reply*
(the complete rewritten hunk, per `FORMAT_REQUIREMENT`'s "no diffs" rule) to
the message history every turn, so context grows every iteration regardless
of condition, and 4096 didn't survive more than a few turns for anyone.
`SLM_SELECTION.md` §8's "a few thousand tokens" estimate was for one
condition's single-turn feedback text, not the accumulated 4-turn
conversation — the two are not the same number.

This also explains an apparent anomaly from the first pilot attempt:
`iraware-structured` (the most-reduced condition) truncated *fastest*, after
only one successful turn — backwards from what the mechanism should do. At
the time this looked like it might be a real confound in the structured
render format. It wasn't — it was noise from a context window too small to
show the real ordering. Once the window was large enough, the condition that
needed the most room turned out to be `raw-structured`, exactly as
`SLM_SELECTION.md` already predicted (the "longest condition" language in
§8). Worth remembering: an undersized resource limit doesn't fail in a way
that points at itself — it can look like a finding about the conditions
being compared.

**Fix, in two confirmed steps, both empirical:**

1. `--max-model-len 8192` / `--gpu-memory-utilization 0.24` (using the real
   per-run numbers Blocker 13 established: ~17.3GB fixed weights+overhead) —
   **8 of 9 conditions ran clean, all 4 turns, no `llm_error`.** Only
   `raw-structured` still truncated, on its 3rd turn.
2. `--max-model-len 12288` / `--gpu-memory-utilization 0.25` — re-tested
   `raw-structured` alone (`--condition raw-structured`, to avoid re-running
   the other eight): **4 iterations, no `llm_error`.** All nine conditions
   now confirmed clean at these parameters.

`VLLM_USE_FLASHINFER_SAMPLER=0` also added at this point (unrelated failure,
same session): vLLM's default sampler backend tries to JIT-compile a CUDA
kernel via `nvcc` on first use, which this container doesn't have installed
(CUDA runtime only, not the toolkit) — `RuntimeError: Could not find nvcc`.
The env var forces vLLM's built-in PyTorch sampler, sidestepping the need for
a compiler entirely.

**These numbers are tuned against memory that moved during diagnosis** — free
memory ranged ~19.5GB to ~23GB across the roughly one hour this took to
resolve, and `12288`/`0.25` (~19.9GB requested) was confirmed working at the
higher end of that range. If a future `vllm serve` restart fails its
admission check, that means the shared tenant's usage has grown past what it
was during this session — the fix is dialing back toward `8192`/`0.24`
(confirmed reliable at the lower, ~19.5GB end), not re-deriving the formula.

**Not yet re-verified:** whether `8192` (not `12288`) survives the full
24-bug sample — `raw-structured`'s context growth could plausibly vary by
bug. If a future sweep run truncates on `raw-structured` again even at
`12288`, that is new information (this bug's hunk size, not a generic
config problem) and worth its own note, not a silent re-run.

---

## 12. Nice-to-Haves

These don't block the experiment. Do them if time permits.

1. **Multi-function modules.** The shrinker only touches the function with the bug; it never deletes other functions.
2. **Real tokenizer.** `estimate_tokens` is chars ÷ 4 — fine for comparing conditions, wrong for absolute token counts.
3. **Better `INTERPRETATION` templates** for error classes we haven't seen much of (memory mismatches especially).

---

## The Honest Summary

**What we have:** a working, tested mechanism. 85.7% reduction on the sample, bug provably preserved, 10× fewer verifier calls than the generic baseline, 20× fewer than the `llvm-reduce` baseline, and an IR reader validated against all 1,462 real reproducers. `opt` has been built and verified for all 24 sample bugs, confirming the build/verify machinery works end-to-end at the scale the real experiment needs — not just for one bootstrap bug.

**What we don't have:** any evidence that this makes an AI fix more bugs.

That is engineering evidence, not scientific evidence. Getting the second kind requires the repair loop to actually run, and that requires an API key and compute budget.

**Before writing any of this up, read [`METHODOLOGY.md`](METHODOLOGY.md)** — it records the limits that must go in the paper's threats-to-validity section.
