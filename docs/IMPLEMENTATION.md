# What We Built, How It Works, and What Still Needs Doing

**Audience:** our team. Assumes no compiler background. Read top to bottom.

---

## Table of contents

1. [The problem, in plain terms](#1-the-problem-in-plain-terms)
2. [What our research actually claims](#2-what-our-research-actually-claims)
3. [Glossary — read this before the code](#3-glossary)
4. [The system in one picture](#4-the-system-in-one-picture)
5. [A worked example](#5-a-worked-example-the-whole-thing-in-one-page)
6. [The codebase, file by file](#6-the-codebase-file-by-file)
7. [The experiment design](#7-the-experiment-design)
8. [How to run things](#8-how-to-run-things)
9. [Hindrances — what is blocking us](#9-hindrances--what-is-blocking-us)
10. [What needs doing, in order](#10-what-needs-doing-in-order)

---

## 1. The problem, in plain terms

A **compiler** turns source code into machine code. On the way it **optimizes** —
rewrites your code to be faster while (supposedly) keeping the behaviour
identical.

Sometimes it gets that wrong. The rewritten code is faster *and different*.
Your program now does the wrong thing, and nothing warned you. This is called a
**miscompilation**, and it is nasty because everything looks fine — it compiles,
it runs, it just silently produces wrong answers.

LLVM is one of the most widely used compilers in the world (Clang, Rust, Swift
all build on it). It has these bugs. Fixing them is slow, expert work.

There is a tool called **Alive2** that can *mathematically prove* a specific
optimization is wrong. When it finds a problem it prints a **counterexample** —
a specific input where the before-code and after-code disagree.

So a natural idea: give an AI model the buggy compiler code plus Alive2's
counterexample, and ask it to fix the bug. **People are already doing this.**
That is the `llvm-autofix` project, and it works reasonably well.

---

## 2. What our research actually claims

This is the part it is easiest to get wrong when explaining the project, so
here it is explicitly.

**We are NOT claiming any of these:**

| ❌ Not our claim | Who already did it |
|---|---|
| "AI can fix LLVM bugs" | `llvm-autofix` |
| "Shrinking failing inputs helps AI repair" | `ReduceFix` |
| "Minimal feedback helps AI" | `PGS` |

**What we are actually asking:**

> Alive2's counterexample is cluttered — most of it has nothing to do with the
> bug. If we clean it up **using knowledge of how LLVM code works**, does the AI
> fix more bugs than if we clean it up with a generic, compiler-unaware method?

That is a narrow question, and narrow is good. It is answerable, and nobody has
answered it.

There are really **two separate ideas** bundled in there, and a big part of the
design is keeping them apart:

- **Idea 1 — shrink it.** Less clutter, less for the model to wade through.
- **Idea 2 — organise it.** Same information, laid out in labelled sections.

These could have completely different effects. Maybe only one matters. Maybe
neither matters alone but together they do. Our experiment is built to tell
those cases apart, which is why it is a grid rather than an "ours vs theirs".

---

## 3. Glossary

Every term you will hit in the code. Skim now, come back later.

### Compiler terms

**LLVM IR**
: The intermediate language LLVM optimizes. Lower-level than C, higher-level
than assembly. Everything in this project operates on IR, not C++ source.

**`opt`**
: LLVM's command-line optimizer. Feed it IR, name a pass, get optimized IR out.
This is what actually triggers the bugs.

**pass**
: One optimization. `InstCombine`, `LoopVectorize`, `SLPVectorizer` are the
ones that show up most in our dataset.

**middle-end**
: The optimizer part of a compiler — the bit between the language frontend and
machine-code generation. All our bugs are here.

### LLVM IR structure

**SSA (Static Single Assignment)**
: Every named value is assigned **exactly once**. `%a` refers to one specific
computation, forever. Hugely convenient for us: names are stable identifiers.

**basic block**
: A straight run of instructions with a label, ending in exactly one
jump/return. Control flow only happens at block ends.

**terminator**
: The last instruction of a block — `ret`, `br`, `switch`.

**def-use**
: "def" = the instruction that creates a value. "use" = an instruction that
reads it. Following these links tells us what depends on what.

**phi node**
: At a point where two paths merge, `phi` picks a value based on which block
you came from. `%p = phi i8 [ %r1, %then ], [ %r2, %else ]`.

**flags** — `nsw`, `nuw`, `inbounds`, `exact`
: Promises attached to an instruction. `nsw` = "this addition never overflows".
The optimizer attaches these to unlock further optimizations. **Attaching one
that isn't actually justified is one of the most common miscompilation bugs.**

### Correctness concepts

**poison**
: LLVM's marker for "this value is garbage because a promise was broken". Not a
number. Spreads to anything computed from it.

**UB (undefined behaviour)**
: "The program did something illegal, all bets are off." An optimization may
never *introduce* UB into a program that didn't have it.

**refinement**
: The rule an optimization must obey: the new code must behave the same as the
old, or be *more* defined — never less. "Doesn't verify" = this rule was broken.

**counterexample**
: A concrete input where before-code and after-code disagree. Alive2's output
when it finds a bug.

**src / tgt**
: "source" and "target" — the code before and after the optimization. These
appear constantly in the code.

### Our terms

**oracle**
: The referee. After every edit we make while shrinking, the oracle re-runs
Alive2 and answers "is this still the same bug?" Standard term in program
reduction research.

**ddmin (delta debugging minimization)**
: The classic shrinking algorithm. "Some subset of these items causes the
failure — find a small subset that still does." Removes big chunks first, then
narrows down.

**slice**
: The set of instructions a particular value actually depends on. Everything
outside the slice is (probably) irrelevant clutter.

**condition**
: One cell of our experiment grid, e.g. `iraware-structured`.

---

## 4. The system in one picture

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
   │   ┌─────────────────────┐        ┌──────────────────────┐     │
   │   │  KNOB 1: shrink it  │        │ KNOB 2: organise it  │     │
   │   │                     │        │                      │     │
   │   │  raw    (no change) │  then  │  plain  (as-is)      │     │
   │   │  generic (text)     │   ──►  │  structured (labels) │     │
   │   │  iraware (smart)    │        │                      │     │
   │   └─────────────────────┘        └──────────────────────┘     │
   │                                                               │
   │        every edit checked by the ORACLE: still same bug?      │
   └───────────────────────────────────────────────────────────────┘
```

**The key structural point:** we did not build a repair agent. One already
exists. We built the box in the middle and a way to A/B test what comes out of
it.

---

## 5. A worked example (the whole thing in one page)

### Step 1 — Alive2 finds a bug

We give it two versions of the same function. Before:

```llvm
define i8 @f(i8 %x, i8 %y, i8 %z) {
entry:
  %a = add nsw i8 %x, 1
  %b = mul i8 %y, 3
  %c = sub i8 %a, %b
  %d = xor i8 %z, 7
  %e = and i8 %c, %d
  %cmp = icmp slt i8 %x, 0
  br i1 %cmp, label %t, label %f
t:
  %r1 = shl i8 %e, 1
  br label %join
f:
  %r2 = ashr i8 %e, 1
  br label %join
join:
  %p = phi i8 [ %r1, %t ], [ %r2, %f ]
  %out = add nuw i8 %p, %a       ← after "optimization" this gains `nsw`
  ret i8 %out
}
```

Alive2 says:

```
ERROR: Target is more poisonous than source

Example:
i8 %x = #x7e (126)
...
Source:  i8 %out = #x82 (130, -126)
Target:  i8 %out = poison
```

Translated: *with x=126, the original code computes 130. The "optimized" code
produces poison — garbage. That is not allowed.*

### Step 2 — Our shrinker goes to work

It notices `%b`, `%c`, `%d`, `%e`, both branches, and the phi node have nothing
to do with why `%out` became poison. It deletes them — **checking with Alive2
after every single deletion** that the bug is still there.

Result:

```llvm
; source
%out = add i8 %ce.arg5, %ce.arg0

; target
%out = add nsw i8 %ce.arg5, %ce.arg0
```

**28 instructions → 4.** The entire bug is now one visible difference: the
optimizer attached `nsw` — promising the addition never overflows — when it had
no right to.

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

### Step 4 — Compare against the dumb baseline

Same input, generic text-based shrinker:

| | instructions after | Alive2 calls used |
|---|---|---|
| **generic (text)** | 28 (nothing removed) | 183 |
| **iraware (smart)** | 4 | 17 |

The generic one deletes a line, the code no longer parses, Alive2 rejects it —
176 out of 183 times. It has no way to know that deleting the line defining
`%a` breaks the five lines that use it.

> ⚠️ **This is one hand-built example.** It shows the mechanism works. It says
> nothing about whether the AI actually fixes more bugs — that experiment has
> not run yet.

---

## 6. The codebase, file by file

```
better-compiler/
├── ce/                    ← the library (everything we built)
├── tests/                 ← 57 tests
├── scripts/               ← safety checks
├── examples/              ← the experiment runner
├── data/samples/          ← real Alive2 output, saved for tests
├── docs/                  ← this file + METHODOLOGY.md
└── llvm-apr-benchmark/    ← upstream, untouched
```

### The library: `ce/`

Files are listed in dependency order — each builds on the ones above.

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

**Why it's tricky:** Alive2's format isn't documented — it's just what the tool
prints. So parsing is best-effort, and anything unrecognised is kept as raw
text rather than crashing. We built this against **real captured output**, not
guesses.

---

#### [`ce/irmodel.py`](../ce/irmodel.py) — read and edit LLVM IR

The foundation of everything "IR-aware". Reads `.ll` text into
functions → blocks → instructions.

| Thing | What it does |
|---|---|
| `parse_module(text)` | Text → objects |
| `Module.text()` | Objects → text (must be byte-identical) |
| `backward_slice(fn, values)` | What do these values depend on? |
| `dead_names(fn)` | Which values does nothing read? |
| `remove_instructions`, `substitute`, `drop_params`, `remove_blocks` | The edits |
| `measure(text)` | Size: lines, instructions, blocks, values |

**The one safety rule:** anything we don't understand must survive
parse-then-print **byte for byte**. We only model a fraction of LLVM IR; real
reproducers are full of vectors, metadata, attribute groups. If we silently
mangled those we'd corrupt code we only meant to shrink.

> ✅ Verified against **all 1462 real reproducers** in the dataset. This check
> already caught three genuine bugs.

**Nice detail:** `backward_slice` also follows *control* dependence. If you need
a value, you also need the branch condition that got you to it. Miss that and
your slice is wrong.

---

#### [`ce/oracle.py`](../ce/oracle.py) — the referee ⭐

**The most important file in the project.**

Shrinking is easy if you don't care about correctness — delete everything, get
an empty file. What makes it *useful* is that the result still demonstrates the
same bug. So every proposed edit goes through:

```
propose edit → re-run Alive2 → still the same bug?
                                 yes → keep it
                                 no  → throw it away
```

Because every edit is checked, the shrunk counterexample isn't *probably* still
valid — it's **provably** still valid, confirmed by the same formal tool that
found the bug.

"The same bug" has three definitions you can pick between:

| Setting | Requires |
|---|---|
| `any_failure` | Some failure still happens (weakest — can drift onto a different bug) |
| `error_class` ← default | Same error type, same function |
| `error_class_and_kind` | Also same failure kind (poison stays poison, UB stays UB) |

It also **counts its own cost** (`.calls`, `.seconds`). That's experiment data —
one of our research questions is how expensive each strategy is.

---

#### [`ce/reduction.py`](../ce/reduction.py) — shared machinery

`Reduction` (the report card both shrinkers fill in) and `ddmin` (the shrinking
algorithm).

ddmin removes big chunks first and only narrows when a big removal fails — much
faster than removing items one at a time. Both shrinkers use it, just over
different kinds of item:

- generic → over **lines of text**
- iraware → over **instructions** and **flags**

That difference is essentially the whole experiment.

---

#### [`ce/reduce_generic.py`](../ce/reduce_generic.py) — the dumb shrinker (control group)

~70 lines. Treats the two files as plain text, ddmin over the lines. Knows
nothing about LLVM.

**Why build something bad on purpose?** Because without it the experiment
proves nothing. If our smart shrinker helps, a reviewer will immediately ask:

> "Is that because your shrinking is clever, or just because shorter prompts
> help? Any shrinking would have done that."

Fair question. So we need a version that shrinks *without* LLVM knowledge:

- generic helps just as much → the benefit is short prompts
- iraware helps much more → the benefit is understanding

For that to be honest, this baseline gets **the same oracle and the same
budget**. Its only handicap is ignorance — the variable under test.

---

#### [`ce/reduce_iraware.py`](../ce/reduce_iraware.py) — the smart shrinker ⭐

**The research contribution.** Three ideas make it work:

**1. The two files are one function, twice.**
`src` and `tgt` are before/after versions of the same code, so every edit
applies to *both*, matched by SSA name. A text shrinker can't even express this.

**2. Only propose valid code.**
The generic shrinker deletes a line and hopes. This one works backwards: pick
the values to *keep*, then automatically add everything they depend on. Results
are valid IR **by construction** — which is why it needs 17 Alive2 calls where
generic burns 183.

**3. Let the counterexample guide the search.**
Alive2 already told us which values disagree, which blocks ran, and every
value's type. We use all three instead of guessing.

The passes, applied in order and repeated until nothing more can go:

| Pass | What it does |
|---|---|
| `dce` | Delete values nothing reads |
| `fold-branches` | The failing run took one path — delete the other |
| `prune-blocks` | Delete now-unreachable blocks |
| `simplify-cfg` | Collapse leftover trivial blocks and phis |
| `slice` | Protect bug-related values, ddmin the rest |
| `promote-operands` | Replace a computed value with a plain argument, letting its whole producer chain be deleted |
| `drop-params` | Remove unused arguments |
| `strip-flags` | Find the smallest set of `nsw`/`nuw`/`inbounds` that still fails |

> ⚠️ **`promote-operands` is our most aggressive pass and a genuine validity
> concern.** Turning a computed value into a free parameter *generalises* the
> program — the value can now be anything. The bug is still provably there, but
> the counterexample might describe a situation that couldn't actually happen in
> the original program. Switch it off with `--no-promotion` and report both.

---

#### [`ce/structured.py`](../ce/structured.py) — the second idea

Reorganises the counterexample into labelled sections: `VIOLATED PROPERTY`,
`WHAT THE TRANSFORMATION CHANGED`, `CRITICAL VALUES`, `DEPENDENCY CHAIN`,
`DIVERGENCE`, `INTERPRETATION`, and so on.

**Nice trick:** the diff matches instructions **by value name, not line
number**. Optimizers reorder and renumber constantly, which makes a normal diff
enormous and useless. Matching `%out` to `%out` shows the one real change.

> **Nothing here is AI-generated.** It would be easy and wrong to have a model
> write the explanation — then we'd be testing *that model*, not our format.
> `INTERPRETATION` is a fixed template chosen by error class, filled with real
> values. It never states anything Alive2 didn't.

---

#### [`ce/feedback.py`](../ce/feedback.py) — the experiment grid

Combines both knobs into seven conditions. One call: bug in, AI message out.

> ⚠️ **`context.md` letters the conditions two different, contradictory ways**
> (§15 prose vs §16 table). We implement the §16 grid; §15's letters still
> resolve via `LEGACY_LETTERS`.
> **Always write full condition names. A bare "condition C" is ambiguous here.**

---

#### [`ce/benchmark.py`](../ce/benchmark.py) — the plug

`normalize_feedback()` — a drop-in replacement for the one line in the
benchmark's loop that pastes failure text into the prompt.

Not every failure is a miscompilation — patches fail to compile, crash, break
regression tests. Those have no counterexample, so they pass through unchanged.
That means a repair loop can route **all** its feedback through this one call.

Also holds `RunLog` / `Iteration` (the experiment record) and `summarize()`.

---

#### [`ce/cli.py`](../ce/cli.py) — command-line access

`check` · `reduce` · `feedback` · `compare`. See [§8](#8-how-to-run-things).

### Tests — [`tests/`](../tests/)

| File | Covers | Needs Alive2? |
|---|---|---|
| `test_alive.py` | Parsing, against real saved output | No |
| `test_irmodel.py` | Round-trip, def-use, slicing, edits | No* |
| `test_reduction.py` | ddmin, oracle rules, formatting | No |
| `test_integration.py` | Real shrinking end-to-end | Yes |

\* the 1462-reproducer corpus test needs `LAB_DATASET_DIR`.

**57 tests, all passing.**

### Scripts — [`scripts/`](../scripts/)

- **`check_ir_roundtrip.py`** — the safety check. Expects `1462 ok, 0 mismatched`.
- **`smoke_reduce_dataset.py`** — runs the shrinker on real messy dataset IR.
  Fakes the "after" version (since `opt` isn't built), so it proves *robustness*,
  not a research result.

### Examples — [`examples/`](../examples/)

- **`repair_experiment.py`** — the actual experiment. `baseline.py` with one
  line changed.
- **`summarize_results.py`** — results table. **Read the second ("paired")
  table** — it only counts bugs every condition attempted, so nobody gets
  credit for having faced an easier subset.

---

## 7. The experiment design

|  | plain | structured |
|---|---|---|
| **raw** (no shrinking) | `raw-plain` | `raw-structured` |
| **generic** (text shrink) | `generic-plain` | `generic-structured` |
| **iraware** (smart shrink) | `iraware-plain` | **`iraware-structured`** |

Plus `baseline` — no counterexample at all.

**Why a grid and not "ours vs theirs":** a grid can answer *why* something
helped.

- Does shrinking help? → compare **rows**
- Does layout help? → compare **columns**
- Does LLVM knowledge beat generic shrinking? → **generic row vs iraware row** ← *the actual research question*
- Do they help more together? → `iraware-structured` vs each alone

**What counts as a fix:** the patch builds, fixes the bug, **and** breaks none
of LLVM's existing regression tests. Enforced by the benchmark, not us.

**Fairness rules:** every condition gets the same model, prompt, iteration
budget, and Alive2 budget. All recorded per run so it can be checked, not
assumed.

---

## 8. How to run things

```bash
docker compose up -d
```

Your code is live-mounted, so edits on your machine take effect immediately —
no rebuild needed.

```bash
# 1. Tests
docker compose exec better-compiler python3 -m pytest tests -q
# expect: 57 passed

# 2. Safety check
docker compose exec better-compiler python3 scripts/check_ir_roundtrip.py
# expect: 1462 ok, 0 mismatched, 0 crashed

# 3. See the six conditions side by side  ← best first thing to try
docker compose exec better-compiler python3 -m ce.cli \
    compare data/samples/poison.src.ll data/samples/poison.tgt.ll

# 4. See what the AI would actually receive
docker compose exec better-compiler python3 -m ce.cli \
    feedback data/samples/poison.src.ll data/samples/poison.tgt.ll \
    --condition iraware-structured

# 5. Shrink something, verbosely
docker compose exec better-compiler python3 -m ce.cli \
    reduce data/samples/poison.src.ll data/samples/poison.tgt.ll --strategy iraware
```

The real experiment (**needs `opt` built + an API key**):

```bash
export LAB_LLM_TOKEN=...
for c in raw-plain generic-plain iraware-plain \
         raw-structured generic-structured iraware-structured; do
    python3 examples/repair_experiment.py --condition "$c" --all --out results/
done
python3 examples/summarize_results.py results/
```

---

## 9. Hindrances — what is blocking us

### 🔴 Blocker 1: `opt` isn't built, so nothing runs end-to-end

`/workspace/llvm-build` is empty. Every bug needs LLVM built at its own specific
commit, and **every repair attempt rebuilds it**.

This is *the* blocker. Everything else works; none of it has produced a single
repair-rate number.

**Fix:** build LLVM for one bug, get one end-to-end repair working, then
decide how to scale. `ccache` is already configured, which makes later builds
much cheaper since most commits are close together in history.

**Status (2026-08-27): resolved for one bug.** `opt` has been built for real,
inside a real container (Docker Engine + Compose v2 in a WSL2 Ubuntu distro),
and the bootstrap bug reproduces as expected. `/workspace/llvm-build` (the
`llvm_build` volume) is no longer empty.

- [`scripts/select_bootstrap_bug.py`](../scripts/select_bootstrap_bug.py) —
  scans the dataset for bugs that qualify for `repair_experiment.py` at all
  (miscompilation, single-function fix, checked by Alive2 not just `lli`) and
  ranks them by simplicity. Picked `115575` (VectorCombine, 3-instruction
  reproducer, one lit dir) as the bootstrap candidate.
- [`scripts/bootstrap_first_repair.py`](../scripts/bootstrap_first_repair.py) —
  run inside the container. Phase 1 (no API key needed) resets to `115575`'s
  `base_commit`, builds `opt`, and confirms the bug actually reproduces there.
  Phase 2 (`--full`, needs `LAB_LLM_TOKEN`) runs the real repair loop against
  that same build via `examples/repair_experiment.py`'s own `repair()`,
  unmodified, for one condition.

**What actually happened, for real, on 2026-08-27:**
```
$ docker compose exec better-compiler python3 scripts/bootstrap_first_repair.py --build-jobs 4
[115575] base_commit=6fb2a6044f11e251c3847d227049d9dae8b87796 bug_type=miscompilation
[115575] resetting llvm-project to base_commit...
[115575] building opt...
[115575] check_fast finished in 6831s (builds so far: 1, build failures: 0)
[115575] opt builds, and the bug reproduces as expected at base_commit.
```
~1h53m wall clock at `--build-jobs 4` (chosen deliberately low: the container
had 18 cores but only ~7.6GB RAM available, and full parallelism risks OOM —
peak usage during the build topped out around 6.1GB, so 4 jobs was the right
call, not just a conservative guess). No build failures, first attempt.
Phase 2 (`--full`, an actual LLM repair attempt) has **not** been run yet — it
needs `LAB_LLM_TOKEN`, which nobody has supplied.

**Next actual step:** decide whether to spend API budget on Phase 2 for this
bug (`--full --condition <name>`), or move on to picking the sample for
Blocker 3 now that the mechanics are proven end-to-end for one bug.

---

### 🟢 Blocker 2: build time probably dwarfs what we're measuring — DECIDED

We planned to measure efficiency in tokens saved. But if one iteration = one
LLVM rebuild (minutes), then saving 200 prompt tokens is **statistical noise**
in wall-clock terms.

**Decided 2026-08-27:** the efficiency claim is reframed around **fewer LLM
iterations to fix** (and the build/oracle-call counts that scale with
iterations), not fewer tokens or seconds. Reflected in `context.md` (RQ4, H5,
§18) and `docs/METHODOLOGY.md` §5. Tokens and wall-clock time are still
recorded in every `RunLog` (`ce/benchmark.py`) and still worth reporting —
just as descriptive context beside the repair-rate/iteration numbers, never
as the headline "N% more efficient" claim.

This was a documentation decision, not a code change — `ce/benchmark.py`'s
`totals()`/`summarize()` already computed `mean_iterations` alongside the
token/time fields; nothing there needed touching, only which number the
prose leads with.

---

### 🟢 Blocker 3: scale — SAMPLE PICKED

Real numbers from the dataset:

| | count |
|---|---|
| Total issues | 491 |
| Crash bugs (no counterexample) | 340 |
| Hang bugs (no counterexample) | 9 |
| Miscompilations | 142 |
| — of which checked by Alive2 | 135 |
| — checked only by `lli` | 7 |
| — of the 135, also `is_single_func_fix` | **100 ← what we can actually use** |

The 135 figure this blocker was originally written against overcounts: multi-
function fixes are skipped by `repair_experiment.py`'s `repair()` (its
`is_single_func_fix()` check) regardless of Alive2 coverage, so the real pool
is **100**, not 135.

100 bugs × 6 conditions × *k* repeats (AI is nondeterministic, so we need
repeats for pass@k) × one LLVM build per iteration = **thousands of builds**.

**Decided 2026-08-27:** [`scripts/select_experiment_sample.py`](../scripts/select_experiment_sample.py)
picks a stratified 24-bug sample (8 each easy/medium/hard by hint-region +
reproducer size, maximizing distinct `hints.components` within each tier
before repeating one — InstCombine alone is 45% of the pool, so an unweighted
pick would be mostly InstCombine at every tier). Selection is fully
deterministic — score, then component, then bug_id as tiebreak — so it's
reproducible from the dataset alone, not "some random sample." The committed
result is [`data/experiment_sample.json`](../data/experiment_sample.json):
`115575` (the Blocker 1 bootstrap bug) is excluded by default since it's
already been build-tested in isolation.

24 is a starting point for a pilot, not a derived sufficient-power number —
see the script's docstring for the reasoning, and re-run with a larger `--n`
once Phase 2 gives a real per-iteration wall-clock estimate to budget
against. **Still undecided:** *k* (the repeat count for pass@k) — that's a
compute-budget call the sampling script deliberately leaves open; see "What
needs doing" below.

---

### 🟢 Blocker 4: benchmark rules conflict with using a good model — DECIDED

The benchmark says you may only use a model whose training cutoff is *earlier*
than the bug. Bugs run into 2025. Any frontier model we'd want to use likely
**violates that rule**, so we can't claim benchmark-legal fixes.

**Decided 2026-08-27, grounded in the actual sample** (not a general
statement — checked against `data/experiment_sample.json`'s real
`knowledge_cutoff` fields): the 24 picked bugs span **2024-02-24 to
2026-02-11**. No model that exists today can honestly claim a training
cutoff before the latest of those — so for this sample specifically, "legal"
is off the table, not just improbable.

There is a second, sharper problem underneath the first: the harness's
legality check (`lab_env.Environment.use_knowledge()`) only compares a
**self-declared** cutoff string against each bug's date — it has no way to
verify that a model's real training data actually respects it.
`examples/repair_experiment.py`'s existing default,
`LAB_LLM_BASEMODEL_CUTOFF=2023-12-31`, paired with `deepseek-reasoner`
(DeepSeek-R1, released January 2025), is almost certainly **not** an honest
claim about that model's real training data — it would make every one of the
24 bugs look "legal" to the harness's bookkeeping without that meaning
anything. Flagged in the code itself now (see the comment above `Model.__init__`
in `repair_experiment.py`) so nobody mistakes passing the harness's check for
an actual legality guarantee.

**The decision, unchanged from the original proposed fix, now stated
explicitly:** we do not claim benchmark-legal or leaderboard-eligible
absolute repair rates, for any model. Every claim is a **relative
comparison between conditions run under the same model** — that comparison's
validity does not depend on the model's knowledge cutoff at all, since every
condition gets identical (and identically "illegal") access to post-cutoff
knowledge. State the model and its actual (best-effort, not
harness-verified) release/training date plainly in the writeup; never quote
the run against the benchmark's own leaderboard.

---

### 🟢 Blocker 5: our generic baseline is attackable — FIXED

A reviewer will say: *"line-level ddmin on `.ll` obviously produces invalid code
— that's a strawman."* And they have a point: 176 of its 183 attempts were
invalid.

**Fixed 2026-08-28:** [`ce/reduce_llvmreduce.py`](../ce/reduce_llvmreduce.py)
wires in `llvm-reduce` (ships with LLVM, already built alongside `opt` in
Blocker 1's run — no extra CMake flag or build step needed) as a second
baseline, exposed as the `llvmreduce-plain`/`llvmreduce-structured`
conditions (`ce/feedback.py`'s `REDUCTIONS` now has 4 levels, not 3).

`llvm-reduce` reduces one file against one opaque interestingness test — it
has no native notion of a src/tgt pair. [`ce/_llvmreduce_test.py`](../ce/_llvmreduce_test.py)
supplies that pairing entirely from the outside (see both files' docstrings
for the two-pass, budget-honoring, externally-counted design); `llvm-reduce`
itself never sees more than a pass/fail bit per candidate.

**Real result, run against the bundled sample (not simulated):**

| condition | instructions after | reduction | oracle calls | seconds |
|---|--:|--:|--:|--:|
| `raw` | 28 | — | — | — |
| `generic` | 28 | 0.000 | 183 | 4.7 |
| `llvmreduce` | 5 | 0.821 | 351 | 32.5 |
| `iraware` | 4 | 0.857 | 17 | 0.9 |

This is the outcome that makes Blocker 5 worth having done: `llvmreduce`
closes almost all of `generic`'s gap (IR-validity alone buys a lot — its
oracle acceptance rate is ~65% vs `generic`'s ~4%), which is exactly the
"maybe any real reducer would have done this" objection a reviewer would
raise. But `iraware` still wins outright — smaller result, **20x fewer
oracle calls** — showing counterexample-awareness adds real, separately
measurable value beyond mere IR-validity, not just repeating what
`llvmreduce` already shows. Verified end-to-end in the real container: 5 new
integration tests in `tests/test_integration.py` pin this relationship, all
passing against real `alive-tv` + `llvm-reduce`, plus the full 62-test suite.

---

### 🟢 Blocker 6: the claimed baseline isn't the implemented one — RESTATED

`context.md` names **llvm-autofix** as the primary baseline. Our code builds on
llvm-apr-benchmark's much simpler `baseline.py`. Those are different systems.

**Decided 2026-08-28 (the "restate" branch of the fix, not the "integrate"
one):** integrating with llvm-autofix would mean standing up a second,
unfamiliar agentic harness (specialized tools, its own prompting, its own
repair loop) we don't have access to and haven't audited — swapping one
undocumented gap for a much larger, riskier one, for a project already
carrying five other blockers. Restating what we actually compare against is
the honest, bounded fix.

Checked `llvm-apr-benchmark/examples/baseline.py` directly rather than
assuming: it's a single OpenAI-compatible chat loop with two tool calls
(`get_source`, and an optional bisection helper), no compiler-specific
scaffolding beyond what the benchmark harness itself provides (the
build/test/Alive2 plumbing in `llvm_helper.py`/`lab_env.py`). That is a real
system, but a much simpler one than `llvm-autofix` is described as being in
`context.md` §6 (specialized tools, sparse-report understanding, a published
agentic-harness paper).

`context.md` §6 has been corrected: it no longer claims llvm-autofix as
*the* baseline our repair loop runs against. llvm-autofix stays exactly what
it always should have been — the strongest published prior result to cite in
related work, framing why an LLM-repair-for-LLVM approach is worth trying at
all — while `examples/repair_experiment.py` (a one-line-changed fork of
`baseline.py`) is named as what this project's numbers are actually measured
against. The narrower, honest framing (`context.md` §6, revised): *how does
verification-feedback representation affect an already-plausible,
`baseline.py`-level compiler-repair loop* — not a claim about improving on
llvm-autofix specifically.

---

### 🟢 Blocker 7: `promote-operands` generalises the program — FIXED

See [`reduce_iraware.py`](#cereduce_irawarepy--the-smart-shrinker-) above.

**Fixed 2026-08-28:** `--no-promotion` already existed, but "switchable"
undersold a real gap — `RunLog.write()` and `repair_experiment.py`'s
"already done" pre-check both keyed the output filename by `(bug_id,
condition)` only. Running the ablation for a bug/condition already run would
either **silently overwrite** the paired result, or (worse) get **skipped
entirely** by the pre-check thinking it was already done — either way, "run
both and report both" was not actually possible without manually renaming
files between runs.

Fixed with one shared function,
[`run_record_path`](../ce/benchmark.py) (`ce/benchmark.py`), used by both
`RunLog.write()` and `repair_experiment.py`'s pre-check so they can't drift
apart again: the default (promotion-on) filename is unchanged
(`<bug_id>.<condition>.json`), and `--no-promotion` runs get a
`.no-promotion` suffix instead of colliding.
[`examples/summarize_results.py`](../examples/summarize_results.py) now
keeps the ablation out of the main comparison tables (it's a separate axis,
not another condition) and prints it as its own labeled table when present,
so "report both as an ablation" is now something running the sweep with and
without `--no-promotion` actually produces, not just permits. Pinned by
`test_no_promotion_ablation_does_not_collide_with_the_default_run` in
`tests/test_integration.py`.

---

### 🟢 Blocker 8: stale numbers everywhere — FIXED

The benchmark README says 295 issues; there are actually **491**. Upstream also
migrated to `llvm-autofix`.

**Fixed 2026-08-28:** `context.md` §2 itself had exactly this stale quote —
"295 verified issues... 106 miscompilation / 181 crash / 8 hang" — sitting
uncorrected since it was written. Added a footnote there with the live count
(same method as Blocker 3's sample selection: counted from the dataset
directory, not read off a doc): **491 total — 142 miscompilation, 340 crash,
9 hang.** No other stale count found elsewhere in this repo's own docs
(`grep -rn "295"` across `README.md`/`context.md`/`docs/`/`examples/`/`ce/`/
`scripts/` turns up only this one, now-annotated, spot).

The practice going forward is already structural, not just a reminder:
`scripts/select_bootstrap_bug.py` and `scripts/select_experiment_sample.py`
both compute their counts live from `llvm-apr-benchmark/dataset/*.json` every
time they run — neither hardcodes a total, so there is nothing in this repo's
own tooling left to go stale the way the upstream README did.

---

## 10. What needs doing, in order

### Before anything else

1. **Build `opt` for a single bug.** Not all of them — one. Get one end-to-end
   repair running under one condition. This will surface integration problems
   the tests can't possibly catch.
   Run `scripts/bootstrap_first_repair.py` inside the container (see Blocker 1
   above) — it does exactly this, against a pre-selected simple candidate.
2. ~~Decide the model and the knowledge-cutoff position (Blocker 4).~~ Done:
   no legality claim, ever — relative comparisons under the same model only.
3. ~~Decide the efficiency framing (Blocker 2).~~ Done: iterations, not
   tokens/seconds — see Blocker 2 above.

### To make the science defensible

4. ~~Add `llvm-reduce` as a second generic baseline (Blocker 5).~~ Done:
   `llvmreduce-plain`/`llvmreduce-structured`, verified against the real
   binary and real `alive-tv`.
5. ~~Decide the sample~~ Done (Blocker 3): `data/experiment_sample.json`, 24
   bugs. **Still open: how many repeats (*k*, for pass@k) and what
   statistical test** — decide before running, not after.
6. **Run the `--no-promotion` ablation** alongside the main sweep. (Blocker 7
   fixed the filename collision that would have silently broken this — this
   item is still open because the sweep itself hasn't run yet.)

### Nice to have

7. **Multi-function modules.** The shrinker only touches the function with the
   bug; it never deletes other functions from the file.
8. **Real tokenizer.** `estimate_tokens` is chars ÷ 4 — fine for comparing
   conditions, wrong for quoting absolute cost.
9. **Better `INTERPRETATION` templates** for error classes we haven't seen much
   of yet (memory mismatches especially).

---

## The honest summary

**What we have:** a working, tested mechanism. 85.7% reduction on the sample,
bug provably preserved, 10× fewer verifier calls than the generic baseline, and
an IR reader validated against all 1462 real reproducers.

**What we don't have:** any evidence that this makes an AI fix more bugs.

That is engineering evidence, not scientific evidence. Getting the second kind
requires the repair loop to actually run, and that requires the LLVM build.

**Before writing any of this up, read [`METHODOLOGY.md`](METHODOLOGY.md)** — it
records the limits that must go in the paper's threats-to-validity section.
