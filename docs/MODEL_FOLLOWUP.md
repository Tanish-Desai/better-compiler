# Optional second-model evaluation

Reviewed on 2026-10-22. This is a proposed follow-up, not a claim that a
different model repairs LLVM more effectively. Validate the v3 harness first;
do not replace unfinished v2 records or mix configurations in one directory.

## Recommendation

Use **`mistralai/Devstral-Small-2-24B-Instruct-2512`** for the first secondary
pilot if there is time after the v3 validation. It provides an independent
vendor and a dense architecture, so agreement would be more informative about
model dependence than another Qwen variant. Its official checkpoint is already
FP8, has 24B parameters, uses Apache 2.0, and documents vLLM serving with
`mistral_common >= 1.8.6`. These facts make it an operationally reasonable
candidate; they do not establish its quality for our four-turn repair task.
[Official model card](https://huggingface.co/mistralai/Devstral-Small-2-24B-Instruct-2512).

The checkpoint revision resolved from the Hugging Face API was
`55c5b41e98c2dbd21b0c8afffc540dcfc9eb5128`. Its
[pinned weight index](https://huggingface.co/mistralai/Devstral-Small-2-24B-Instruct-2512/blob/55c5b41e98c2dbd21b0c8afffc540dcfc9eb5128/model.safetensors.index.json)
reports **25,792,912,480 bytes (24.02 GiB)** of tensor storage. This is disk
tensor storage, not a measured peak GPU-memory requirement.

At `--gpu-memory-utilization 0.55`, an 80 GB-class H100 gives this process
roughly 44 GB of the device. Actual usable memory must be read from the
startup log. Weight scales, non-FP8 modules, activations, KV cache and runtime
buffers also consume memory. The estimate leaves substantially more headroom
than an 80B FP8 checkpoint; startup and a real generation must still pass.
Do not allocate this budget simultaneously to two model servers.

## Other candidates considered

| Model | Verified properties | Decision for this deadline |
|---|---|---|
| [Qwen3.6-35B-A3B-FP8](https://huggingface.co/Qwen/Qwen3.6-35B-A3B-FP8) | Official FP8 release, Apache 2.0. Its [base model card](https://huggingface.co/Qwen/Qwen3.6-35B-A3B) describes a 35B language model with 3B activated parameters, a vision encoder, hybrid attention and optional thinking. | Reserve candidate. Roughly 35 GB for one-byte language-model weights alone, before unquantized parts, caches and buffers; a fit in the shared allocation is unverified. Thinking settings also need explicit recording. |
| [Qwen3-Coder-Next-FP8](https://huggingface.co/Qwen/Qwen3-Coder-Next-FP8) | Apache 2.0; [80B total, 3B activated](https://huggingface.co/Qwen/Qwen3-Coder-Next). Its [pinned index](https://huggingface.co/Qwen/Qwen3-Coder-Next-FP8/blob/da6e2ed27304dd39abadd9c82ef50e8de67bdd4c/model.safetensors.index.json) reports 80,362,904,064 bytes, or 74.84 GiB, of weights. | Does not fit the roughly 44 GB allocation even before KV cache. Active parameter count does not determine weight residency. Lower-bit quantization or CPU offload would be a different, untested serving setup. |

Qwen3.6's FP8 revision resolved to
`95a723d08a9490559dae23d0cff1d9466213d989`; its index did not expose a
`metadata.total_size`, so no measured checkpoint size is claimed here.
The vLLM **v0.28.0** [model registry](https://github.com/vllm-project/vllm/blob/v0.28.0/vllm/model_executor/models/registry.py)
contains the relevant `Mistral3ForConditionalGeneration`,
`Qwen3_5MoeForConditionalGeneration` and `Qwen3NextForCausalLM` entries.
Architecture support is not a guarantee that every kernel/dependency on this
particular H100 is compatible. No model ranking is inferred from vendor
SWE-bench scores with different agents or budgets.

## Devstral serving candidate

Run this only after the existing repair process and its model server have
stopped. Keep the LLVM checkout exclusive to one repair/validation process.
Use the vLLM environment, verify its actual versions, and retain the resulting
server log alongside the experiment provenance. Do not upgrade the running
Qwen environment to try this model.

```bash
source ~/vllm-venv/bin/activate
python - <<'PY'
from importlib.metadata import version
for name in ("vllm", "torch", "transformers", "mistral-common"):
    print(name, version(name))
PY
```

If `mistral-common` is absent or below the documented minimum, prepare a
separate compatible serving environment before continuing. Preserve its full
package versions. The command below combines the official model's server
invocation with the local experiment's serial, text-only memory limits. It
has **not** been executed on the university H100.

```bash
export VLLM_USE_FLASHINFER_SAMPLER=0
vllm serve mistralai/Devstral-Small-2-24B-Instruct-2512 \
    --revision 55c5b41e98c2dbd21b0c8afffc540dcfc9eb5128 \
    --tokenizer-revision 55c5b41e98c2dbd21b0c8afffc540dcfc9eb5128 \
    --served-model-name devstral-small2-24b \
    --max-model-len 32768 --max-num-seqs 1 \
    --gpu-memory-utilization 0.55 --enforce-eager \
    --language-model-only --generation-config vllm \
    --host 127.0.0.1 --port 8000 --api-key local-sweep
```

The checkpoint declares FP8 itself; this command does not request another
online conversion. Tool calling is unnecessary because the v3 runner requests
targeted search/replace edits within its source window, applies those edits
itself, and returns validation feedback.
The [vLLM serving reference](https://docs.vllm.ai/en/latest/cli/serve/)
documents revision selection, context limits, generation defaults and
text-only serving. Confirm these flags with the installed v0.28.0 help if a
local installation differs from the published version.

In a separate shell with the repair-loop venv and native LLVM environment:

```bash
source ~/better-compiler-runtime/env.sh
export LAB_LLM_URL=http://127.0.0.1:8000/v1
export LAB_LLM_TOKEN=local-sweep
export LAB_LLM_MODEL=devstral-small2-24b
export LAB_LLM_TEMP=0.8
export LAB_LLM_MAX_TOKENS=8192
python3 scripts/check_llm_endpoint.py
```

Do not infer or invent a training-data cutoff from the release date. The
harness's cutoff setting is self-declared and does not certify absence of
benchmark contamination. Preserve it with the rest of the run configuration
and state this limitation in the paper.

## One excluded-bug pilot before any expansion

Bug **115575** was excluded from the sample before the sweep. Use it for
operational screening, not a newly selected main-sample bug on which one
condition happened to look promising. First require the known human fix check
to pass under the v3 harness, with no other LLVM process running:

```bash
python3 scripts/check_validation.py --bug-id 115575 --build-jobs 64
```

Use 64 jobs only if the container's memory allocation supports it. Once that
check passes, the six conditions below cover all four original primary
comparisons. One trial gives six runs and at most 24 model replies:

```bash
pilot_conditions=(baseline raw-plain generic-plain llvmreduce-plain
                  iraware-plain iraware-structured)
python3 examples/repair_experiment.py 115575 \
    --repeat 1 --max-iterations 4 --build-jobs 64 \
    --out results-devstral-small2-v3-pilot \
    --condition "${pilot_conditions[@]}" --plan
python3 examples/repair_experiment.py 115575 \
    --repeat 1 --max-iterations 4 --build-jobs 64 \
    --out results-devstral-small2-v3-pilot \
    --condition "${pilot_conditions[@]}"
python3 scripts/triage_sweep.py results-devstral-small2-v3-pilot
python3 scripts/inspect_patches.py results-devstral-small2-v3-pilot
```

Inspect completion truncation, applicable edits, compilation diagnostics,
displayed reductions, full validation and runtime. One bug cannot establish a
feedback effect or identify a superior model. Do not select a model because
its intervention-minus-baseline difference on this pilot is favorable. If an
additional evaluation fits the remaining time, fix its bug set, conditions,
repeat count and time budget before generating those outcomes; label the
follow-up exploratory and keep it separate from Qwen v2/v3 records.

The existing Cursor credit balance and Claude Code cloud-session credits are
not a callable model API for this command. No credit bridge is configured or
assumed. A supported agent adapter would introduce another experimental
setup and needs its own validation and provenance. Completing a reliable
Qwen v3 evaluation takes priority over adding such an integration this week.
