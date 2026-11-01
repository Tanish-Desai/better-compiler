"""A host-wide CPU count or free-VRAM number is not the container's budget."""
import json
from types import SimpleNamespace

import pytest

import scripts.resource_plan as planner
from scripts.resource_plan import (GIB, build_jobs, cgroup_location, container_limits,
                                   cpu_limit, gpu_plan, memory_available, select_gpu)


def test_container_ram_headroom_limits_builds_even_on_large_host():
    available = memory_available("MemAvailable: 500000000 kB", str(16 * GIB), str(10 * GIB))
    assert available == 6 * GIB
    assert build_jobs(128, available) == 2


def test_host_pressure_and_cgroup_unlimited_are_respected():
    assert memory_available("MemAvailable: 1024 kB", "max", "0") == 1024 ** 2
    assert memory_available("MemAvailable: 1024 kB", str(4 * GIB), str(5 * GIB)) == 0
    assert build_jobs(128, 100 * GIB) == 8
    assert build_jobs(1, 100 * GIB) == 1
    assert build_jobs(64, None) == 2


@pytest.mark.parametrize("quota,expected", [("400000 100000", 4), ("150000 100000", 1),
                                           ("max 100000", 32), ("-1 100000", 32),
                                           ("invalid", 32)])
def test_cpu_quota(quota, expected):
    # Negative quota is cgroup v1's unlimited sentinel.
    assert cpu_limit(32, quota) == expected


def test_gpu_fraction_is_of_total_memory_and_reserves_other_tenants_headroom():
    plan = gpu_plan(81920, 30720)
    assert plan["gpu_memory_utilization"] == 0.212
    assert 16384 <= plan["budget_mib"] <= 17408
    assert plan["offload20_profile_memory_gate"]
    assert gpu_plan(81920, 20000)["offload20_profile_memory_gate"] is False
    assert gpu_plan(81920, 50000) == plan


def test_larger_card_does_not_turn_thirty_percent_into_an_unsafe_allocation():
    plan = gpu_plan(98304, 30720)
    assert plan["gpu_memory_utilization"] == 0.177
    assert 16384 <= plan["budget_mib"] <= 17408


@pytest.mark.parametrize("total,free", [(0, 0), (100, -1), (100, 101)])
def test_bad_gpu_snapshots_are_rejected(total, free):
    with pytest.raises(ValueError):
        gpu_plan(total, free)


def test_actual_shared_h100_snapshot_fits_fixed_offload20_memory_gate():
    plan = gpu_plan(81559, 21796)
    assert plan["gpu_memory_utilization"] == 0.213
    assert 16384 <= plan["budget_mib"] <= 17408
    assert 21796 - plan["budget_mib"] >= 4096
    assert plan["offload20_profile_memory_gate"]


V2_MOUNT = "30 20 0:25 / /sys/fs/cgroup rw - cgroup2 cgroup rw"
HOST_MEM = "MemAvailable: 134217728 kB"


def test_nested_v2_limits_include_parent_cpu_and_ram_headroom():
    files = {}
    for path, quota, maximum, current in [
        ("/sys/fs/cgroup", "max 100000", "max", 60 * GIB),
        ("/sys/fs/cgroup/team", "200000 100000", str(16 * GIB), 10 * GIB),
        ("/sys/fs/cgroup/team/job", "800000 100000", str(12 * GIB), 1 * GIB),
    ]:
        files.update({path + "/cpu.max": quota, path + "/memory.max": maximum,
                      path + "/memory.current": str(current)})
    result = container_limits(32, HOST_MEM, "0::/team/job", V2_MOUNT, files.get)
    assert result["effective_cpus"] == 2
    assert result["available_bytes"] == 6 * GIB
    assert result["cgroup_limits"]["memory"]["location"]["paths"] == [
        "/sys/fs/cgroup/team/job", "/sys/fs/cgroup/team", "/sys/fs/cgroup"]
    assert not result["warnings"]


def test_mount_root_mapping_does_not_duplicate_process_path():
    mount = "30 20 0:25 /team /sys/fs/cgroup rw - cgroup2 cgroup rw"
    location = cgroup_location("0::/team/job", mount, "memory")
    assert location["paths"] == ["/sys/fs/cgroup/job", "/sys/fs/cgroup"]
    assert location["mount_root"] == "/team"
    assert cgroup_location("0::/outside/job", mount, "memory") is None


def test_mountinfo_escaped_paths_are_decoded():
    mount = r"30 20 0:25 /team /sys/fs/cgroup/my\040slice rw - cgroup2 cgroup rw"
    assert cgroup_location("0::/team/job", mount, "cpu")["paths"] == [
        "/sys/fs/cgroup/my slice/job", "/sys/fs/cgroup/my slice"]


def test_v1_separate_controllers_use_their_own_current_paths():
    membership = "3:cpu,cpuacct:/jobs/a\n2:memory:/jobs/b"
    mounts = ("30 20 0:25 /jobs /sys/fs/cgroup/cpu rw - cgroup cgroup rw,cpu,cpuacct\n"
              "31 20 0:26 /jobs /sys/fs/cgroup/memory rw - cgroup cgroup rw,memory")
    files = {
        "/sys/fs/cgroup/cpu/a/cpu.cfs_quota_us": "400000",
        "/sys/fs/cgroup/cpu/a/cpu.cfs_period_us": "100000",
        "/sys/fs/cgroup/cpu/cpu.cfs_quota_us": "200000",
        "/sys/fs/cgroup/cpu/cpu.cfs_period_us": "100000",
        "/sys/fs/cgroup/memory/b/memory.limit_in_bytes": str(10 * GIB),
        "/sys/fs/cgroup/memory/b/memory.usage_in_bytes": str(2 * GIB),
        "/sys/fs/cgroup/memory/memory.limit_in_bytes": str(16 * GIB),
        "/sys/fs/cgroup/memory/memory.usage_in_bytes": str(12 * GIB),
    }
    result = container_limits(32, HOST_MEM, membership, mounts, lambda path: files.get(path, ""))
    assert result["effective_cpus"] == 2
    assert result["available_bytes"] == 4 * GIB
    assert result["cgroup_limits"]["cpu"]["location"]["paths"][0] == "/sys/fs/cgroup/cpu/a"
    assert any("ancestors outside mount root" in warning for warning in result["warnings"])


@pytest.mark.parametrize("membership,mounts", [("", ""), ("0::/other", V2_MOUNT.replace(" / /", " /team /")),
                                               ("0::/../escape", V2_MOUNT)])
def test_unresolvable_cgroups_never_use_host_ram_as_a_container_guarantee(membership, mounts):
    result = container_limits(64, HOST_MEM, membership, mounts, lambda path: "")
    assert result["effective_cpus"] == 1
    assert result["available_bytes"] is None
    assert result["host_available_bytes"] == 128 * GIB
    assert len(result["warnings"]) == 2


def test_malformed_ancestor_limit_forces_unknown_and_zero_limit_is_real():
    files = {"/sys/fs/cgroup/job/cpu.max": "800000 100000",
             "/sys/fs/cgroup/cpu.max": "invalid",
             "/sys/fs/cgroup/job/memory.max": "0", "/sys/fs/cgroup/job/memory.current": "0"}
    result = container_limits(64, HOST_MEM, "0::/job", V2_MOUNT, lambda path: files.get(path, ""))
    assert result["effective_cpus"] == 1
    assert result["available_bytes"] == 0


GPUS = [{"index": 0, "uuid": "GPU-first", "name": "H100", "total_mib": 81559, "free_mib": 21796},
        {"index": 3, "uuid": "GPU-other", "name": "H100", "total_mib": 81559, "free_mib": 50000}]


def test_gpu_is_never_selected_implicitly_even_when_inventory_is_nonempty():
    gpu, selection = select_gpu(GPUS, None, "3")
    assert gpu is None
    assert selection["requested"] is None
    assert "No GPU selected" in selection["warnings"][0]


@pytest.mark.parametrize("selector", ["0", "GPU-first"])
def test_explicit_physical_index_or_uuid_identifies_exact_gpu(selector):
    gpu, selection = select_gpu(GPUS, selector, "GPU-first")
    assert gpu["uuid"] == "GPU-first"
    assert selection["physical_index"] == 0
    assert selection["cuda_mapping_verified"]
    assert selection["selected_cuda_ordinal"] == 0


def test_numeric_cuda_visibility_mismatch_is_flagged_without_rewriting_environment(monkeypatch):
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "3")
    gpu, selection = select_gpu(GPUS, "0", planner.os.environ["CUDA_VISIBLE_DEVICES"])
    assert gpu["index"] == 0
    assert not selection["cuda_mapping_verified"]
    assert any("does not include" in warning for warning in selection["warnings"])
    assert selection["recommended_cuda_visible_devices"] == "GPU-first"
    assert planner.os.environ["CUDA_VISIBLE_DEVICES"] == "3"


def test_matching_numeric_cuda_index_is_still_unverified():
    _, selection = select_gpu(GPUS, "0", "0")
    assert not selection["cuda_mapping_verified"]
    assert any("CUDA_VISIBLE_DEVICES=GPU-first" in warning for warning in selection["warnings"])


def test_uuid_in_multiple_visible_gpus_records_ordinal_and_requests_single_device_binding():
    _, selection = select_gpu(GPUS, "0", "GPU-other,GPU-first")
    assert selection["selected_cuda_ordinal"] == 1
    assert not selection["cuda_mapping_verified"]


def test_unknown_explicit_gpu_selection_fails_cli_and_is_saved(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(planner, "read_text", lambda path: "")
    monkeypatch.setattr(planner.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=
        "0, GPU-first, H100, 81559, 21796\n3, GPU-other, H100, 81559, 50000\n"))
    output = tmp_path / "snapshot.json"
    assert planner.main(["--gpu-index", "2", "--out", str(output)]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["selected_gpu"] is None
    assert "does not uniquely match" in report["gpu_selection"]["error"]
    assert json.loads(output.read_text()) == report


def test_collect_records_selected_uuid_and_mapping_and_actual_capture_time(monkeypatch):
    monkeypatch.setattr(planner, "read_text", lambda path: "")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "GPU-first")
    monkeypatch.setattr(planner.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=
        '0, GPU-first, "H100, PCIe", 81559, 21796\n3, GPU-other, H100, 81559, 50000\n'))
    report = planner.collect("0")
    assert report["selected_gpu"]["uuid"] == "GPU-first"
    assert report["selected_gpu"]["name"] == "H100, PCIe"
    assert report["selected_gpu"]["gpu_memory_utilization"] == 0.213
    assert report["gpu_selection"]["cuda_mapping_verified"]
    assert report["captured_at_utc"].endswith("+00:00")
