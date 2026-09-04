# Model Selection

**What this decides:** which open-weight model drives `examples/repair_experiment.py`
for the condition sweep, and why.

**Written:** 2026-09-04, from `slm_research_papers/` (11 PDFs), `ref_papers/`
(7 PDFs), and current model availability. Every number attributed to a paper
was read out of that paper; numbers attributed to vendor pages or aggregator
sites are marked as claimed, because we have not reproduced them.

**Companion documents:** `docs/ANALYSIS_PLAN.md` fixes *k* and the statistical
test. `docs/METHODOLOGY.md` states what the results can support. This file is
only about the model.

**Operational note (2026-09-04):** the analysis below still ranks
`Qwen3-Coder-30B-A3B-Instruct` first, and that ranking is unchanged — it
assumes a card that's yours alone or close to it. The actual H100 turned out
to have a standing tenant leaving only ~20GB free, below that model's ~31GB
FP8 footprint, so the sweep is currently running `Qwen2.5-Coder-14B-Instruct`
instead (§4's own fit table, just not the row this document originally
picked). See Blocker 12 in [`IMPLEMENTATION.md`](IMPLEMENTATION.md) for the
numbers and the serving command actually in use, and the runbooks
([`RUNBOOK.md`](RUNBOOK.md), [`RUNBOOK_NATIVE.md`](RUNBOOK_NATIVE.md)) for how
to tell whether that still applies to your machine. §8 below is the original
command, correct for whoever actually gets the exclusive access this analysis
assumed.

---

## 1. The decision, up front

| role | model | licence | why |
|---|---|---|---|
| **Primary** | `Qwen/Qwen3-Coder-30B-A3B-Instruct` | Apache 2.0 | Strongest open coder that is also *fast*: 30B total, ~3.3B active per token. The sweep needs ~2,600 generations, and a sparse model makes that cheap. Fits an H100 in bf16 and leaves half the card free in FP8. |
| **Secondary** (model-sensitivity check only) | `mistralai/Devstral-Small-2-24B-Instruct-2512` | Apache 2.0 | Different vendor, different data, dense 24B. Used to check the finding is not a Qwen artefact — not to double the sweep. |
| **Fallback** (contamination-clean, weaker) | `Qwen/Qwen2.5-Coder-32B-Instruct` | Apache 2.0 | Released 2024-11; its training data predates the fixes for 10 of the 24 sample bugs. Buys a cleaner story at the cost of capability. |
| **Rejected** | 1–4B tier (Phi-4-mini, Qwen2.5-Coder-3B, Gemma-3-4B) | — | Floor effect. See §3 — this is the single most important thing in this document. |
| **Rejected** | `facebook/llm-compiler-{7b,13b}` | bespoke, not OSI | Section 5. |
| **Rejected** | Codestral | non-commercial | Not free/open by the criterion you set. |

**Run the sweep with one model.** 648 runs is already ~two weeks of rebuilds
(`docs/ANALYSIS_PLAN.md` §5). A second model doubles that and answers a
question nobody asked. Use Devstral only if a reviewer challenges
model-specificity, and then only on the four primary comparisons.

---

## 2. What the papers say

### 2.1 The two papers that matter most

These two measure the *exact* task — LLMs repairing LLVM middle-end bugs — and
they are the reason the rest of the SLM literature has to be read carefully.

#### `slm_research_papers/agentic_harness.pdf`
**Agentic Harness for Real-World Compilers** (Zheng, Li, Li, Zhang, Su;
arXiv:2603.20075v2, 2026). Same author as `llvm-apr-benchmark`, which this repo
builds on — this is the closest existing work there is.

- Benchmark: `llvm-bench`, 334 reproducible LLVM middle-end bugs; crashes and
  miscompilations; easy 76.3% / medium 13.2% / hard 10.5%.
- Five models evaluated: GPT 5, Gemini 2.5 Pro, DeepSeek V3.2, Qwen 3 Max,
  GPT 4o. Temperature 0, 64K context, **5 million token budget per issue**,
  up to 500 chat rounds.
- **Frontier models collapse on compiler bugs.** Moving from SWE-bench Verified
  to `llvm-bench live` costs each model 35–83% of its resolution rate
  (average −60.2%). Gemini 2.5 Pro: 53.6% → 9.2%. Qwen 3 Max: 69.6% → 24.4%.
  DeepSeek V3.2 holds up best at 60.0% → 38.9%.
- With their specialised agent, best is GPT 5 at 51.5% (118/229).
- **Miscompilations are harder than crashes** (their Figure 4). Our sample is
  100% miscompilations.
- By difficulty, `mini-SWE-agent` averages 23.2% / 15.8% / 6.1% for
  easy / medium / hard.

> **What we take from it:** the ceiling for this task is low even for frontier
> models with enormous budgets. Our harness gives 4 turns and a located hunk —
> far less scaffolding, but a much easier task shape. Expect single-digit to
> low-double-digit repair rates, and treat a 0% sweep as a harness or model
> problem, not a result.

#### `slm_research_papers/llvm_bench.pdf`
**LLVM-Bench** (Tian, Zhao, Suo, Wang, Chen; arXiv:2607.00700v1, 2026).
423 validated LLVM issue-resolution tasks.

- Open-source models chosen as representatives: **DeepSeek v3.2** and
  **qwen3-coder-plus**. Commercial: gemini-3-flash, grok-code-fast-1.
- Retrieval-augmented LLMs: **below 5%** resolution. Agents: **below 11%**.
  Their ensemble (`LLVM-Ens`): **21.99%**.
- Dominant failure modes: **patch invalidity and build failures** — not subtle
  wrong logic.
- Temperature 0, max generation 8,192 tokens.
- Their own threat-to-validity notes SWE-agent + Gemini-3-Flash scores 78% on
  SWE-bench Verified but **4.26%** on LLVM-Bench.

> **What we take from it:** (a) Qwen-family models are the accepted open-source
> representative for this task, which supports the primary pick. (b) "Patch
> invalidity and build failures dominate" is a warning about our own harness:
> `apply_patch` returns False when the model does not echo the hunk verbatim,
> and that failure mode will eat iterations. Watch it in the pilot.

### 2.2 The SLM papers

#### `slm_research_papers/how_small_is_enough.pdf`
**How Small is Enough?** (Kusama, Shu, Kondo, Kamei; arXiv:2508.16499v1, 2025).
14 SLMs, 23 models from 13 architectures, on QuixBugs.

- Best SLMs — **Phi-3 (3.8B)** and **Qwen2.5-Coder-3B-Instruct** — each fix
  **38/40**, against Codex's 39/40.
- Nine of 14 SLMs beat GPT-NeoX (20B).
- **int8 quantization costs ~0.25 bugs; fp16 costs nothing.** GPTQ.
- **Code-specialised beats general at equal size**, decisively: Code Llama 37
  vs Llama2 23. Chat-tuning *hurts*: Vicuna scores 19 below Llama2.
- Their SLM definition: fits in <24GB VRAM.
- Sampling: 200 samples per bug, top-p 0.95; a bug counts fixed if any sample
  passes.
- Their own threat-to-validity flags QuixBugs data leakage.

> **What we take from it:** two usable rules — prefer a code-specialised model
> over a general one at the same size, and **quantization is close to free**, so
> FP8 on a shared H100 is not a compromise. The headline result does *not*
> transfer; see §3.

#### `slm_research_papers/slm_as_a_judge.pdf`
**Improving Code Generation via SLM-as-a-judge** (Crupi, Tufano, Bavota;
arXiv:2602.11911v1, 2026). Defines SLM as <5B parameters.

- Generators: DeepSeek Coder 1.3B, OpenCoder 1.5B, Qwen2.5 Coder 3B,
  Phi-4-mini, Gemma-3 4B. Judges: fine-tuned Qwen2.5-Coder 0.5B/3B,
  Gemma-3 4B, Llama-3.2 3B.
- **All SLMs are useless as zero-shot judges**; fine-tuning is what makes them
  work.
- **Generating 10 candidates and selecting beats 2 or 5, consistently across
  all five SLMs.** One judge is usually enough.
- Statistics: **McNemar's test with Benjamini-Hochberg** adjustment, over
  145 tasks × 10 repetitions.

> **What we take from it:** the strongest independent argument for *k* > 1 —
> repeated sampling is where small models get their wins. And the exact
> statistical apparatus we adopt (`docs/ANALYSIS_PLAN.md`).

#### `slm_research_papers/slm_fix.pdf`
**SLMFix** (Fu, Gupta, Councilman, Grove, Wang, Adve; arXiv:2511.19422v1, 2025).

- A **500M** model, RL-fine-tuned, is enough to fix *statically detected*
  errors (syntax, types) in LLM-generated DSL code; >95% static-validator pass.
- The framing is explicit: repair suits small models because "the model only
  needs to be able to make corrections, not generate the code from scratch."

> **What we take from it:** the tiny-model success stories are all *mechanical*
> repair — syntax and types, checkable statically. Fixing an unjustified `nsw`
> in `InstCombine` is root-cause reasoning over compiler semantics. Different
> task; do not import the size conclusion.

#### `slm_research_papers/repair_llama.pdf`
**RepairLLaMA.** Fine-tunes CodeLlama-7B and deepseek-coder-6.7b-base for APR.

- **LoRA beats full-parameter fine-tuning** — clearly, on Defects4J: 195 vs 146
  plausible, 144 vs 98 correct.
- Statistics: **McNemar for each pairwise combination of representations**,
  described as "binary outcomes of two representations evaluated on the same
  set of benchmark examples" — structurally identical to our design.
- Beats GPT-3.5 and GPT-4 on GitBug-Java (post-cutoff bugs), significantly.

> **What we take from it:** the statistical precedent, and a future direction
> (LoRA on LLVM patches) that is out of scope here.

#### `slm_research_papers/hej_robust(benchmark).pdf`
**HEJ-Robust** (Rabbi; arXiv:2605.02215v3, 2026). 1,450 instances from
HumanEval-Java-Bug under 8 semantics-preserving transformations.

- **Fine-tuned APR models lose 50.5–57.3% of pass@10** under local variable
  renaming alone.
- CodeBLEU stays flat throughout — it does not detect the damage.

> **What we take from it:** avoid task-fine-tuned APR models; they latch onto
> identifier patterns. General instruction-tuned coders are the safer choice
> for a study whose whole point is sensitivity to *prompt content*.

### 2.3 Compiler- and feedback-specific background

| paper | one line | bearing on this repo |
|---|---|---|
| `slm_research_papers/meta_compiler_repair.pdf` | **LLM Compiler** (Meta): Code Llama 7B/13B further-trained on **546B tokens of LLVM-IR and assembly**. 77% of autotuning's optimisation potential; 45% disassembly round-trip. | The only model family that genuinely *knows* LLVM IR. Rejected — see §5. |
| `ref_papers/chat_repair.pdf` | **Conversational APR**: interleave patch generation with validation, feeding failures back into the same conversation instead of resampling the same prompt. | This is precisely the loop `repair_experiment.py` runs. It is why feeding *better* failure text is a plausible lever at all. |
| `ref_papers/reducefix.pdf` | Reducing failure-inducing test inputs before prompting improves repair; long prompts trigger "lost in the middle". Validated with Mann-Whitney-Wilcoxon. | Establishes that reduction-helps-repair is known. Our contribution is *IR-aware* vs generic reduction, not reduction itself. |
| `ref_papers/pgs.pdf`, `ref_papers/delta-debugging.pdf` | Minimal-feedback and ddmin foundations. | Already implemented in `ce/reduce_generic.py`; background, not model selection. |
| `ref_papers/alive2-pldi21.pdf` | Alive2's refinement checking — the source of every counterexample here. | Background. |
| `slm_research_papers/llm_software_repair.pdf` | Survey of 66 repair systems. **pass@1 is the most common metric** and rising (1/12 systems in 2023 → 13/30 in 2025). Uses Fisher's exact for *unpaired* tag-outcome associations. | Confirms reporting pass@1 alongside pass@k, and confirms Fisher's role: unpaired data. Ours is paired. |
| `slm_research_papers/repair_agent.pdf`, `automated_program_repair.pdf` | Agentic repair; PEFT survey. | Background. |

---

## 3. Why the SLM literature's headline does not transfer

`how_small_is_enough` says a 3.8B model matches Codex. That result is real —
**on QuixBugs**: 40 programs, each with a single one-line bug, written to be
fixable by a human in under a minute, and given 200 samples per bug.

Our task, per bug:

- rewrite a ~60-line window of LLVM middle-end C++ (`InstCombine`,
  `SLPVectorizer`, `ScalarEvolution`, …),
- from an Alive2 refinement counterexample in LLVM IR,
- such that it builds, fixes the reproducer, **and** breaks none of the
  hundreds of existing lit tests in that pass's directory,
- in at most 4 turns.

`agentic_harness` measured what happens to this exact gap: models scoring
50–70% on ordinary software bugs score 9–39% on LLVM middle-end bugs. A 3B
model is not near the bottom of that range; it is off it.

**Why this matters more than "waste some GPU time":** the experiment compares
nine feedback formats. If the model fixes zero bugs under every condition, all
nine cells are identical and McNemar has no discordant pairs to test — the
sweep produces two weeks of compute and no measurable outcome. **A floor effect
is not a null result, it is a failed experiment.** The mirror risk — a model so
strong it fixes everything from the issue title alone — is not credible here
given the numbers above, so the selection rule is simply: *take the strongest
model that fits and runs fast enough.*

That is why "SLM" in this project should mean "an open-weight model that runs on
one H100", the `how_small_is_enough` definition scaled to the hardware you
actually have — not the <5B definition from `slm_as_a_judge`.

---

## 4. Fit on one H100 (80GB)

Weights only; add roughly 4–12GB for KV cache at 32K context and vLLM overhead.

| model | params | active | bf16 | FP8 | context | licence |
|---|--:|--:|--:|--:|--:|---|
| Qwen3-Coder-30B-A3B-Instruct | 30B | ~3.3B | ~61GB | ~31GB | 262K | Apache 2.0 |
| Devstral-Small-2-24B-Instruct-2512 | 24B | 24B | ~48GB | ~24GB | 256K | Apache 2.0 |
| Qwen2.5-Coder-32B-Instruct | 32B | 32B | ~64GB | ~32GB | 32K | Apache 2.0 |
| Qwen2.5-Coder-14B-Instruct | 14B | 14B | ~28GB | ~14GB | 32K | Apache 2.0 |
| gpt-oss-120b | 117B | ~5.1B | — | ~63GB (MXFP4) | 128K | Apache 2.0 |

**Because the card is sometimes occupied**, serve in FP8 and cap
`--gpu-memory-utilization`. `how_small_is_enough` measured int8's cost on repair
accuracy at about a quarter of a bug out of 40 — the memory headroom is worth
far more to you than that.

**On `gpt-oss-120b`:** it does fit a single 80GB card in MXFP4 and it is
Apache 2.0, so it is tempting as the strongest single-card open model. Two
reasons it is not the primary. It is a reasoning model, so every one of ~2,600
generations pays a long chain-of-thought — on a sweep this size that is days,
not minutes. And its output uses a channel format that `extract_code()` in
`repair_experiment.py` was not written for; a harness change to accommodate one
model is exactly the kind of thing that stops conditions being comparable.
Reconsider it only if the pilot shows Qwen3-Coder at the floor.

---

## 5. Why not LLM Compiler, given it is the one model that knows LLVM IR

`meta_compiler_repair.pdf` describes 7B and 13B models trained on 546B tokens of
LLVM IR — on paper, the perfect fit for reading Alive2 counterexamples. Three
reasons it is not usable:

1. **Wrong task.** It is trained for optimisation-flag prediction and
   disassembly, not for instruction-following C++ repair. Our loop needs a model
   that reads an issue title and returns a rewritten C++ hunk.
2. **Wrong licence.** Released under a bespoke commercial licence, not an OSI
   one. You asked for free and open source.
3. **Wrong size for the task's difficulty**, per §3.

It stays worth citing as related work: it is the strongest existing evidence
that LLVM IR is learnable as a first-class modality, which is the assumption
behind `ce/structured.py` presenting IR rather than prose.

---

## 6. Statistical precedent found in the papers

Collected here because it is a finding *from* this reading, and it decides
`docs/ANALYSIS_PLAN.md`.

| paper | test | design |
|---|---|---|
| `agentic_harness` | **one-sided McNemar, α = 0.05**, with the `#01 #10 #11 #00` matrix printed | two agents, same LLVM bugs |
| `repair_llama` | **McNemar**, every pairwise combination of representations | representations × same benchmark examples |
| `slm_as_a_judge` | **McNemar + Benjamini-Hochberg** | teams vs baselines, 145 tasks × 10 reps |
| `reducefix` | Mann-Whitney-Wilcoxon | continuous outcome |
| `llm_software_repair` | **Fisher's exact** | *unpaired* tag/outcome contingency |

Two conclusions:

- **McNemar, not Fisher.** Every paper testing paired binary repair outcomes on
  a shared bug set uses McNemar. Fisher's exact appears exactly once, for
  genuinely unpaired contingency data. Our nine conditions are run over the
  same 24 bugs, so the data are paired and Fisher would discard the pairing —
  which is the only thing making n = 24 workable at all.
- **Report the discordant counts.** `agentic_harness` prints the full 2×2 for
  every comparison. With samples this small the counts are more informative
  than the p-value, and `examples/analyze_significance.py` prints both.

---

## 7. Contamination, and why it is survivable

The 24 sample bugs span **2024-02-24 to 2026-02-11** (`knowledge_cutoff` in
each dataset entry). Any model worth using here was trained after most of those
dates, so its training data plausibly contains the upstream fix commits.

`docs/IMPLEMENTATION.md` Blocker 4 already settled the policy: **no
benchmark-legal claims, ever.** What is worth adding, from thinking about it
against this design:

**Contamination inflates every condition equally.** All nine conditions use the
same model on the same bugs and differ only in the feedback text. A memorised
fix helps `baseline` exactly as much as `iraware-structured`. So contamination
threatens the *absolute* repair rate — which we never claim — and not the
*between-condition differences*, which are the entire result. The within-subject
design is doing real work here.

The one way it does bite: memorisation pushes toward a **ceiling**, where every
condition fixes the same bugs and discordant pairs vanish. Watch for this in the
pilot — if `baseline` (no counterexample at all) matches `iraware-structured`,
suspect memorisation before celebrating.

`Qwen2.5-Coder-32B-Instruct` (2024-11) is listed as the fallback for anyone who
wants a cleaner story: it predates the fixes for the 10 bugs with a 2025-or-later
cutoff. It is a weaker model, which trades directly against the floor risk in §3.

---

## 8. Serving it

The runner is already an OpenAI-compatible client (`Model` in
`examples/repair_experiment.py`), so a local vLLM server is a drop-in — no code
change, only environment variables.

On the H100 host:

```bash
vllm serve Qwen/Qwen3-Coder-30B-A3B-Instruct \
    --served-model-name qwen3-coder-30b \
    --quantization fp8 \
    --max-model-len 32768 \
    --gpu-memory-utilization 0.55 \
    --host 0.0.0.0 --port 8000 \
    --api-key local-sweep
```

`--gpu-memory-utilization 0.55` is the concession to the card being shared;
raise it when you have the H100 to yourself. `--max-model-len 32768` is
generous — the longest condition (`raw-structured`) is a few thousand tokens.

Wherever `repair_experiment.py` runs:

```bash
export LAB_LLM_URL=http://<h100-host>:8000/v1
export LAB_LLM_TOKEN=local-sweep
export LAB_LLM_MODEL=qwen3-coder-30b
export LAB_LLM_TEMP=0.8            # MUST be > 0; see below
```

Then confirm it before starting a multi-day sweep:

```bash
python3 scripts/check_llm_endpoint.py
```

**Temperature must be greater than zero.** `docs/ANALYSIS_PLAN.md` sets *k* = 3,
and three trials at temperature 0 are three copies of the same answer — the
repeats would cost three times the compute and buy nothing. 0.8 with top-p 0.95
matches `how_small_is_enough`'s sampling setup. Note this differs deliberately
from `llvm_bench` and `agentic_harness`, which both use temperature 0 *because
they report pass@1 from a single sample* — the opposite trade.

**The LLVM builds, not the GPU, are the bottleneck.** Inference is well under 1%
of this sweep's wall time; `env.check_full()` is the rest. If the H100 host has
more cores and RAM than the current build machine, run the whole thing there and
rebuild `opt` for the 24 bugs on it — `docs/IMPLEMENTATION.md` Blocker 9 is
explicit that the build volumes do not transfer between machines. If it does
not, or if it is too contended for a multi-day job, leave the builds where they
are and point `LAB_LLM_URL` across the network; the runner only needs HTTP.

**Two build-machine settings to fix first**, whichever machine you use:

- `ccache -M 250G`. It is currently 40G and has already run 44 cleanups holding
  24 different `base_commit`s — it is evicting entries it is about to need, so
  every bug switch pays close to a full rebuild.
- `mem_limit` in `docker-compose.yml` (currently 10g) and `--build-jobs`
  (currently 4) both sized to the host. On a server-class box these are what
  turn an 11-day sweep into a shorter one.

---

## 9. Screen before you commit

Do not start 648 runs on an unvalidated model. Bug `115575` exists for exactly
this: it is build-verified, and it was deliberately **excluded** from the 24-bug
sample (`data/experiment_sample.json`'s `excluded` field), so using it costs no
sample data.

```bash
python3 examples/repair_experiment.py --out results/pilot 115575 \
    --condition baseline raw-plain generic-plain llvmreduce-plain iraware-plain \
                raw-structured generic-structured llvmreduce-structured \
                iraware-structured
```

Nine runs on an already-built commit. Read four things off it:

1. **Does the loop complete end-to-end?** LLM → patch → build → lit → Alive2 →
   reduced feedback → next turn. This has never run.
2. **How often does `apply_patch` fail?** `llvm_bench` found patch invalidity to
   be a dominant failure mode. If the model will not echo the hunk verbatim,
   iterations are being burned on formatting, not repair, and the prompt needs
   fixing *before* the sweep — not after.
3. **Minutes per iteration.** This is the one number that turns
   `docs/ANALYSIS_PLAN.md` §5's arithmetic into a real schedule. Nothing else
   currently measures it.
4. **Floor check.** Not "did it fix the bug" — one bug proves nothing either
   way. Look at whether the model engages with the counterexample at all, and
   whether the conditions produce visibly different attempts.

If the model is clearly at the floor, the fix is a bigger model or more
iterations, **not** a bigger *k*.

---

## 10. Sources

Papers, as filed in this repo:

- `slm_research_papers/agentic_harness.pdf` — arXiv:2603.20075v2
- `slm_research_papers/llvm_bench.pdf` — arXiv:2607.00700v1
- `slm_research_papers/how_small_is_enough.pdf` — arXiv:2508.16499v1
- `slm_research_papers/slm_as_a_judge.pdf` — arXiv:2602.11911v1
- `slm_research_papers/slm_fix.pdf` — arXiv:2511.19422v1
- `slm_research_papers/repair_llama.pdf`
- `slm_research_papers/hej_robust(benchmark).pdf` — arXiv:2605.02215v3
- `slm_research_papers/meta_compiler_repair.pdf` — arXiv:2407.02524v1
- `slm_research_papers/llm_software_repair.pdf`, `repair_agent.pdf`,
  `automated_program_repair.pdf`
- `ref_papers/` — `alive2-pldi21.pdf`, `chat_repair.pdf`, `reducefix.pdf`,
  `pgs.pdf`, `delta-debugging.pdf`, `finding-bugs-compilers.pdf`,
  `agentic_harness.pdf`

Model cards and vendor pages (claimed figures, not reproduced here):

- <https://huggingface.co/Qwen/Qwen3-Coder-30B-A3B-Instruct>
- <https://huggingface.co/mistralai/Devstral-Small-2-24B-Instruct-2512>
- <https://mistral.ai/news/devstral-2-vibe-cli/>
- <https://huggingface.co/Qwen/Qwen2.5-Coder-32B-Instruct>
- <https://huggingface.co/openai/gpt-oss-120b>
- <https://huggingface.co/blog/welcome-openai-gpt-oss>
