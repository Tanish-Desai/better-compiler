# Models for a shared H100

Checked against official model repositories and vLLM 0.28.0 documentation on
2026-11-01. These commands have **not been tested on the new H100 container**.
Run one server at a time. A model that starts still needs the endpoint probe,
known-patch validation and the held-out repair pilot before the main experiment.

The first choice retains Qwen3-Coder's 30B total parameters and FP8 weights by
offloading some weights to host RAM. The fallback is Red Hat AI's FP8 quantization
of the smaller dense Qwen3.5-9B model. Neither choice has demonstrated an
improvement on this repair benchmark.

The supplied hardware snapshot is an H100 PCIe with 81,559 MiB total GPU memory
and **21,796 MiB free (21.29 GiB)**, driver 580.178.04, Ubuntu 24.04, and about
928 GiB host RAM available. The launch below targets approximately **17 GiB GPU
memory**, leaving at least 4 GiB outside this process at that snapshot. Recheck
with the hardware doctor before launch: shared GPU use and effective container
RAM limits can change. The earlier 30 GB-free assumption does not apply here.

## Check the host driver before installing the server

First record the actual host driver; a fresh container does not replace it:

```bash
nvidia-smi --query-gpu=name,driver_version,memory.total,memory.free --format=csv
uname -m
```

The vLLM **0.28.0 release artifacts** identify PyPI's default wheel as CUDA 13.0.
Its versioned installation page still describes a CUDA 12.9 default, so use the
release artifacts and the installed runtime as the evidence. Merely adding a
PyTorch index or `--torch-backend=auto` does not establish that the selected vLLM
wheel was compiled for the same CUDA version.

For a conservative fresh Linux installation, use these gates:

| Actual host driver | Server installation route |
| --- | --- |
| At least 580.65.06 | The pinned default `uv pip install 'vllm==0.28.0' --torch-backend=cu130` route can be tried in a fresh server environment. |
| At least 575.51.03, below the CUDA 13 gate | Use the explicit official CUDA 12.9 release wheel below. |
| Older or unknown | First identify a driver-compatible, supported server environment or an administrator-provided compatible container. Do not blindly install the default wheel or change the university's host driver. |

The CUDA 12.9 gate uses its packaged Linux driver version, not the lower
theoretical CUDA 12.x minor-compatibility minimum. NVIDIA documents limitations
for PTX/JIT applications on older drivers. A pre-existing administrator-supported
compatibility setup may work, but this guide has not verified one on the new
container. A CUDA runtime import alone is not proof that all serving kernels work.

For **Linux x86_64** and the CUDA 12.9 gate, the official release lists this
specific wheel:

```bash
uv pip install \
  'https://github.com/vllm-project/vllm/releases/download/v0.28.0/vllm-0.28.0+cu129-cp38-abi3-manylinux_2_28_x86_64.whl' \
  --torch-backend=cu129 \
  --extra-index-url https://download.pytorch.org/whl/cu129

python3 - <<'PY'
import torch, vllm
print('vLLM:', vllm.__version__, 'PyTorch:', torch.__version__, 'CUDA runtime:', torch.version.cuda)
assert torch.cuda.is_available(), 'CUDA is unavailable in this server environment'
print(torch.cuda.get_device_name(0))
print(torch.ones(1, device='cuda').sum().item())
PY
```

Published wheel SHA256:
`8ec943b66a0c6b4351d0778e99d7bacfca5788dd8eedd49425092bacb61c4397`.
The release also lists an aarch64 wheel; do not install the x86_64 URL on that
architecture. Dependency installation, model startup and the endpoint probes
still need to pass. Save the resulting `pip freeze` and CUDA/driver information
with the experiment rather than upgrading an environment during a sweep.

The supplied **580.178.04** driver passes the default CUDA 13.0 gate; a CUDA 12.9
wheel is not required for that snapshot. This checks the driver version, not a
completed vLLM installation or successful model startup.

## Primary: Qwen3-Coder-30B FP8 with CPU weight offload

Use an installed vLLM 0.28.0 environment. The following profile is for a physical
H100 **80 GB** with the supplied approximately 21.29 GiB free and sufficient
effective container RAM for the offloaded weights, serving overhead and LLVM
builds:

```bash
export VLLM_USE_FLASHINFER_SAMPLER=0

vllm serve Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8 \
  --revision dcaee4d4dfc5ee71ad501f01f530e5652438fde0 \
  --tokenizer-revision dcaee4d4dfc5ee71ad501f01f530e5652438fde0 \
  --served-model-name qwen3-coder-30b-fp8-offload \
  --dtype bfloat16 \
  --cpu-offload-gb 20 \
  --gpu-memory-utilization 0.213 \
  --max-model-len 32768 \
  --max-num-seqs 1 \
  --max-num-batched-tokens 2048 \
  --enable-chunked-prefill \
  --enforce-eager \
  --generation-config vllm \
  --host 127.0.0.1 --port 8000 --api-key local-sweep
```

`--gpu-memory-utilization` is a fraction of the GPU's **total** memory, not its
currently free memory: 0.213 targets about 16.96 GiB on the supplied 81,559 MiB
device. The hardware doctor's rounded 17 GiB cap leaves external headroom.
Use the hardware doctor's calculated cap if the physical GPU or available
allocation differs. This allocation is not a reservation against another
user's future allocations. Do not raise it just because more memory is briefly
free.

The pinned checkpoint's safetensors files contain 31,175,618,584 bytes, about
29.03 GiB. With approximately 20 GiB of weights on the CPU, roughly 9 GiB of
weights should remain on the GPU. One 32K BF16 KV cache is approximately 3 GiB,
calculated from 48 layers, four KV heads and head dimension 128. These are
capacity estimates, **not measured peak memory**; allocator, kernel workspace,
profiling and other runtime allocations also need space. Twenty GiB is an
offload amount, not a bound on the server's total host-memory use.

vLLM documents CPU offload as requiring a fast CPUâ€“GPU interconnect. Its tagged
Qwen3 MoE implementation uses the shared layer offloader, and the loader restores
offloaded placement after quantization processing. This supports trying the
profile; it does not establish its speed or stability on this container. Compare
observed request latency with the time available before committing to a sweep.
This is an H100 **PCIe**: offloaded weight accesses cross the CPUâ€“GPU link and
may substantially reduce generation speed. Abundant host RAM does not remove
that bandwidth limit. A passed long-input toy probe and the held-out pilot are
required; fitting the estimate is not proof that a 17 GiB launch succeeds.

In the runner shell, after sourcing the native runtime environment:

```bash
export LAB_LLM_URL=http://127.0.0.1:8000/v1
export LAB_LLM_TOKEN=local-sweep
export LAB_LLM_MODEL=qwen3-coder-30b-fp8-offload
export LAB_LLM_MODEL_REPOSITORY=Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8
export LAB_LLM_MODEL_REVISION=dcaee4d4dfc5ee71ad501f01f530e5652438fde0
export LAB_LLM_TEMP=0.8
export LAB_LLM_MAX_TOKENS=8192

python3 scripts/check_llm_endpoint.py --repair-format --repeat 3
python3 scripts/check_llm_endpoint.py --context-probe
```

The context probe prepends about 50K characters of deterministic numbered C++
comments to the toy source and requires the same applicable SEARCH/REPLACE edit.
It reports the server's actual prompt/completion token counts, request wall
latency and completion tokens per wall second. The rate includes prefill,
queueing and network time; it is not isolated decode speed. Missing server token
usage is reported as unavailable. A pass establishes this bounded long-input
toy request, **not full 32K capacity, a long output, multi-turn capacity or LLVM
repair quality**. There is no arbitrary throughput pass/fail threshold.

This official prequantized checkpoint differs from the earlier BF16 checkpoint
with online `--quantization fp8`. Keep its model repository, revision, complete
launch command, package versions and serving identity in provenance. Use the new
matching protocol and a fresh output directory; do not append to the old sweep.
If a stable 40â€“50 GB allocation becomes available, this FP8 checkpoint can also
be piloted without CPU offload under a separately recorded serving profile.
The previous `--cpu-offload-gb 12 --gpu-memory-utilization 0.30` profile is only an
optional profile when **at least 30 GB GPU memory is stably free**. It cannot fit
the supplied 21,796 MiB-free snapshot. Do not mix serving profiles within one
results directory or change them while a sweep is running.

## Fallback: Red Hat AI Qwen3.5-9B FP8, text only, non-thinking

If the offload profile cannot run reliably or has impractical latency, stop that
server before trying this one:

```bash
export VLLM_USE_FLASHINFER_SAMPLER=0

vllm serve RedHatAI/Qwen3.5-9B-FP8-dynamic \
  --revision 790f0576d2d77dd5322aa0603a470bd9e3a3d1f6 \
  --tokenizer-revision 790f0576d2d77dd5322aa0603a470bd9e3a3d1f6 \
  --served-model-name qwen35-9b-fp8-redhat \
  --dtype bfloat16 \
  --language-model-only \
  --default-chat-template-kwargs '{"enable_thinking":false}' \
  --gpu-memory-utilization 0.213 \
  --max-model-len 32768 \
  --max-num-seqs 1 \
  --max-num-batched-tokens 2048 \
  --enable-chunked-prefill \
  --enforce-eager \
  --generation-config vllm \
  --host 127.0.0.1 --port 8000 --api-key local-sweep
```

This is a **Red Hat AI quantization of Qwen's model**, not a Qwen-published FP8
checkpoint. No public `Qwen/Qwen3.5-9B-FP8` checkpoint was verified. Red Hat's card
provides a vLLM text-only deployment and its quantization recipe. Its main
`model.safetensors` is 13,520,203,008 bytes, about 12.59 GiB. An additional
486,582,848-byte MTP checkpoint is optional and is not enabled by this command;
both files together total about 13.04 GiB. Text-only serving skips the vision
encoder. The hybrid architecture has eight full-attention layers, giving about
1 GiB of BF16 KV at 32K, plus linear-attention state and runtime memory. This
makes the 17 GiB cap plausible, **not verified**. Do not set `--quantization fp8`
on this checkpoint: its existing `compressed-tensors` configuration selects its
quantization method.

The model thinks by default, so the explicit template setting
prevents hidden reasoning from consuming the output budget. Do not use `/nothink`
as a substitute: its official card says that soft switch is unsupported.

Set `LAB_LLM_MODEL=qwen35-9b-fp8-redhat`,
`LAB_LLM_MODEL_REPOSITORY=RedHatAI/Qwen3.5-9B-FP8-dynamic`, and the revision shown
above. Record a
new serving identity, run both endpoint checks and use a **separate held-out
pilot, protocol and results directory**. The Qwen3-Coder protocol's model identity
must not be relabeled to make it accept this model. Do not select it because an
observed main-sweep result looks better.

The Qwen-published BF16 checkpoint needs about 17.98 GiB for its complete weights
alone and is **not** the fallback for this 17 GiB GPU cap.

## Alternatives and paid API limits

| Option | Practical assessment |
| --- | --- |
| Official Qwen2.5-Coder-14B-Instruct-AWQ | Dense 14.77B, about 9.29 GiB checkpoint files, but about 6 GiB BF16 KV at 32K before runtime. Too tight for a conservative 17 GiB cap. Revision `eb3172f06a6d6b3a15f08947b0668d782e4d2d2c`. |
| QuantTrio Qwen3-Coder-30B AWQ | Its quantizer explicitly warns of substantial 4-bit degradation; not the default. |
| Unsloth Qwen3-Coder-30B Q4_K_M GGUF | About 17.28 GiB file before KV/runtime; would require CPU offload at this cap. Adds a new serving stack and quantization configuration to validate. Revision `b17cb02dd882d5b6ab62fc777ad2995f19668350`. |
| Devstral Small 2 FP8 | About 24.02 GiB weights plus approximately 5 GiB BF16 KV at 32K, before runtime overhead. Too tight for a conservative 30 GB allocation without offload. |
| Paid API | Removes GPU serving pressure but leaves LLVM validation time. Subscription/editor credits are not automatically API credit. Budget and model identity need separate controls. |

For scale, DeepSeek's official Flash API prices on the check date are $0.15 per
million uncached input tokens and $0.60 per million output tokens off-peak; peak
prices double. Six cells Ã— four turns Ã— 25K input and 8K output tokens per turn
would cost about $0.21 off-peak. A 192-cell experiment at that illustrative density
could approach $6.6 off-peak. These are illustrative costs, not measured
usage or a promise that â‚¹200â€“300 covers the experiment. Check minimum top-up and
payment availability before paying. An API run also needs explicit thinking-mode
control and a token/cost ledger; it is a distinct experiment.

## Primary sources

- [Pinned Qwen3-Coder FP8 files](https://huggingface.co/Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8/tree/dcaee4d4dfc5ee71ad501f01f530e5652438fde0)
- [vLLM 0.28.0 release artifacts](https://github.com/vllm-project/vllm/releases/tag/v0.28.0), [exact wheel list and checksums](https://github.com/vllm-project/vllm/releases/expanded_assets/v0.28.0), and [installation options](https://docs.vllm.ai/en/v0.28.0/getting_started/installation/gpu/)
- [NVIDIA CUDA 12.9 driver versions](https://docs.nvidia.com/cuda/archive/12.9.0/cuda-toolkit-release-notes/index.html), [CUDA 13.0 driver versions](https://docs.nvidia.com/cuda/archive/13.0.0/cuda-toolkit-release-notes/index.html), and [minor-compatibility limitations](https://docs.nvidia.com/deploy/cuda-compatibility/minor-version-compatibility.html)
- [vLLM 0.28.0 serving options](https://docs.vllm.ai/en/v0.28.0/cli/serve/)
- [Tagged Qwen3 MoE implementation](https://raw.githubusercontent.com/vllm-project/vllm/v0.28.0/vllm/model_executor/models/qwen3_moe.py), [layer offloader](https://raw.githubusercontent.com/vllm-project/vllm/v0.28.0/vllm/model_executor/offloader/uva.py), and [quantization loader](https://raw.githubusercontent.com/vllm-project/vllm/v0.28.0/vllm/model_executor/model_loader/utils.py)
- [Official Qwen3.5-9B card](https://huggingface.co/Qwen/Qwen3.5-9B), [pinned files](https://huggingface.co/Qwen/Qwen3.5-9B/tree/c202236235762e1c871ad0ccb60c8ee5ba337b9a), and [vLLM supported architectures](https://docs.vllm.ai/en/v0.28.0/models/supported_models/)
- [Red Hat AI's FP8 quantization and deployment recipe](https://huggingface.co/RedHatAI/Qwen3.5-9B-FP8-dynamic), [pinned files](https://huggingface.co/RedHatAI/Qwen3.5-9B-FP8-dynamic/tree/790f0576d2d77dd5322aa0603a470bd9e3a3d1f6), and [pinned quantization configuration](https://huggingface.co/RedHatAI/Qwen3.5-9B-FP8-dynamic/blob/790f0576d2d77dd5322aa0603a470bd9e3a3d1f6/config.json)
- [Official dense 14B AWQ card](https://huggingface.co/Qwen/Qwen2.5-Coder-14B-Instruct-AWQ)
- [QuantTrio's quantization warning](https://huggingface.co/QuantTrio/Qwen3-Coder-30B-A3B-Instruct-AWQ)
- [Unsloth GGUF files](https://huggingface.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF/tree/b17cb02dd882d5b6ab62fc777ad2995f19668350) and [llama.cpp](https://github.com/ggml-org/llama.cpp)
- [Official Devstral Small 2 card](https://huggingface.co/mistralai/Devstral-Small-2-24B-Instruct-2512)
- [DeepSeek API pricing](https://api-docs.deepseek.com/quick_start/pricing/)
