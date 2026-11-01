#!/usr/bin/env python3
"""Freeze a shared-H100 launch and runner environment without starting a server."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ce.protocol import load_protocol
from scripts import resource_plan

PROTOCOL = ROOT / "data/repair_v3_shared_h100_protocol.json"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_text(value: dict) -> str:
    return json.dumps(value, sort_keys=True, indent=2) + "\n"


def capture(argv: list[str]) -> str:
    return subprocess.check_output(argv, cwd=ROOT, text=True, timeout=60).strip()


def configure(gpu_index: int, venv: Path, runtime: Path, out: Path) -> bool:
    """Write once; return True only when an identical intact receipt was reused."""
    protocol, protocol_sha = load_protocol(str(PROTOCOL))
    profile = protocol["serving_profile"]
    if profile["cpu_offload_gb"] != 20 or profile["vllm"] != "0.28.0":
        raise ValueError("This helper requires the frozen 20 GiB offload / vLLM 0.28.0 profile")
    venv, runtime, out = venv.expanduser().resolve(), runtime.expanduser().resolve(), out.expanduser().resolve()
    runtime_env = runtime / "env.sh"
    python, activate, server = (venv / "bin" / name for name in ("python", "activate", "vllm"))
    for path in (runtime_env, python, activate, server):
        if not path.is_file():
            raise ValueError(f"Required runtime/server file is missing: {path}")
    existing = None
    receipt_path = out / "receipt.json"
    if out.exists() and any(out.iterdir()):
        if not receipt_path.is_file():
            raise ValueError(f"Refusing partial/existing configuration without a receipt: {out}")
        existing = json.loads(receipt_path.read_text(encoding="utf-8"))
        if (not isinstance(existing, dict) or not isinstance(existing.get("identity"), dict)
                or not isinstance(existing.get("artifacts_sha256"), dict)):
            raise ValueError(f"Existing receipt is malformed; preserve it and choose a new --out: {out}")
        for name in ("env.sh", "serve.sh", "resources.json", "packages.txt"):
            path = out / name
            if not path.is_file() or digest(path.read_bytes()) != existing.get("artifacts_sha256", {}).get(name):
                raise ValueError(f"Existing configuration artifact changed or is missing: {path}")
    resources = resource_plan.collect(str(gpu_index))
    gpu = resources.get("selected_gpu")
    if not gpu:
        raise ValueError("The selected physical GPU is unavailable")
    ram = resources.get("available_ram_gib")
    known = resources.get("cgroup_limits", {}).get("memory", {}).get("known")
    if existing is None:
        if not gpu.get("offload20_profile_memory_gate") or gpu.get("budget_mib", 0) < 16384:
            raise ValueError("Selected GPU does not pass the 20 GiB offload profile's 16 GiB GPU memory gate")
        if known is not True or not isinstance(ram, (int, float)) or not math.isfinite(ram) or ram < 64:
            raise ValueError("Need known effective container RAM availability of at least 64 GiB")
    version = capture([str(python), "-c", "from importlib.metadata import version; print(version('vllm'))"])
    if version.split("+", 1)[0] != "0.28.0":
        raise ValueError(f"Server environment needs vLLM 0.28.0; found {version!r}")
    packages = capture([str(python), "-m", "pip", "freeze"]) + "\n"
    driver_rows = list(csv.reader(capture([
        "nvidia-smi", f"--id={gpu['uuid']}", "--query-gpu=uuid,driver_version",
        "--format=csv,noheader,nounits",
    ]).splitlines(), skipinitialspace=True))
    if len(driver_rows) != 1 or len(driver_rows[0]) != 2 or driver_rows[0][0].strip() != gpu["uuid"]:
        raise ValueError("Could not bind the driver receipt to the selected GPU UUID")
    driver = driver_rows[0][1].strip()
    if not driver:
        raise ValueError("Selected GPU driver version is unavailable")
    commit = capture(["git", "rev-parse", "HEAD"])
    # Reuse retains its original budget even while the server consumes the free
    # memory. It verifies identity/integrity, not permission to launch anew.
    fraction = float(existing["identity"]["gpu_memory_utilization"] if existing else gpu["gpu_memory_utilization"])
    if not math.isfinite(fraction) or not 0 < fraction < 1:
        raise ValueError("Hardware doctor returned an invalid GPU memory fraction")
    util = f"{fraction:.3f}"
    argv = [str(server), "serve", protocol["model_repository"],
            "--revision", protocol["model_revision"], "--tokenizer-revision", protocol["model_revision"],
            "--served-model-name", protocol["model"], "--dtype", "bfloat16",
            "--cpu-offload-gb", str(profile["cpu_offload_gb"]), "--gpu-memory-utilization", util,
            "--max-model-len", str(profile["max_model_len"]), "--max-num-seqs", str(profile["max_num_seqs"]),
            "--max-num-batched-tokens", str(profile["max_num_batched_tokens"]), "--enable-chunked-prefill"]
    if profile["enforce_eager"]:
        argv.append("--enforce-eager")
    argv += ["--generation-config", profile["generation_config"],
             "--host", "127.0.0.1", "--port", "8000", "--api-key", "local-sweep"]
    gpu_receipt = {key: gpu[key] for key in ("index", "uuid", "name", "total_mib")}
    gpu_receipt["budget_mib"] = existing["identity"]["gpu"]["budget_mib"] if existing else gpu["budget_mib"]
    identity = {"schema": 1, "protocol_id": protocol["id"], "protocol_sha256": protocol_sha,
                "source_commit": commit, "source_root": str(ROOT), "runtime_root": str(runtime),
                "runtime_env_sha256": digest(runtime_env.read_bytes()), "vllm_venv": str(venv),
                "output_root": str(out), "vllm_version": version, "packages_sha256": digest(packages.encode()),
                "gpu": gpu_receipt, "gpu_memory_utilization": fraction,
                "driver_version": driver, "serving_argv": argv}
    serving_id = "shared-h100-" + digest(json_text(identity).encode())
    exports = {"LAB_LLM_URL": "http://127.0.0.1:8000/v1", "LAB_LLM_TOKEN": "local-sweep",
               "LAB_LLM_MODEL": protocol["model"], "LAB_LLM_MODEL_REPOSITORY": protocol["model_repository"],
               "LAB_LLM_MODEL_REVISION": protocol["model_revision"], "LAB_LLM_TEMP": str(protocol["temperature"]),
               "LAB_LLM_MAX_TOKENS": str(protocol["max_completion_tokens"]), "LAB_LLM_SERVING_ID": serving_id,
               "CUDA_VISIBLE_DEVICES": gpu["uuid"], "BC_GPU_UTIL": util, "VLLM_USE_FLASHINFER_SAMPLER": "0"}
    environment = ("#!/usr/bin/env bash\n" + f"source {shlex.quote(str(runtime_env))}\n" +
                   "".join(f"export {key}={shlex.quote(value)}\n" for key, value in exports.items()))
    serve = ("#!/usr/bin/env bash\nset -euo pipefail\n" +
             f"source {shlex.quote(str(out / 'env.sh'))}\nsource {shlex.quote(str(activate))}\n" +
             "exec " + shlex.join(argv) + "\n")
    contents = {"env.sh": environment, "serve.sh": serve, "resources.json": json_text(resources),
                "packages.txt": packages}
    receipt = {"identity": identity, "serving_identity": serving_id,
               "artifacts_sha256": {name: digest(value.encode()) for name, value in contents.items()}}
    if existing is not None:
        if existing.get("identity") != identity or existing.get("serving_identity") != serving_id:
            raise ValueError(f"Existing receipt differs; preserve it and choose a new --out: {out}")
        print(f"Reusing identical configuration: {receipt_path}")
        print("Saved launch budget retained; current free-memory gates were not reapplied to a running server.")
        return True
    out.mkdir(parents=True, exist_ok=True)
    for name, value in {**contents, "receipt.json": json_text(receipt)}.items():
        path = out / name
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
        path.chmod(0o700 if name.endswith(".sh") else 0o600)
    print(f"Saved configuration: {receipt_path}")
    return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gpu-index", type=int, default=0, help="physical nvidia-smi GPU index (default: 0)")
    parser.add_argument("--vllm-venv", type=Path, default=Path.home() / "vllm-venv")
    parser.add_argument("--runtime-root", type=Path, default=Path.home() / "better-compiler-runtime")
    parser.add_argument("--out", type=Path, default=Path("run-provenance/shared-h100"))
    args = parser.parse_args(argv)
    if args.gpu_index < 0:
        parser.error("--gpu-index must be nonnegative")
    try:
        configure(args.gpu_index, args.vllm_venv, args.runtime_root, args.out)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f"Configuration refused: {error}", file=sys.stderr)
        return 2
    out = args.out.expanduser().resolve()
    print(f"Runner shell: source {shlex.quote(str(out / 'env.sh'))}")
    print(f"Server shell: bash {shlex.quote(str(out / 'serve.sh'))}")
    print("No server started. Recheck shared resources; this snapshot does not reserve them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
