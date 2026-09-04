# Runbook: running the sweep without Docker

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

When it finishes, it prints:

```
Environment written to $ROOT/env.sh -- source it before running anything:
    source $ROOT/env.sh
```

**Do that in every new shell** — the vLLM shell, the pilot shell, the sweep
shell, all of them. Nothing below works without it, since the code (correctly)
has no built-in defaults for these paths and just does `os.environ["LAB_LLVM_DIR"]`.

## 2. Confirm it worked

```bash
source ~/better-compiler-runtime/env.sh   # or your install root + /env.sh
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
pip install vllm          # first run downloads ~60GB of weights
vllm serve Qwen/Qwen3-Coder-30B-A3B-Instruct \
    --served-model-name qwen3-coder-30b \
    --quantization fp8 \
    --max-model-len 32768 \
    --gpu-memory-utilization 0.9 \
    --host 0.0.0.0 --port 8000 \
    --api-key local-sweep
```

A dedicated venv, not the one `setup_native.sh` made — same PEP 668 restriction
that hit the repo's own deps applies here too, and vLLM's dependency tree
(torch, transformers, ...) is large and unrelated to the repair loop's; keeping
them apart avoids one's resolver fighting the other's pins.

Detach with `Ctrl-b d`.

## 5. Point the runner at it

The Docker path baked `LAB_LLM_*` into `docker-compose.h100.yml` via `.env`.
There's no compose layer here, so export them directly — once per shell, or
append to `~/.bashrc`:

```bash
export LAB_LLM_URL=http://127.0.0.1:8000/v1     # localhost if vLLM is right here;
                                                  # otherwise that container's
                                                  # reachable address — see §6
export LAB_LLM_TOKEN=local-sweep                 # must match --api-key above
export LAB_LLM_MODEL=qwen3-coder-30b             # must match --served-model-name
export LAB_LLM_TEMP=0.8                          # MUST be > 0 -- see RUNBOOK.md
```

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
