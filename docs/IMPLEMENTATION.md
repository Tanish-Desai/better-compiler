# Implementation Guide

**Audience:** our team. Assumes no compiler background. Read top to bottom.

**Last updated:** 2026-08-29 (after merging PR #1: `feat/e2e-bootstrap` → `feat/counterexample-toolkit`)

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
- ✅ **`opt` built for 1 bug** (`115575`, VectorCombine) — confirmed the bug reproduces inside the Docker container
- ✅ **24-bug sample selected** and committed at `data/experiment_sample.json`
- ✅ **Experiment runner written** (`examples/repair_experiment.py`) — ready to go
- ✅ **Result aggregator written** (`examples/summarize_results.py`) — reads run records into a comparison table
- ✅ **Scripts** — `scripts/check_ir_roundtrip.py` (IR safety check), `scripts/bootstrap_first_repair.py` (build `opt` + reproduce a bug), `scripts/select_experiment_sample.py` (picked the 24-bug sample), `scripts/smoke_reduce_dataset.py` (stress-tests the shrinker on real dataset IR), `scripts/select_bootstrap_bug.py` (picked the bootstrap candidate)
- ✅ **Tests** — 4 test files in `tests/`: `test_alive.py` (parser), `test_irmodel.py` (round-trip, def-use, slicing), `test_reduction.py` (ddmin, oracle rules, structured output), `test_integration.py` (end-to-end shrinking with real Alive2 + llvm-reduce)
- ✅ **llvm-apr-benchmark/** — the upstream benchmark repo, checked out and **unmodified**. Provides the 491 real LLVM bugs, the build/test harness (`lab_env.py`, `llvm_helper.py`), and the `baseline.py` repair loop we forked
- ✅ **All 8 blockers resolved** (decisions documented in [§11](#11-decisions-made))

### What doesn't work yet

- ❌ **No repair-rate numbers exist.** The AI experiment has never run. It needs `LAB_LLM_TOKEN` (an API key).
- ❌ **`opt` built for only 1 of 24 bugs.** Each build takes ~2 hours. The other 23 bugs need their own builds.
- ❌ **Repeat count (*k* for pass@k) undecided.** Must choose before running.
- ❌ **Statistical test undecided.** Must choose before running.
- ❌ **`results/` directory is empty.** No experiment data.

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

#### Step 1: Get an API key and run one real repair

The experiment runner is ready. It just needs an API key:

```bash
# Inside the Docker container:
export LAB_LLM_TOKEN=<your-deepseek-or-openai-key>

# Run one bug, one condition (the bootstrap bug we already built opt for):
python3 examples/repair_experiment.py --condition iraware-structured 115575
```

This will validate the full end-to-end pipeline: LLM → patch → LLVM build → test → Alive2 → feedback → retry.

#### Step 2: Decide repeat count (*k*)

This is a compute-budget decision. The experiment needs repeats because AI is nondeterministic:

- *k* = 1: pilot run, ~24 bugs × 9 conditions = 216 runs
- *k* = 3: standard, ~648 runs  
- *k* = 5: stronger, ~1080 runs

Each run includes an LLVM build (~2 hours first time, faster with ccache for nearby commits).

**Decide this before running, not after.**

#### Step 3: Decide the statistical test

With 24 bugs:
- **McNemar's test** — standard for paired binary outcomes in APR
- **Fisher's exact test** — for small samples
- Choose before running so the analysis plan isn't cherry-picked

#### Step 4: Build `opt` for the rest of the sample

Only bug `115575` has a built `opt`. The other 23 need theirs:

```bash
# The experiment runner handles the build automatically per bug,
# but each first build is ~2 hours. Plan accordingly.
# ccache is configured, so close-together commits build faster.
```

#### Step 5: Run the full sweep

```bash
export LAB_LLM_TOKEN=...
for c in raw-plain generic-plain llvmreduce-plain iraware-plain \
         raw-structured generic-structured llvmreduce-structured iraware-structured; do
    python3 examples/repair_experiment.py --condition "$c" --out results/ \
        115575 89390 165878 135182 ...  # or use the bug_ids from experiment_sample.json
done
python3 examples/summarize_results.py results/
```

#### Step 6: Run the `--no-promotion` ablation

Same sweep but with `--no-promotion` added. This tests whether the `promote-operands` pass (which generalizes the program) affects results:

```bash
python3 examples/repair_experiment.py --condition iraware-structured --no-promotion --out results/ ...
```

The filenames won't collide (this was fixed in Blocker 7).

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

The script that actually builds LLVM. Phase 1 (no API key needed): resets `llvm-project` to the bug's `base_commit`, runs `cmake` + `ninja` to build `opt`, then runs the bug's reproducer to confirm it fails as expected. Phase 2 (`--full`, needs `LAB_LLM_TOKEN`): runs one real repair attempt via `examples/repair_experiment.py`'s `repair()` function. Phase 1 was successfully run on 2026-08-27 for bug `115575` (~1h53m at `--build-jobs 4`). Phase 2 has not been run.

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

---

## 12. Nice-to-Haves

These don't block the experiment. Do them if time permits.

1. **Multi-function modules.** The shrinker only touches the function with the bug; it never deletes other functions.
2. **Real tokenizer.** `estimate_tokens` is chars ÷ 4 — fine for comparing conditions, wrong for absolute token counts.
3. **Better `INTERPRETATION` templates** for error classes we haven't seen much of (memory mismatches especially).

---

## The Honest Summary

**What we have:** a working, tested mechanism. 85.7% reduction on the sample, bug provably preserved, 10× fewer verifier calls than the generic baseline, 20× fewer than the `llvm-reduce` baseline, and an IR reader validated against all 1,462 real reproducers. `opt` has been built for one bug confirming the build/verify machinery works end-to-end.

**What we don't have:** any evidence that this makes an AI fix more bugs.

That is engineering evidence, not scientific evidence. Getting the second kind requires the repair loop to actually run, and that requires an API key and compute budget.

**Before writing any of this up, read [`METHODOLOGY.md`](METHODOLOGY.md)** — it records the limits that must go in the paper's threats-to-validity section.
