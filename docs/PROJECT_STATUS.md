# Project Status & Resource Requirements

**Written:** 2026-09-10. Snapshot as of commit `4c07c52` (branch `feat/h100-sweep`).

---

## 1. What the project is

Research into automated program repair (APR) for real-world LLVM
middle-end/optimization bugs. The question: when an LLM tries to repair an
LLVM compiler bug, does giving it an **LLVM-IR-aware, semantically minimized
and structured** rendering of an Alive2 formal-verification counterexample
produce better repairs than raw or generically-reduced feedback? The project
does not build a new repair agent — it forks the existing
`llvm-apr-benchmark` baseline loop and swaps in one thing: how the failure
feedback is represented before it goes back to the model. Full framing:
`context.md`.

---

## 2. What has been accomplished

### Engineering — done and tested

- Full `ce/` toolkit: Alive2 output parser, an LLVM IR model (parse/edit/print,
  byte-exact round-trip), an oracle (re-verifies every proposed edit against
  Alive2), three reducers — generic (text ddmin), `llvm-reduce`-backed, and
  the IR-aware reducer (the actual research contribution) — a structured
  feedback renderer, and a 9-condition feedback grid.
- 63 automated tests passing (unit + integration, the latter needing Alive2).
- IR model validated byte-exact against all 1,462 real reproducers in the
  benchmark dataset.
- 24-bug stratified experiment sample selected and committed
  (`data/experiment_sample.json`), drawn from the 100 usable bugs (Alive2-checked,
  single-function-fix miscompilations) out of 491 total bugs in the dataset.
- `opt` built and build-verified for all 24 sample bugs plus the excluded
  bootstrap bug `115575` — confirms the build/verify pipeline works at the
  scale the real experiment needs, not just for one bug.
- Experiment runner (`examples/repair_experiment.py`) and result aggregator
  (`examples/summarize_results.py`) written and working.
- Statistical design preregistered *before* any run data existed
  (`docs/ANALYSIS_PLAN.md`): repeat count *k*=3, McNemar's exact one-sided
  test with Benjamini-Hochberg correction over 4 primary comparisons.
- Model selected: `Qwen/Qwen2.5-Coder-14B-Instruct` (see §4 — forced down from
  the originally-preferred 30B model by shared-GPU memory constraints).

### What the one available "result" shows (mechanism demo, not science)

On a single bundled hand-built example (28 LLVM-IR instructions):

| condition | instructions after | reduction | oracle calls |
|---|--:|--:|--:|
| raw | 28 | — | — |
| generic (text) | 28 | 0% | 183 |
| llvm-reduce | 5 | 82% | 351 |
| **iraware (ours)** | **4** | **86%** | **17** |

This shows the reducer mechanism works and is far cheaper than the baselines.
It says nothing about whether it helps an LLM actually fix more bugs — that
requires the real repair-rate sweep, which has not produced usable data yet.

### 16 blockers hit and resolved during setup (full log: `docs/IMPLEMENTATION.md` §11)

Highlights: no `opt` build existed at all (B1) → resolved bug-by-bug; picked
efficiency metric as iteration count, not tokens/time (B2); picked the 24-bug
sample (B3); banned benchmark-leaderboard-style claims due to training-data
contamination (B4); added a second, IR-valid-but-counterexample-blind
baseline reducer to avoid a strawman comparison (B5); corrected which
existing system this actually builds on (B6); fixed silent result-overwrite
between ablation and main runs (B7); corrected stale dataset counts (B8);
found and fixed an OOM in parallel LLVM builds (B9); decided *k* and the
statistical test (B10); picked and then re-picked the model twice under real
GPU-memory constraints (B11, B12); two rounds of vLLM KV-cache/context-length
misconfiguration that silently truncated conversations (B13, B14); found the
IR-aware reducer no-ops on some counterexamples where a smaller violating
version demonstrably exists (**B15 — still OPEN**); and, most recently,
**discovered the first full sweep attempt (216 runs, the whole easy tier)
fixed zero bugs because the model returned the source code back unchanged on
64.5% of turns** (B16 — root-caused to two upstream harness issues: a hunk
window that could open/close mid-statement, and a prompt more strongly
weighted toward "reproduce this exactly" than "fix this" — both now fixed,
those 216 runs discarded for repair-rate purposes).

### Where things stand right now

- No valid repair-rate numbers exist yet. The only completed sweep (216 runs)
  was invalidated by Blocker 16 and discarded (kept only as reducer-behavior
  data).
- Blocker 16's fixes pass unit tests but are **not yet re-validated against a
  real pilot run**.
- Blocker 15 (IR-aware reducer no-ops on ~2 of 8 observed real counterexamples)
  is still open; a decision rule is preregistered (≤2 of first ~10 bugs
  no-op → proceed and note as a limitation; ≥4 → stop and fix) but hasn't
  been evaluated yet because the sweep hasn't restarted.
- Current model is `Qwen2.5-Coder-14B-Instruct`, not the originally
  preregistered `Qwen3-Coder-30B-A3B-Instruct` — forced by a shared H100
  tenant leaving only ~20GB free (see §4). A recent note (`SLM_SELECTION.md`)
  flags a possible `Qwen3-27B`-Coder variant as worth investigating if it
  turns out to exist, as a way back toward a stronger model that still fits.

### Immediate next step

Re-run the nine-condition pilot on bug `115575` (excluded from the sample, so
it costs no sample data) under the Blocker-16-fixed harness, to confirm the
no-op rate actually dropped, **before** committing another multi-day sweep.

---

## 3. Compute / resource requirements

### GPU

- **1× NVIDIA H100 (80GB)**, currently **shared** with another tenant that
  occupies ~60GB, leaving only ~20GB free and fluctuating. This single fact
  has already forced two model downgrades and two rounds of vLLM
  out-of-memory debugging (Blockers 12–14).
- Model served locally via vLLM, FP8 quantized, `--enforce-eager` (CUDA graph
  capture doesn't fit in the remaining memory budget).
- Current working config: `Qwen2.5-Coder-14B-Instruct`, ~14GB FP8 weights,
  `--max-model-len 8192–12288`, `--gpu-memory-utilization 0.24–0.25`.
- Originally preferred model (`Qwen3-Coder-30B-A3B-Instruct`, ~31GB FP8) needs
  a card that is exclusively theirs or has substantially more free headroom;
  it does not fit today.
- Inference itself is **not** the bottleneck — under 1% of total sweep wall
  time.

### CPU / build machine

- The real bottleneck: **every repair iteration is a full or incremental
  LLVM build** (minutes, not seconds).
- LLVM builds run in Docker (`docker-compose.yml`, 10GB container memory cap)
  or natively (`RUNBOOK_NATIVE.md`). `--build-jobs` must stay well below
  `os.cpu_count()` (currently capped at 4) or the compiler gets OOM-killed
  mid-build — confirmed the hard way (Blocker 9).
- `ccache` needs a large cache (recommended 250GB, was found under-sized at
  40GB) to make bug-to-bug switching affordable — 24 different LLVM
  `base_commit`s otherwise evict each other's cached objects.
- Per-bug fresh-build time observed: **18 minutes to 2h49m**, ~20 hours total
  across 24 bugs (measured directly).
- Build artifacts (`llvm-build`, `ccache`) are **per-machine and do not
  transfer** — any new machine repeats this build cost from scratch.

### Other infrastructure

- LLVM source checkout + build toolchain (cmake/ninja).
- Alive2 (`alive-tv`) for formal verification — available in the Docker
  image.
- `llvm-reduce` binary (second baseline reducer).
- The `llvm-apr-benchmark` dataset (491 bug JSONs, checked out unmodified).

### Scale of the pending experiment

| run set | size |
|---|--:|
| Main sweep: 24 bugs × 9 conditions × 3 repeats | 648 runs |
| Ablation (`--no-promotion`): 24 bugs × 2 conditions × 3 repeats | 144 runs |
| **Total** | **792 runs** |

Each run is up to 4 LLM turns, and **each turn is a build + the bug's full
lit-test directory**, not just an LLM call. The project's own estimate: 648
runs alone is "already ~two weeks of rebuilds" at current build-machine
settings; the full 792-run set (main + ablation) is proportionally more.
Minutes-per-iteration has not yet been measured on the fixed harness — that
is exactly what the pending pilot run is for, and the docs explicitly say not
to schedule the full sweep from a guess.

Preregistered fallback if the measured cost makes 792 runs infeasible, in
order: drop the smaller ablation (-72 runs); drop repeat count to 2 for
non-primary condition pairs only; reduce max iterations from 4 to 3 uniformly.
Dropping bugs from the 24-bug sample is explicitly ruled out (n=24 is already
the binding constraint on statistical power).

---

## 4. Cost estimate

**No cloud GPU rental or paid LLM API costs are recorded in the project's own
docs** — the model is open-weight (`Qwen2.5-Coder-14B-Instruct`, Apache 2.0)
and self-hosted via vLLM on an H100 the team already has (shared) access to,
so there is no metered API spend, and the docs never state a rental cost.

**Rough cloud-equivalent estimate**, for planning purposes if this had to be
rented rather than using existing shared access (not stated anywhere in the
project's docs — computed here from public on-demand H100 rates, roughly
$2–3/hr as of 2026 on providers like Lambda, RunPod, or Vast.ai):

| scenario | wall time | @ $2/hr | @ $2.50/hr | @ $3/hr |
|---|--:|--:|--:|--:|
| Main sweep only (648 runs, "~2 weeks" per project's own estimate) | ~336 hrs | ~$670 | ~$840 | ~$1,010 |
| Main + ablation (792 runs, proportionally more) | ~400–450 hrs | ~$800–900 | ~$1,000–1,125 | ~$1,200–1,350 |

Caveats on this estimate:
- It assumes a **dedicated** H100 with no other tenant contending for
  memory/compute — the project's actual card is shared, which is a large part
  of why it has been slower and more constrained than planned.
- It only covers GPU time for inference. It does not include the ~20 hours of
  one-time LLVM build time already spent building `opt` for all 24 bugs, nor
  the CPU/RAM cost of a build machine capable of running dozens of LLVM
  compiles — that's a separate, non-GPU resource this estimate doesn't price.
- The actual per-iteration time is still unmeasured on the current
  (Blocker-16-fixed) harness; the "~2 weeks" figure predates that fix and
  should be treated as an order-of-magnitude planning number, not a
  commitment, until the pending pilot run confirms it.

---

## 5. GPU sizing for an effective SLM (not just "what fits")

The project's own memory-constrained downgrades (Blockers 11–14) were a
consequence of sharing a card, not of the models being inherently too big.
For clean, unconstrained runs:

| model | class | FP8 weights | bf16 weights | minimum GPU to run comfortably |
|---|---|--:|--:|---|
| Qwen2.5-Coder-14B-Instruct (**current**) | dense 14B | ~14GB | ~28GB | any single 24GB+ card (RTX 4090, L40S, A100/H100) |
| Qwen3-27B-Coder (if it exists — **investigate**, §1) | dense 27B | ~27GB | ~54GB | single 40GB+ card exclusively, or 80GB shared |
| Qwen3-Coder-30B-A3B-Instruct (**originally preferred**) | MoE, 30B/~3.3B active | ~31GB | ~61GB | single 80GB card (A100/H100), largely exclusive |

**Bottom line:** a single **80GB H100 or A100 with exclusive access** (not
shared, as the current one is) is enough to run the originally-preferred
30B-A3B model at full context (262K, though this project only needs a few
thousand tokens per turn) with headroom for KV cache and CUDA graphs —
removing the entire chain of Blockers 12–14. A 40–48GB card (A6000, L40S,
RTX 6000 Ada) is enough for the 14B model currently in use, or the 27B
candidate, with room to spare. Going below ~24GB VRAM is not recommended for
this task class: the related-work review (`docs/SLM_SELECTION.md` §3) found
that models under ~5B parameters hit a floor effect on LLVM middle-end bugs
specifically (as opposed to easier synthetic benchmarks like QuixBugs), so
"smaller GPU, smaller model" trades directly against getting any usable
signal out of the sweep at all.

Inference itself is cheap regardless of GPU choice — under 1% of total sweep
wall time. The GPU spec matters for *which model fits*, not for *how fast the
sweep runs*; the LLVM build machine (§3) is what determines wall-clock time.

---

## 6. API cost if using a hosted LLM instead of self-hosting

The project currently self-hosts an open-weight model (no metered spend). If
that were swapped for a paid hosted API, here is what it would actually cost
— **and it turns out to be small, because this harness's per-bug budget is
lean (≤4 turns, one localized code window per turn), not the 500-round /
5M-token-per-issue budget the related-work papers use for frontier-model
agentic harnesses (`docs/SLM_SELECTION.md` §2.1).**

**Token volume**, from the project's own estimate
(`docs/SLM_SELECTION.md`: "the sweep needs ~2,600 generations"):

| run set | generations (≤4 turns each) |
|---|--:|
| Main sweep (648 runs) | ~2,600 |
| + ablation (144 runs) | ~3,176 total |

Conversation context grows each turn (the full prior reply is re-sent), up to
the ~8K–12K token window this project measured as sufficient
(`docs/IMPLEMENTATION.md` Blocker 14). Using a conservative per-turn average
of **~4,000 input tokens** and **~600 output tokens** (rewritten C++ hunk),
full-experiment (792 runs) volume is roughly:

- **~13M input tokens, ~2M output tokens** (upper-bound estimate — many runs
  stop early on a fix or a rejected no-op, so real usage will likely be
  lower).

**Cost at current (September 2026) list prices**, full 792-run experiment:

| tier | example models | $/1M in · $/1M out | estimated cost |
|---|---|---|--:|
| Cheap (SLM-equivalent) | Claude Haiku 4.5 · GPT-5.6 Luna | $1/$5 · $0.20/$1.20 | **~$5–$23** |
| Mid | Claude Sonnet 5 · GPT-5.6 Terra | $2/$10 · $2/$12 | **~$46–$50** |
| Frontier (used in the related-work papers, §2.1) | Claude Opus 5 · GPT-5.6 Sol | $5/$25 · $4/$20 | **~$92–$115** |

Sources: [Claude API pricing](https://platform.claude.com/docs/en/about-claude/pricing), [OpenAI GPT-5.6 pricing coverage](https://www.layer3labs.io/guides/gpt-5-6-api-pricing).

**Read this carefully — it is not an apples-to-apples swap:**

- These numbers assume this project's *own* lean 4-turn harness, just with a
  paid API in place of the self-hosted model. A frontier model run this way
  would likely score better (per `agentic_harness`'s numbers, §2.1 there),
  but that is a different, untested harness shape.
- If instead you wanted to reproduce something closer to `agentic_harness`'s
  own evaluation — full agentic scaffolding, up to 500 chat rounds, a
  **5 million token budget per issue** — the cost model changes completely:
  even at the cheapest frontier rate above, 5M tokens on a single issue is
  itself on the order of $10–$50, and this project's dataset has 100 usable
  bugs — i.e., potentially **thousands of dollars** for a single full pass,
  before any repeats. That is a different experiment design, not a pricing
  variant of this one.
- **The LLVM rebuild time, not the token cost, remains the dominant expense**
  either way — swapping to a paid API does not remove the ~2-week,
  build-bottlenecked wall-clock cost described in §3.

---

## 7. Key open risks

1. **No validated repair-rate data yet.** Everything scientific still depends
   on a clean pilot run and then the full sweep completing.
2. **Blocker 15 (IR-aware reducer no-ops on some counterexamples) is open**
   and could bias the sweep toward a null result if it occurs on ≥4 of the
   first ~10 real bugs measured.
3. **Model is weaker than originally planned** (14B vs. the preregistered
   30B), due to shared-GPU memory, which increases floor-effect risk (the
   related-work review found frontier models already lose 35–83% of their
   resolution rate moving from general software bugs to LLVM middle-end
   bugs — a 14B model sits well below that).
4. **~2-week-plus wall-clock commitment**, bottlenecked by LLVM rebuilds, not
   GPU inference — shared build-machine and shared-GPU contention could both
   extend this further.
