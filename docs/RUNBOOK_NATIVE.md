# Runbook: running the sweep without Docker

**Already set up and just need to run it?** [`OPERATING.md`](OPERATING.md)
is the short, plain-language page for day-to-day operation.

**When to use this instead of [`RUNBOOK.md`](RUNBOOK.md):** you have root (or
passwordless sudo) inside a container someone else provisioned — a shared
cluster, a JupyterHub pod, a rented GPU box sold as "a container, not a VM" —
and no way to start a Docker *daemon* in it. Nested Docker generally needs
either `--privileged` or a mounted `docker.sock`, both of which are host-level
trust that a container-scoped grant doesn't include. If that's your situation,
`docker compose up` will simply never work here, no matter what flags you try.

**The fix is not to avoid Docker's benefits — it's that you don't need
Docker to get them.** Read [`../Dockerfile`](../Dockerfile) and it is five
things: install a toolchain with `apt-get`, clone two repos, build one of
them, `pip install`, set five environment variables. All five run as plain
shell commands with no daemon involved. The one thing Docker was also doing —
`mem_limit: 10g` in [`docker-compose.yml`](../docker-compose.yml), to stop a
runaway `ninja` from OOM-killing the host — is already handled: you're inside
someone else's container, so its own cgroup limits already cap you. You are
not running unsandboxed.

Everything else in this document is identical in substance to
[`RUNBOOK.md`](RUNBOOK.md) — same two processes (vLLM + the repair loop), same
792 runs, same reasoning for running it on the H100 rather than split across
machines. Only *how you invoke commands* changes: no `docker compose exec`
prefix, because there's no container boundary between you and the tools.

**Don't have apt-get, or apt-get is also blocked?** Stop and ask whoever
manages the box what you're allowed to install — there's no clean way to
build LLVM's toolchain from nothing. If GPU passthrough is also refused, see
§6.

---

## 1. Run the setup script

```bash
cd better-compiler
./scripts/setup_native.sh
```

Optionally pass an install root as the one argument (default
`$HOME/better-compiler-runtime`) — everything the script creates lives under
there; nothing outside it is touched except system packages via `apt`.

It runs diagnostics first (root? apt-get? GPU visible?) and fails loudly
before doing anything expensive if something's missing, rather than
half-installing. Read its output.

It then, in order: installs the toolchain (same PPAs and packages as
[`Dockerfile`](../Dockerfile) steps 1 and 3), does a metadata-only clone of
`llvm-project` (step 2), builds Alive2 pinned to the same commit (step 3),
installs the Python deps (step 5), and writes an env file.

**This step is idempotent.** Re-running it skips the clone and the Alive2
build if they're already done, so if it dies partway through (a flaky
connection during the LLVM clone, say), just run it again.

It finishes by writing `$ROOT/env.sh` — every variable anything in this repo
reads, in one file — and adding a block to `~/.bashrc` that sources it. **So
every new shell already has the environment**, and no command in these docs
needs an `export` line or an `env VAR=... ` prefix. That matters because the
code (correctly) has no built-in defaults for these paths; it just does
`os.environ["LAB_LLVM_DIR"]` and fails loudly if it isn't set.

The `~/.bashrc` edit is idempotent — it greps for its own marker first, so
re-running the script never stacks up duplicate lines.

## 2. Confirm it worked

The shell you ran the script in started *before* `~/.bashrc` was updated, so
load the environment once by hand here. Every shell after this one gets it
automatically:

```bash
source ~/better-compiler-runtime/env.sh   # or your install root + /env.sh
echo $LAB_LLM_URL                          # expect http://127.0.0.1:8000/v1
python3 -m pytest tests -q
```

Same test suite as the Docker path, same expected result.

## 3. Give ccache room

```bash
ccache -M 250G
```

Same reasoning as [`RUNBOOK.md`](RUNBOOK.md) step 5 — this is the single
cheapest change to the sweep's wall time, and it's easy to forget it's a
one-time setting, not something the script above does for you.

## 4. Start the model server

Same command as [`RUNBOOK.md`](RUNBOOK.md) step 6, just without `docker compose
exec` around it — run it directly, in `tmux`, in whichever container/machine
actually has the GPU (see §6 if that isn't this one):

```bash
tmux new -s vllm
python3 -m venv ~/vllm-venv && source ~/vllm-venv/bin/activate
pip install vllm          # first run downloads ~28GB of weights

nvidia-smi --query-gpu=memory.free --format=csv    # check the real number first

export VLLM_USE_FLASHINFER_SAMPLER=0

vllm serve Qwen/Qwen2.5-Coder-14B-Instruct \
    --served-model-name qwen2.5-coder-14b \
    --quantization fp8 \
    --max-model-len 12288 \
    --gpu-memory-utilization 0.25 \
    --enforce-eager \
    --host 0.0.0.0 --port 8000 \
    --api-key local-sweep
```

A dedicated venv, not the one `setup_native.sh` made — same PEP 668 restriction
that hit the repo's own deps applies here too, and vLLM's dependency tree
(torch, transformers, ...) is large and unrelated to the repair loop's; keeping
them apart avoids one's resolver fighting the other's pins.

**14B, not the 30B `SLM_SELECTION.md` originally chose** — see Blocker 12 in
[`IMPLEMENTATION.md`](IMPLEMENTATION.md). A standing tenant on this H100 left
only ~20GB free, below the 30B model's ~31GB FP8 footprint; no
`--gpu-memory-utilization` value fixes a gap that shape. If your `nvidia-smi`
check above shows most of the card free, use the original model and command
from [`SLM_SELECTION.md`](SLM_SELECTION.md) §8 instead — this substitution is
a response to a specific memory shortage, not a standing preference for the
smaller model. Re-check free memory before every restart; what fits depends
on who else is on the card *right now*.

**`VLLM_USE_FLASHINFER_SAMPLER=0` matters on this container specifically.**
Without it, vLLM's default sampler tries to JIT-compile a CUDA kernel via
`nvcc` on first use, which fails here (`Could not find nvcc`) — this
container has CUDA runtime but not the full toolkit. The env var forces
vLLM's built-in PyTorch sampler instead; no meaningful throughput cost at
this sweep's request rate.

**`--enforce-eager` matters at this margin.** Without it, weight loading
(15.39GB, a bit over the ~14GB estimate) plus default CUDA graph capture ran
the budget negative before KV cache got any memory at all (Blocker 13:
`Available KV cache memory: -0.49 GiB`). `--enforce-eager` skips graph capture
and `torch.compile`, trading inference throughput for that memory back —
irrelevant here since inference is under 1% of this sweep's wall time.

**`--max-model-len` and `--gpu-memory-utilization` move together, not
independently, and getting this wrong fails silently, not loudly (Blocker
14).** Weights (~15.4GB) plus ~2GB fixed overhead leaves only what's left of
the utilization budget for KV cache. The danger isn't a startup error like
the ones above — it's that `repair_experiment.py`'s multi-turn loop appends
every reply to the message history, so context grows across
`--max-iterations`, and a cap that's too low doesn't reject the request
upfront: it lets several turns succeed, then fails mid-conversation and
records the run as a genuine repair failure. `4096` truncated all nine pilot
conditions; `8192` fixed eight of nine (only `raw-structured` — the
documented longest condition — still truncated); `12288` (paired with
`--gpu-memory-utilization 0.25`) was confirmed clean on all nine. These exact
numbers depend on free memory that fluctuated 19.5–23GB over the course of an
hour on this box — if a future restart's admission check fails, dial back
toward `8192`/`0.24` (confirmed reliable at the lower end of that range)
rather than re-deriving from scratch.

Detach with `Ctrl-b d`.

## 5. Point the runner at it

**Nothing to do here — `setup_native.sh` already did it.** The `LAB_LLM_*`
variables (and `VLLM_USE_FLASHINFER_SAMPLER`) are written into
`$ROOT/env.sh` alongside the `LAB_LLVM_*` paths, and the script adds a block
to `~/.bashrc` that sources that file, so every new shell has them. No
command in these docs needs an `export` line or an `env VAR=... ` prefix.

Confirm in a fresh shell:

```bash
echo $LAB_LLM_URL        # expect http://127.0.0.1:8000/v1
```

Empty output means this shell predates the setup script — `source
$ROOT/env.sh` once, or open a new shell.

Edit `$ROOT/env.sh` directly if any of it needs changing (a different port, a
GPU on another host — see §7). Re-running `setup_native.sh` regenerates the
file, so keep changes you care about somewhere else too.

## 6. Everything from here is identical to `RUNBOOK.md`

Preflight, the nine-run pilot, the real sweep, reading results — steps 7
through 10 in [`RUNBOOK.md`](RUNBOOK.md) — are exactly the same commands,
just run directly instead of through `docker compose exec better-compiler`.
For example, step 7 becomes:

```bash
python3 scripts/check_llm_endpoint.py --repeat 3
```

Follow that document from its "Step 7 — Preflight the endpoint" heading
onward; only the invocation prefix changes, not the reasoning, the flags, or
the order.

**One thing that differs from RUNBOOK.md's daily-check list:** with no
container `mem_limit`, nothing automatically protects the box from
`--build-jobs` set too high — you're relying on whatever cgroup cap your
container already has, not a limit you chose. Check it before picking a
number, don't assume:

```bash
cat /sys/fs/cgroup/memory.max 2>/dev/null || cat /sys/fs/cgroup/memory/memory.limit_in_bytes
free -g
```

Blocker 9 (see [`IMPLEMENTATION.md`](IMPLEMENTATION.md)) measured roughly
0.9 GB of peak memory per parallel compile job on LLVM's heaviest translation
units. Divide whatever cap you find by that, leave headroom, and pass the
result as `--build-jobs`. Don't reuse `--build-jobs 64` from `RUNBOOK.md`
without checking — that number assumed the full 256 GB the Docker path grants
itself, which you may not actually have here.

---

## 7. If the GPU isn't visible in this container

Some cluster setups split "build container" from "GPU container" — you might
have 96 CPUs here and the H100 sitting in a *different* container or node.
If `nvidia-smi` failed in the setup script's diagnostics, that's what's
happening.

The fix is unchanged from the core insight in [`RUNBOOK.md`](RUNBOOK.md): the
LLM call is just HTTP, so it doesn't matter which container serves it. Run
vLLM (step 4 above) wherever `nvidia-smi` *does* work, note that container's
address, and point `LAB_LLM_URL` at it instead of `127.0.0.1`. Everything else
in this document — the build, the sweep, the results — stays where you're
reading this from.

---

## 8. What NOT to do

**Don't try to install Docker-in-Docker or request the daemon socket** as a
workaround — if the platform scoped you to container permissions on purpose,
that request will likely just be declined, and it buys nothing this script
doesn't already give you.

**Don't try to prebuild the 24 bugs' `opt` binaries elsewhere and ship them
here via GitHub.** It looks appealing — "skip the 20 hours of builds" — but
doesn't hold up: each is a multi-gigabyte artifact tied to a specific commit
and toolchain, there are 24 of them, GitHub isn't built to move that, and
[Blocker 9](IMPLEMENTATION.md#L795) already found this box builds them faster
than the 4-core machine that produced the original 20-hour figure. The one
artifact in this pipeline that's genuinely reusable across machines is
Alive2's binary — and it builds in a couple of minutes, so there's nothing to
save by prebuilding it either.
