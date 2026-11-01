"""Launch receipts preserve identity and enforce fresh-config resource gates."""
import copy
import json
from pathlib import Path
import shlex
from types import SimpleNamespace

import pytest

from scripts import configure_shared_h100 as configure


@pytest.fixture
def machine(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime with 'quote $literal"
    venv = tmp_path / "vllm env with 'quote $literal"
    runtime.mkdir()
    (runtime / "env.sh").write_text("export LAB_DATASET_DIR=/data/bugs\n", encoding="utf-8")
    (venv / "bin").mkdir(parents=True)
    for name in ("python", "activate", "vllm"):
        (venv / "bin" / name).write_text("", encoding="utf-8")
    gpu = {"index": 0, "uuid": "GPU-01234567-aaaa-bbbb-cccc-0123456789ab", "name": "NVIDIA H100 PCIe",
           "total_mib": 81559, "free_mib": 21796, "budget_mib": 17372,
           "gpu_memory_utilization": 0.213, "offload20_profile_memory_gate": True}
    state = SimpleNamespace(
        runtime=runtime, venv=venv, out=tmp_path / "output with 'quote $literal", selectors=[], calls=[],
        resources={"captured_at_utc": "2026-11-01T12:00:00+00:00", "selected_gpu": gpu,
                   "available_ram_gib": 928.0, "cgroup_limits": {"memory": {"known": True}}},
        version="0.28.0", packages="torch==2.13.0\nvllm==0.28.0", commit="a" * 40,
        driver="580.178.04", driver_uuid=None,
    )

    def collect(selector):
        state.selectors.append(selector)
        return copy.deepcopy(state.resources)

    def capture(argv):
        state.calls.append(argv)
        if argv[:2] == ["git", "rev-parse"]:
            return state.commit
        if argv[0] == "nvidia-smi":
            return f"{state.driver_uuid or state.resources['selected_gpu']['uuid']}, {state.driver}"
        assert argv[0] == str(venv / "bin/python")
        if argv[1] == "-c":
            return state.version
        assert argv[1:] == ["-m", "pip", "freeze"]
        return state.packages

    monkeypatch.setattr(configure.resource_plan, "collect", collect)
    monkeypatch.setattr(configure, "capture", capture)
    return state


def write(machine):
    return configure.configure(0, machine.venv, machine.runtime, machine.out)


def test_fresh_configuration_binds_frozen_protocol_and_quotes_bash_paths(machine):
    assert write(machine) is False
    assert machine.selectors == ["0"]
    receipt = json.loads((machine.out / "receipt.json").read_text())
    identity = receipt["identity"]
    argv = identity["serving_argv"]
    assert argv[:3] == [str(machine.venv / "bin/vllm"), "serve", "Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8"]
    for option, value in {"--cpu-offload-gb": "20", "--gpu-memory-utilization": "0.213",
                          "--max-model-len": "32768", "--max-num-seqs": "1",
                          "--max-num-batched-tokens": "2048", "--generation-config": "vllm"}.items():
        assert argv[argv.index(option) + 1] == value
    assert "--enforce-eager" in argv
    assert "--enable-chunked-prefill" in argv
    assert identity["source_commit"] == "a" * 40
    assert identity["driver_version"] == "580.178.04"
    assert identity["gpu"]["uuid"] == machine.resources["selected_gpu"]["uuid"]
    env = (machine.out / "env.sh").read_text()
    exports = dict(shlex.split(line)[1].split("=", 1) for line in env.splitlines() if line.startswith("export "))
    assert shlex.split(env.splitlines()[1]) == ["source", str(machine.runtime / "env.sh")]
    assert exports["CUDA_VISIBLE_DEVICES"] == identity["gpu"]["uuid"]
    assert exports["LAB_LLM_MODEL"] == "qwen3-coder-30b-fp8-offload"
    assert exports["LAB_LLM_MAX_TOKENS"] == "8192"
    assert exports["LAB_LLM_TEMP"] == "0.8"
    assert exports["BC_GPU_UTIL"] == "0.213"
    assert exports["LAB_LLM_SERVING_ID"] == receipt["serving_identity"]
    assert len(exports["LAB_LLM_MODEL_REVISION"]) == 40
    server = (machine.out / "serve.sh").read_text().splitlines()
    assert shlex.split(server[2]) == ["source", str(machine.out / "env.sh")]
    assert shlex.split(server[3]) == ["source", str(machine.venv / "bin/activate")]
    assert shlex.split(server[4]) == ["exec", *argv]
    assert all(b"\r\n" not in (machine.out / name).read_bytes() for name in ("env.sh", "serve.sh"))
    assert all("serve" not in call[1:] for call in machine.calls)  # Never launch a server.


@pytest.mark.parametrize("gate", ["no_gpu", "gpu_low", "gpu_unknown", "ram_low", "ram_unknown", "cgroup_unknown"])
def test_fresh_configuration_refuses_unsafe_or_unknown_effective_limits_without_writing(machine, gate):
    if gate == "no_gpu":
        machine.resources["selected_gpu"] = None
    elif gate == "gpu_low":
        machine.resources["selected_gpu"]["budget_mib"] = 16383
    elif gate == "gpu_unknown":
        machine.resources["selected_gpu"]["offload20_profile_memory_gate"] = False
    elif gate == "ram_low":
        machine.resources["available_ram_gib"] = 63.99
    elif gate == "ram_unknown":
        machine.resources["available_ram_gib"] = None
    else:
        machine.resources["cgroup_limits"]["memory"]["known"] = False
    with pytest.raises(ValueError):
        write(machine)
    assert not machine.out.exists()
    assert machine.calls == []


def test_reuse_keeps_original_receipt_and_budget_while_server_consumes_resources(machine, capsys):
    write(machine)
    initial = {path.name: path.read_bytes() for path in machine.out.iterdir()}
    machine.resources["captured_at_utc"] = "2026-11-01T15:00:00+00:00"
    machine.resources["selected_gpu"].update(free_mib=1000, budget_mib=0,
                                            gpu_memory_utilization=0, offload20_profile_memory_gate=False)
    machine.resources["available_ram_gib"] = None
    assert write(machine) is True
    assert {path.name: path.read_bytes() for path in machine.out.iterdir()} == initial
    assert "current free-memory gates were not reapplied" in capsys.readouterr().out


@pytest.mark.parametrize("change", ["packages", "commit", "driver", "gpu_uuid", "runtime_env"])
def test_changed_serving_identity_is_refused_and_original_files_remain(machine, change):
    write(machine)
    initial = {path.name: path.read_bytes() for path in machine.out.iterdir()}
    if change == "packages":
        machine.packages += "\nchanged==1.0"
    elif change == "commit":
        machine.commit = "b" * 40
    elif change == "driver":
        machine.driver = "580.999.00"
    elif change == "gpu_uuid":
        machine.resources["selected_gpu"]["uuid"] = "GPU-another-device"
    else:
        (machine.runtime / "env.sh").write_text("changed runtime exports\n")
    with pytest.raises(ValueError, match="Existing receipt differs"):
        write(machine)
    assert {path.name: path.read_bytes() for path in machine.out.iterdir()} == initial


@pytest.mark.parametrize("name", ["env.sh", "serve.sh", "resources.json", "packages.txt"])
def test_modified_saved_artifact_is_never_overwritten(machine, name):
    write(machine)
    path = machine.out / name
    path.write_text("changed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="artifact changed"):
        write(machine)
    assert path.read_text() == "changed\n"


def test_partial_output_directory_is_preserved_and_refused(machine):
    machine.out.mkdir()
    (machine.out / "env.sh").write_text("keep this\n")
    with pytest.raises(ValueError, match="without a receipt"):
        write(machine)
    assert (machine.out / "env.sh").read_text() == "keep this\n"


@pytest.mark.parametrize("version,accepted", [("0.28.0+cu129", True), ("0.27.0", False),
                                              ("0.28.0rc1", False)])
def test_server_distribution_metadata_is_pinned(machine, version, accepted):
    machine.version = version
    if accepted:
        assert write(machine) is False
    else:
        with pytest.raises(ValueError, match="needs vLLM 0.28.0"):
            write(machine)
        assert not machine.out.exists()


def test_driver_probe_must_match_exact_selected_uuid(machine):
    machine.driver_uuid = "GPU-wrong-device"
    with pytest.raises(ValueError, match="driver receipt"):
        write(machine)
    assert not machine.out.exists()


def test_exact_resource_gate_boundaries_are_allowed(machine):
    machine.resources["available_ram_gib"] = 64
    machine.resources["selected_gpu"]["budget_mib"] = 16384
    assert write(machine) is False


def test_profile_must_retain_frozen_offload_amount(machine, monkeypatch):
    protocol, protocol_sha = configure.load_protocol(str(configure.PROTOCOL))
    protocol["serving_profile"]["cpu_offload_gb"] = 12
    monkeypatch.setattr(configure, "load_protocol", lambda *args: (protocol, protocol_sha))
    with pytest.raises(ValueError, match="20 GiB offload"):
        write(machine)
    assert not machine.out.exists()


def test_missing_runtime_export_file_is_refused_before_hardware_or_configuration(machine):
    (machine.runtime / "env.sh").unlink()
    with pytest.raises(ValueError, match="Required runtime/server file is missing"):
        write(machine)
    assert machine.selectors == []
    assert not machine.out.exists()


def test_cli_defaults_use_native_runtime_and_vllm_environment(machine, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(configure.Path, "home", lambda: Path("/home/tester"))
    monkeypatch.setattr(configure, "configure", lambda *args: calls.append(args))
    assert configure.main([]) == 0
    assert calls == [(0, Path("/home/tester/vllm-venv"), Path("/home/tester/better-compiler-runtime"),
                      Path("run-provenance/shared-h100"))]
    assert "No server started" in capsys.readouterr().out
