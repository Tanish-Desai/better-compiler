# Runbook: running the sweep on the H100 server

**What this is:** the end-to-end procedure for actually running the experiment,
from a bare server to a results table. It assumes no prior knowledge of the
repo's internals — follow it top to bottom.

**Companion documents:** [`IMPLEMENTATION.md`](IMPLEMENTATION.md) explains what
the experiment *is*. [`SLM_SELECTION.md`](SLM_SELECTION.md) explains why this
model. [`ANALYSIS_PLAN.md`](ANALYSIS_PLAN.md) fixes *k* and the statistical
test. This file is only about operating the thing.

**No Docker access on your server?** Some hosting setups grant container
permissions but not the ability to start a Docker daemon inside them (no
`--privileged`, no `docker.sock`). If `docker compose up` isn't an option for
you, use [`RUNBOOK_NATIVE.md`](RUNBOOK_NATIVE.md) instead — same experiment,
same reasoning, installed directly rather than through an image.

---

## 1. The decision, up front

**Everything runs on the H100 server. The laptop runs nothing.**

| | laptop | H100 server |
|---|--:|--:|
| logical cores | 16 | **96** |
| RAM installed | 47 GB | **1024 GB** |
| RAM actually free | ~22 GB | — |

The temptation is to split it: bugs on the laptop, model on the server, HTTP in
between. That works — the runner is an OpenAI-compatible client and only needs
a URL — but it optimises the wrong resource. [`ANALYSIS_PLAN.md`](ANALYSIS_PLAN.md)
§5 is blunt: *the dominant cost is not the model.* Inference is well under 1% of
this sweep's wall time; the LLVM rebuilds are all the rest. Splitting would pin
the 99% to the slowest machine to keep the 1% company.

The binding constraint on the laptop is not the core count, it is the free RAM.
Blocker 9 forced `--build-jobs 4` there because the container cap had to stay
at 10 GB to protect the host. The server can give the container 256 GB and
build with 64 jobs.

Splitting also buys a failure mode: every LLM call crosses a network, and a
dropped connection mid-run silently records a repair failure that never
happened. On one box that risk is close to gone.

**The laptop's job from here is editing code and reading results.** Nothing in
this runbook runs on it.

---

## 2. What actually runs

Two processes, side by side on the same machine:

```
   H100 server
   ┌─────────────────────────────────────────────┐
   │                                             │
   │   vLLM  ──────────── serves the model       │
   │   (uses the GPU)     on port 8000           │
   │        ▲                                    │
   │        │ HTTP, localhost                    │
   │        │                                    │
   │   Docker container ── builds LLVM,          │
   │   (uses the 96 CPUs)  runs the experiment   │
   │                                             │
   └─────────────────────────────────────────────┘
```

**vLLM** is a web server with a model behind it. Start it once, leave it up for
the whole sweep. GPU-bound, almost no CPU.

**The container** is this repo. It runs the repair loop, which for each bug
does: ask the model for a patch, apply it, rebuild LLVM, run the tests, and if
it still fails, feed the reduced counterexample back and ask again. Up to 4
turns. CPU-bound, no GPU.

They never contend for the same resource, which is exactly why putting them on
one box is free.

That loop, 792 times, is the experiment.

---

## 3. Step by step

### Step 1 — Prerequisites on the server

You need Docker, git, and a Python environment for vLLM. Nothing else:
`alive-tv`, the LLVM clone and the bug dataset are all baked into the image by
the [`Dockerfile`](../Dockerfile).

```bash
docker --version          # any recent version
nvidia-smi                # confirm the H100 is visible
nproc                     # expect 96
free -g                   # expect ~1000 total
```

### Step 2 — Clone the repo

```bash
git clone <repo-url> better-compiler
cd better-compiler
git checkout feat/h100-sweep
```

### Step 3 — Turn on the H100 configuration

```bash
cp .env.h100 .env
```

**Do not skip this, and do not do it on the laptop.** It is what makes every
other command in this file work as written. `docker compose` reads `.env`
automatically; that one file sets `COMPOSE_FILE` so the H100 overlay
([`docker-compose.h100.yml`](../docker-compose.h100.yml)) layers on top of the
base config, and supplies the four `LAB_LLM_*` variables to the container.

Without it you get laptop-sized memory limits, no route from the container to
vLLM, and no model configuration.

Read [`.env.h100`](../.env.h100) once — it is short and every line is
commented. If you are **sharing this server with other people**, also lower
`mem_limit` in the overlay; 256 GB is sized for having the box to yourself.

### Step 4 — Build the image and check it works

```bash
docker compose up -d --build
docker compose exec better-compiler python3 -m pytest tests -q
```

The first build is slow (it compiles Alive2 and clones LLVM). The tests are
fast and prove the `ce` package works before anything expensive starts.

### Step 5 — Give ccache room

```bash
docker compose exec better-compiler ccache -M 250G
```

Small command, large effect. The 24 bugs sit at 24 different LLVM commits, and
at the old 40 GB the cache was evicting entries it was about to need again.
[`ANALYSIS_PLAN.md`](ANALYSIS_PLAN.md) §5 calls this "the single cheapest change
to the sweep's wall time."

### Step 6 — Start the model server

vLLM needs the GPU directly, so it runs on the **host**, not in the container.

```bash
tmux new -s vllm

pip install vllm          # in a venv. First run downloads ~60GB of weights.

vllm serve Qwen/Qwen3-Coder-30B-A3B-Instruct \
    --served-model-name qwen3-coder-30b \
    --quantization fp8 \
    --max-model-len 32768 \
    --gpu-memory-utilization 0.9 \
    --host 0.0.0.0 --port 8000 \
    --api-key local-sweep
```

Detach with `Ctrl-b d`. Leave it running.

**Use tmux (or screen).** This has to outlive your SSH session by days. If the
connection drops and takes vLLM with it, the sweep goes down too.

Lower `--gpu-memory-utilization` to `0.55` if you are sharing the card.
`--served-model-name` and `--api-key` must match `LAB_LLM_MODEL` and
`LAB_LLM_TOKEN` in your `.env`.

### Step 7 — Preflight the endpoint

```bash
docker compose exec better-compiler python3 scripts/check_llm_endpoint.py --repeat 3
```

Thirty seconds that can save you four days. It proves two things:

1. The container can reach vLLM at all.
2. Three identical prompts give three **different** answers.

If the three replies come back identical, temperature is not being applied —
and *k* = 3 is then buying three copies of one run at three times the cost.
[`SLM_SELECTION.md`](SLM_SELECTION.md) §8 is emphatic about this. Fix it before
going further.

### Step 8 — The pilot: nine runs, one bug

```bash
docker compose exec better-compiler python3 examples/repair_experiment.py \
    --out results/pilot 115575 \
    --build-jobs 64 \
    --condition baseline raw-plain generic-plain llvmreduce-plain iraware-plain \
                raw-structured generic-structured llvmreduce-structured \
                iraware-structured
```

Bug `115575` is deliberately **outside** the 24-bug sample, so this costs no
experimental data.

This is the first time the full pipeline runs end to end. Read four things off
it ([`SLM_SELECTION.md`](SLM_SELECTION.md) §9):

- Does the loop complete at all?
- How often does `apply_patch` fail? LLVM-Bench found patch invalidity to be a
  dominant failure mode, so a high rate here is a harness problem, not a result.
- **Minutes per iteration.** This is the number nobody has yet, and the only
  honest basis for scheduling the sweep.
- Does the model engage with the counterexample, or ignore it?

**Time this step and multiply by 792.** Do not schedule from a guess.

### Step 9 — The sweep

```bash
tmux new -s sweep

docker compose exec better-compiler python3 examples/repair_experiment.py \
    --sample --repeat 3 --out results/ \
    --build-jobs 64 \
    --condition baseline raw-plain generic-plain llvmreduce-plain iraware-plain \
                raw-structured generic-structured llvmreduce-structured \
                iraware-structured
```

Detach with `Ctrl-b d` and leave it for days.

`--sample` reads the 24 bug ids from `data/experiment_sample.json`, so the
sweep and the committed sample cannot drift apart. Passing all nine conditions
to **one** invocation is what makes this affordable — the runner then iterates
bug-major, paying the expensive commit switch 24 times instead of 216·*k*.

**Always pass `--build-jobs` explicitly.** The default is `os.cpu_count()`,
which on this box is 96. That is survivable at `mem_limit: 256g`, but it is
antisocial on a shared machine and it is not what anyone measured.

**It is safe to interrupt.** Every finished cell writes its own JSON file and
re-running skips whatever is already on disk, so you can stop and resume
freely. Use `--overwrite` only when you deliberately want a cell redone.

### Step 10 — Read the results

```bash
docker compose exec better-compiler python3 examples/summarize_results.py results/
docker compose exec better-compiler python3 examples/analyze_significance.py results/
```

`results/` is bind-mounted, so the run records are on the host filesystem and
survive the container. Copy them back to the laptop to analyse.

Report the discordant counts and rate differences whatever the p-values say —
at n = 24 the counts carry more information than the p-value does
([`ANALYSIS_PLAN.md`](ANALYSIS_PLAN.md) §2).

---

## 4. While it runs

**Check daily:**

```bash
tmux attach -t sweep              # progress: [n/792] lines
docker stats --no-stream          # container memory against the 256g cap
nvidia-smi                        # vLLM still alive
ls results/*.json | wc -l         # cells completed
```

**Check for silent infrastructure failures.** An LLM error that hits mid-run
truncates that run and records it as a failure. The retry guard in
`Model.chat` (3 attempts, 30s then 120s backoff) covers a vLLM restart, but if
one gets through it leaves a fingerprint:

```bash
grep -l llm_error results/*.json
```

Anything listed there is an infrastructure artefact, not a result. Delete those
records and re-run with `--overwrite`.

**If the sweep is going to take too long,** the preregistered order for cutting
scope is in [`ANALYSIS_PLAN.md`](ANALYSIS_PLAN.md) §5: drop ablation A2 first,
then `--repeat 2` on non-primary pairs, then `--max-iterations 3` uniformly.
**Do not drop bugs from the sample** — n = 24 is already the binding constraint
on statistical power.

---

## 5. Troubleshooting

| symptom | cause | fix |
|---|---|---|
| `Connection error` on every LLM call | `.env` not copied, so no `extra_hosts` | Step 3, then `docker compose up -d` to recreate |
| `model ... not found` | `LAB_LLM_MODEL` differs from `--served-model-name` | make them match; `check_llm_endpoint.py` lists what vLLM actually serves |
| `c++: fatal error: Killed signal terminated program cc1plus` | build OOM | lower `--build-jobs`; see Blocker 9 in [`IMPLEMENTATION.md`](IMPLEMENTATION.md) |
| three identical replies at `--repeat 3` | temperature not applied | confirm it reached the container: `docker compose exec better-compiler env` and look for `LAB_LLM_TEMP` |
| sweep died when SSH dropped | not in tmux | Steps 6 and 9 |
| `Device or resource busy` from `git clean` | build dir inside the LLVM tree | it is mounted outside on purpose — do not move it; see [`docker-compose.yml`](../docker-compose.yml) |

---

## 6. Things that are true and surprising

**The 20 hours of builds already done on the laptop do not transfer.** The
`llvm-build` and `ccache` volumes are per-machine — [`docker-compose.yml`](../docker-compose.yml)
says so in its own comments, and Blocker 9 repeats it. The server rebuilds all
24 from scratch. That is expected, not a mistake, and on 96 cores it is far
cheaper than it was on 16.

**Wall time is genuinely unknown.** The only hard datum is ~20 hours for 24
from-scratch builds at `--build-jobs 4` on the laptop. The sweep's real cost is
the ~2,600 *incremental* rebuilds inside the loop, which nobody has timed. That
is what Step 8 is for.

**A 0% sweep is a bug, not a finding.** `agentic_harness` reports frontier
models collapsing on this exact task; expect single-digit to low-double-digit
repair rates. Zero across every condition means the harness or the model is
broken — check `apply_patch` failures first.
