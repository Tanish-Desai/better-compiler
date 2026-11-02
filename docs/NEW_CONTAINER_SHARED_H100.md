# Fresh shared-H100 container: commands and evaluation gates

**2026-11-02 alternative:** If fewer than 10 GiB of VRAM remain, use
[HOSTED_GEMINI_WEEK.md](HOSTED_GEMINI_WEEK.md) for hosted inference and local
CPU validation. The local offload profile below will not fit that allocation.
The API study has its own protocol and output directory; do not pool them.

Written 2026-11-01 for the operator's Ubuntu 24.04.1 container, H100 PCIe,
driver 580.178.04, 81,559 MiB total / 21,796 MiB free VRAM, 928 GiB host RAM
available, and 1.2 TB disk free. Host RAM is not necessarily the container's
limit. These commands have not been executed on that container here. Startup and validation are gates, not assumed successes.

Use Bash as root inside the container. Only one repair/known-patch validation
process may own the dedicated LLVM checkout. This runbook supersedes the
October six-arm commands for new data; keep all older results unchanged.

## What will be measured

The operator confirmed no v3 runs have started. The new frozen manifest is
`data/repair_v3_shared_h100_protocol.json`: 16 original remaining bugs, four
conditions, three trials, four turns, **192 cells** / at most 768 replies.

| Condition | Why retain it |
| --- | --- |
| raw-plain | Original unreduced-feedback anchor |
| raw-structured | Strongest descriptive v2 control; compare at fixed renderer |
| llvmreduce-structured | Existing IR reducer with the same structured renderer |
| iraware-structured | Our paired, witness-aware reduction pipeline |

Three primary contrasts are frozen in the manifest: ours vs raw structured,
ours vs LLVM-reduce structured, and raw structured vs raw plain. The analysis
uses bug-level pass@3, exact paired tests and BH across those three contrasts.
Condition order is counterbalanced independently of outcomes. Do not add
trials, drop difficult bugs or change models based on interim p-values.

This is a small amended follow-up, not a guaranteed significant result or Q1
evaluation. Bug 115149 had prior diagnostic exposure; the 16 bugs are not a
pristine unseen set. A paper must also report plausible-patch review,
optimization suppression, actual witness reduction/fallback, costs and
limitations. Compare all arms within this one configuration, not against v2
rates. The detailed dated amendment is in `docs/ANALYSIS_PLAN.md`.

## 1. Clone and inspect, before the long installations

```bash
apt-get update
apt-get install -y git tmux ca-certificates curl python3 python3-venv util-linux

git clone --branch feat/h100-sweep --single-branch \
    https://github.com/Tanish-Desai/better-compiler.git "$HOME/better-compiler"
cd "$HOME/better-compiler"
mkdir -p run-provenance

bash scripts/setup_native.sh --check
python3 scripts/resource_plan.py --gpu-index 0 --out run-provenance/initial-resources.json
```

Index0 is the physical H100 shown by `nvidia-smi -L`; select the appropriate
index if there is more than one device. The planner records the UUID rather
than assuming CUDA numeric indices always match nvidia-smi. It reads cgroup
CPU/RAM limits and keeps 4 GiB of VRAM beyond this experiment's allocation.
It does not reserve resources against other tenants.

For the reported snapshot it computes GPU utilization **0.213**, about
16.96 GiB, rather than the old 0.55 allocation. The configuration helper below
requires at least 16 GiB of GPU budget and known container RAM availability
of 64 GiB. If a gate fails, preserve its output and resolve the limit; do not
increase allocation to take memory from other jobs.

## 2. Native setup and known-patch validation in tmux

```bash
tmux new -s bc-setup
```

Inside that session:

```bash
cd "$HOME/better-compiler"
set -o pipefail
LAB_SETUP_JOBS=4 bash scripts/setup_native.sh "$HOME/better-compiler-runtime" \
    2>&1 | tee run-provenance/setup.log
```

Require setup to finish successfully before continuing. It installs GCC13,
LLVM21/llvm-reduce, builds pinned Alive2, creates a dedicated repair environment
and LLVM checkout, and preserves an existing environment on reruns. On Noble
it uses Noble package sources, not the old Jammy PPA mixture.

```bash
source "$HOME/better-compiler-runtime/env.sh"
ccache -M 100G
python3 -m pytest -q
python3 scripts/check_ir_roundtrip.py

python3 scripts/resource_plan.py --out run-provenance/build-resources.json
export BUILD_JOBS="$(python3 -c 'import json; print(json.load(open("run-provenance/build-resources.json"))["recommended_build_jobs"])')"
export LAB_LLVM_PARALLEL_LINK_JOBS=1

# Populate the fresh metadata-only clone at the excluded diagnostic bug,
# rather than fetching unrelated latest-HEAD files first.
FIRST_BASE="$(python3 -c 'import json,os; print(json.load(open(os.path.join(os.environ["LAB_DATASET_DIR"],"115575.json")))["base_commit"])')"
git -C "$LAB_LLVM_DIR" checkout --detach "$FIRST_BASE"

flock -n "$HOME/better-compiler-runtime/repair.lock" \
    python3 scripts/check_validation.py --bug-id 115575 --build-jobs "$BUILD_JOBS" \
    2>&1 | tee run-provenance/known-patch.log
```

Require `PASS`. A failing unpatched build, missing verifier/plugin, timeout,
or reproducer that already passes must be diagnosed before model evaluation.
This can require a long first LLVM build; it needs no GPU. Keep assertions,
backends, reproducers and listed regression checks intact. The resource
planner caps compile jobs at 8 initially; do not copy the old 64-job command.
The 100 GB ccache setting is appropriate only while the reported disk room
remains available. Inspect `df -h` and `ccache -s` over time.

Tmux: **Ctrl-b, then d** detaches without stopping the command. Reconnect with
`tmux attach -t bc-setup`; list sessions with `tmux ls`. Tmux survives SSH
disconnects, not destruction/replacement of the container.

## 3. CPU-only reducer evidence, with no model bill

After the known-patch check, run in the existing setup session or a new one:

```bash
tmux new -s bc-witnesses
```

Inside:

```bash
cd "$HOME/better-compiler"
source "$HOME/better-compiler-runtime/env.sh"
python3 scripts/compare_saved_witnesses.py --plan
set -o pipefail
python3 scripts/compare_saved_witnesses.py --budget 400 \
    --out witness-comparison-v2-cpu 2>&1 | tee run-provenance/witnesses.log
```

This compares raw, generic, LLVM-reduce, IR-aware and IR-aware without operand
promotion on **eight saved real initial witnesses**. Every method uses the
same candidate-call cap, followed by a fresh same-function/error-class check.
The report retains displayed pairs, sizes, fallbacks, failures and separate
candidate/refresh/postcheck costs. It does not invoke a model, run opt or
rebuild LLVM. Run separately from the sweep when interpreting timings.

These are development witnesses from v2, not a large independent benchmark.
The ablation disables operand promotion only. Same error class does not prove
the same semantic root cause. Do not call this diagnostic a demonstrated
repair improvement. Its report is written after each completed case; if
interrupted preserve that partial report and use a separately named attempt
rather than overwriting it.

## 4. Install the pinned server environment

Use another tmux session for the large package download:

```bash
tmux new -s bc-model-install
```

Inside:

```bash
cd "$HOME/better-compiler"
python3 -m venv "$HOME/vllm-venv"
"$HOME/vllm-venv/bin/python" -m pip install --upgrade pip uv
"$HOME/vllm-venv/bin/uv" pip install --python "$HOME/vllm-venv/bin/python" \
    'vllm==0.28.0' --torch-backend=cu130

source "$HOME/better-compiler-runtime/env.sh"
python3 scripts/configure_shared_h100.py --gpu-index 0 \
    --vllm-venv "$HOME/vllm-venv" \
    --runtime-root "$HOME/better-compiler-runtime" \
    --out run-provenance/shared-h100

source run-provenance/shared-h100/env.sh
source "$HOME/vllm-venv/bin/activate"
python3 - <<'PY'
import torch, vllm
print('vLLM', vllm.__version__, 'torch', torch.__version__, 'CUDA', torch.version.cuda)
assert torch.cuda.is_available()
print(torch.cuda.get_device_name(0))
print(torch.ones(1, device='cuda').sum().item())
PY
```

This CUDA13 route is for the reported driver 580.178.04. A different driver
needs the compatibility checks in [SHARED_H100_MODELS.md](SHARED_H100_MODELS.md).
The helper writes a quoted runner environment, exact server launcher and
immutable receipt containing package hashes, model revision, GPU UUID and
launch arguments. It refuses mismatches; don't edit the receipt to bypass
the guard. It starts no server and spends no API credits.

The primary model is pinned official
`Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8`, revision
`dcaee4d4dfc5ee71ad501f01f530e5652438fde0`, with **20 GiB CPU weight offload**.
Estimated GPU weight residency is about 9 GiB, plus about 3 GiB of 32K BF16 KV
and runtime buffers. **Fit and latency are not yet measured.** The 20 GiB offload amount is
not the model server's total host RAM use. PCIe transfers may be slow.

## 5. Start the model, then probe it before any repair pilot

```bash
tmux new -s bc-vllm
```

Inside:

```bash
cd "$HOME/better-compiler"
set -o pipefail
bash run-provenance/shared-h100/serve.sh \
    2>&1 | tee run-provenance/shared-h100/server.log
```

Wait for successful server startup, then detach. If it runs out of memory or
fails a kernel import, keep the log. Do not raise the cap or kill other users'
GPU processes. The documented smaller-model fallback would use a different
prospective protocol and results directory; don't relabel it as this model.

In another shell, check short output format and long-input operation:

```bash
cd "$HOME/better-compiler"
source run-provenance/shared-h100/env.sh
set -o pipefail
python3 scripts/check_llm_endpoint.py --repair-format --repeat 3 \
    2>&1 | tee run-provenance/shared-h100/endpoint.log
python3 scripts/check_llm_endpoint.py --context-probe \
    2>&1 | tee run-provenance/shared-h100/context-probe.log
```

Both must exit successfully. The context probe uses about 51K characters of
input and reports actual token usage and request-wall throughput. It is not
a proof of full 32K/multiturn capacity or good repairs; the pilot checks the
real loop. Judge the actual latencies before committing the main run budget.

The pilot and main protocol also require a clean semantic failure from the
unpatched compiler on every bug. A timeout, unsupported IR, verifier error
or unproved function stops the sweep before querying the model; it must not
be counted as an unsuccessful feedback treatment.

## 6. Four-cell excluded pilot

```bash
tmux new -s bc-pilot
```

Inside:

```bash
cd "$HOME/better-compiler"
source run-provenance/shared-h100/env.sh
python3 scripts/resource_plan.py --out run-provenance/pilot-build-resources.json
export BUILD_JOBS="$(python3 -c 'import json; print(json.load(open("run-provenance/pilot-build-resources.json"))["recommended_build_jobs"])')"
export LAB_LLVM_PARALLEL_LINK_JOBS=1
set -o pipefail
flock -n "$HOME/better-compiler-runtime/repair.lock" \
    python3 examples/repair_experiment.py 115575 \
    --repeat 1 --max-iterations 4 --oracle-budget 400 --require-semantic-baseline \
    --build-jobs "$BUILD_JOBS" --out results-qwen3-v3-shared-pilot \
    --condition raw-plain raw-structured llvmreduce-structured iraware-structured \
    2>&1 | tee run-provenance/pilot.log

python3 scripts/triage_sweep.py results-qwen3-v3-shared-pilot
python3 scripts/inspect_patches.py results-qwen3-v3-shared-pilot
python3 scripts/audit_sweep.py results-qwen3-v3-shared-pilot --repeat 1 \
    --condition raw-plain raw-structured llvmreduce-structured iraware-structured
```

Require four completed cells, usable edits, a clean known-patch check and at
least one full harness pass on this previously repairable development bug.
Inspect every malformed/truncated reply, unknown-linker error and verifier
fallback. If the pilot produces no full passes, diagnose before evaluation;
don't spend 192 cells on a broken endpoint, invalid patches or an unvalidated
model floor. Pilot success is a feasibility gate, not a selection based on
main-sample repair rates. Retain pilot evidence, including failures.

## 7. Freeze and run 192 evaluation cells

After the preceding gates pass:

```bash
tmux new -s bc-sweep
```

Inside:

```bash
cd "$HOME/better-compiler"
source run-provenance/shared-h100/env.sh
python3 scripts/resource_plan.py --out run-provenance/sweep-build-resources.json
export BUILD_JOBS="$(python3 -c 'import json; print(json.load(open("run-provenance/sweep-build-resources.json"))["recommended_build_jobs"])')"
export LAB_LLVM_PARALLEL_LINK_JOBS=1
python3 -m pip freeze > run-provenance/repair-packages.txt
sha256sum "$LAB_LLVM_ALIVE_TV" "$LAB_LLVM_LLVM_REDUCE" > run-provenance/verifier-binaries.sha256
git rev-parse HEAD > run-provenance/source-commit.txt

python3 examples/repair_experiment.py \
    --protocol data/repair_v3_shared_h100_protocol.json \
    --build-jobs "$BUILD_JOBS" --out results-qwen3-v3-shared --plan

set -o pipefail
flock -n "$HOME/better-compiler-runtime/repair.lock" \
    python3 examples/repair_experiment.py \
    --protocol data/repair_v3_shared_h100_protocol.json \
    --build-jobs "$BUILD_JOBS" --out results-qwen3-v3-shared \
    2>&1 | tee -a run-provenance/sweep.log
```

The plan must show 192 cells. Completed compatible records are skipped on
resume; endpoint-error records are retried. After a disconnect, attach to
`bc-sweep` first. If the process has ended, source the same generated env and
rerun the identical sweep command. Do not run two repair processes, use
`--overwrite`, delete `.experiment.json`, pull code into a running sweep, or
change offload/weights/precision when more GPU memory happens to become free.
The server continues to hold its allocation during CPU validation; agree a
stable share with the university if other jobs need that memory.

## 8. Monitor, require complete coverage, archive off-container

From another shell:

```bash
cd "$HOME/better-compiler"
source run-provenance/shared-h100/env.sh
python3 scripts/triage_sweep.py results-qwen3-v3-shared
python3 examples/summarize_results.py results-qwen3-v3-shared \
    --protocol data/repair_v3_shared_h100_protocol.json

# Only after all planned cells complete; incomplete coverage is rejected.
python3 examples/analyze_significance.py results-qwen3-v3-shared \
    --protocol data/repair_v3_shared_h100_protocol.json --final
```

Archive completed files regularly. The following assumes the pilot and
validation have run; the main sweep and CPU diagnostic are included if present:

```bash
mkdir -p archives
paths=(results-qwen3-v3-shared-pilot validation-checks run-provenance)
for p in results-qwen3-v3-shared witness-comparison-v2-cpu; do
    if [ -d "$p" ]; then paths+=("$p"); fi
done
tar --exclude='*.tmp' -czf "archives/shared-h100-$(date -u +%Y%m%dT%H%M%SZ).tar.gz" "${paths[@]}"
ls -lh archives
```

Download the archive to your own machine using the university's file-transfer
method. A backup on the same ephemeral container does not protect against its
replacement. Archive records and nonsecret provenance, not API keys or full
private environment files. Read plausible passing patches against the reference
fix and additional semantic corner cases; selected test passes alone do not
certify correctness or preserved optimization quality.

## Budget and what a paper can conclude

The existing university allocation plus local inference adds no API bill.
The expensive part remains CPU builds/checks and collecting valid matched
data. Do not rent a GPU expecting â‚¹200â€“300 to cover an entire compiler-repair
study. No GPU training or fine-tuning is part of this plan.

On 2026-11-01 OpenRouter lists Qwen3-Coder 30B at $0.07/M input and $0.28/M
output; its endpoint API identifies the cheap route as SiliconFlow FP8. Scaling
the old 205-run snapshot's measured token density to 192 runs gives an
illustrative **$0.85** usage estimate. At 768 requests each using 24,576 input
and 8,192 output tokens, the same rates give **$3.08**, before pilot, retries,
fees and tax. Neither estimate is a guaranteed bill or GPU/build cost.
[Model prices](https://openrouter.ai/qwen/qwen3-coder-30b-a3b-instruct),
[current endpoints](https://openrouter.ai/api/v1/models/qwen/qwen3-coder-30b-a3b-instruct/endpoints).

OpenRouter's terms state a **$5 minimum credit purchase**, and purchase fees
apply. That upfront amount already exceedsâ‚¹300 at ordinary USD/INR rates.
Use an already-funded university API account if available rather than buying
unneeded credits. An API experiment would pin a provider, disable fallback,
record its actual model/precision and use a separate protocol/directory; no
API routing adapter is configured by this local-serving guide.
[Terms](https://openrouter.ai/terms/), [pricing fees](https://openrouter.ai/pricing),
[provider controls](https://openrouter.ai/docs/guides/routing/provider-selection).

Free Groq or Gemini access is quota/model dependent, and Colab does not
guarantee hardware or runtime lifetime. They can support a supplementary
pilot, not a promised complete equivalent experiment. Keep the university's
CPU environment for LLVM validation even if inference is moved elsewhere.
[Groq limits](https://console.groq.com/docs/rate-limits),
[Gemini prices](https://ai.google.dev/gemini-api/docs/pricing),
[Colab limits](https://research.google.com/colaboratory/faq.html).

For a strong submission, the useful outcomes are credible paired repair
comparisons, measured witness preservation/reduction and costs, and reviewed
patches. Novelty rests on the paired witness-aware intervention and evidence
for its contribution, not on a significance target or model substitution.
More independent bugs and an independent model would strengthen external
validity if resources later permit; three trials do not turn 16 bugs into 48.
If the proposed reducer does not improve repair, report that result and its
mechanism evidence honestly. No journal quartile can be guaranteed in advance.
