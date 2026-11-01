#!/usr/bin/env python3
"""Read host/container limits before choosing compile jobs or a GPU budget.

No processes are stopped and no GPU memory is reserved. A free-memory snapshot
cannot prevent another tenant from allocating memory after this check.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import subprocess

GIB = 1024 ** 3


def read_text(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def positive_limit(text: str) -> int | None:
    try:
        value = int(text)
    except ValueError:
        return None
    return value if 0 < value < 2 ** 60 else None


def memory_available(meminfo: str, maximum: str = "", current: str = "") -> int | None:
    fields = {line.split(":")[0]: line.split(":", 1)[1].strip()
              for line in meminfo.splitlines() if ":" in line}
    host_fields = fields.get("MemAvailable", "").split()
    host = positive_limit(host_fields[0] if host_fields else "")
    host = host * 1024 if host is not None else None
    try:
        limit = int(maximum)
        limit = limit if 0 <= limit < 2 ** 60 else None
    except ValueError:
        limit = None
    try:
        used = max(0, int(current))
    except ValueError:
        used = None
    container = max(0, limit - used) if limit is not None and used is not None else None
    values = [value for value in (host, container) if value is not None]
    return min(values) if values else None


def cpu_limit(affinity: int, quota: str = "") -> int:
    fields = quota.split()
    try:
        if len(fields) == 2 and fields[0] != "max":
            amount, period = int(fields[0]), int(fields[1])
            if amount > 0 and period > 0:
                return max(1, min(affinity, math.floor(amount / period)))
    except (ValueError, ZeroDivisionError):
        pass
    return max(1, affinity)


def cgroup_location(cgroups: str, mountinfo: str, controller: str) -> dict | None:
    """Map the process membership through a matching cgroup mount's root."""
    memberships = {}
    for line in cgroups.splitlines():
        fields = line.split(":", 2)
        if len(fields) == 3:
            for name in fields[1].split(","):
                memberships[name] = fields[2]
    name = controller if controller in memberships else ""
    membership = memberships.get(name)
    if not membership or not membership.startswith("/") or ".." in PurePosixPath(membership).parts:
        return None
    candidates = []
    for line in mountinfo.splitlines():
        before, separator, after = line.partition(" - ")
        left, right = before.split(), after.split()
        if not separator or len(left) < 6 or len(right) < 3:
            continue
        version = 2 if right[0] == "cgroup2" else 1 if right[0] == "cgroup" else 0
        if not version or (version == 2) != (name == ""):
            continue
        if version == 1 and controller not in right[2].split(","):
            continue
        unescape = lambda value: re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)
        root, mount = PurePosixPath(unescape(left[3])), PurePosixPath(unescape(left[4]))
        if not root.is_absolute() or not mount.is_absolute() or ".." in (*root.parts, *mount.parts):
            continue
        try:
            relative = PurePosixPath(membership).relative_to(root)
        except ValueError:
            continue
        current = mount / relative
        paths = [str(current)]
        while current != mount:
            current = current.parent
            paths.append(str(current))
        candidates.append((len(root.parts), {"version": version, "membership": membership,
                                             "mount_root": str(root), "mount_point": str(mount),
                                             "paths": paths}))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def container_limits(affinity: int, meminfo: str, cgroups: str, mountinfo: str, reader=read_text) -> dict:
    """Take the tightest readable ancestor limits; never infer host-only safety."""
    host_available = memory_available(meminfo)
    available, cpus = host_available, max(1, affinity)
    details, warnings = {}, []
    for controller in ("cpu", "memory"):
        location = cgroup_location(cgroups, mountinfo, controller)
        details[controller] = {"location": location, "checks": [], "known": False}
        if location:
            malformed = False
            for path in location["paths"]:
                if controller == "cpu":
                    quota = (reader(path + "/cpu.max") if location["version"] == 2 else
                             " ".join((reader(path + "/cpu.cfs_quota_us"),
                                       reader(path + "/cpu.cfs_period_us"))))
                    fields = quota.split()
                    if not fields:
                        continue  # Root cgroups may not expose a quota file.
                    try:
                        valid = (len(fields) == 2 and int(fields[1]) > 0 and
                                 (fields[0] == "max" or int(fields[0]) == -1 or int(fields[0]) > 0))
                    except ValueError:
                        valid = False
                    malformed |= not valid
                    if valid:
                        cpus = cpu_limit(cpus, quota)
                        details[controller]["checks"].append({"path": path, "quota": quota})
                else:
                    maximum = reader(path + ("/memory.max" if location["version"] == 2 else
                                             "/memory.limit_in_bytes"))
                    if not maximum:
                        continue
                    current = reader(path + ("/memory.current" if location["version"] == 2 else
                                             "/memory.usage_in_bytes"))
                    try:
                        unlimited = maximum == "max" or int(maximum) >= 2 ** 60
                        valid = unlimited or (int(maximum) >= 0 and int(current) >= 0)
                    except ValueError:
                        valid = False
                    malformed |= not valid
                    if valid:
                        headroom = memory_available(meminfo, maximum, current)
                        if headroom is not None:
                            available = min(available, headroom) if available is not None else headroom
                        details[controller]["checks"].append({"path": path, "maximum": maximum,
                                                              "current": current})
            details[controller]["known"] = bool(details[controller]["checks"]) and not malformed
        if not details[controller]["known"]:
            warnings.append(f"Cannot determine the process's {controller} cgroup limits; conservative unknown fallback.")
            if controller == "cpu":
                cpus = 1
            else:
                available = None
        if location and location["mount_root"] != "/":
            warnings.append(f"{controller}: ancestors outside mount root {location['mount_root']} are not visible.")
    return {"effective_cpus": cpus, "available_bytes": available,
            "host_available_bytes": host_available, "cgroup_limits": details, "warnings": warnings}


def build_jobs(cpus: int, available_bytes: int | None) -> int:
    # Conservative starting point for shared LLVM builds, not a peak-RAM bound.
    memory_jobs = max(1, int(available_bytes / GIB / 3)) if available_bytes is not None else 2
    return max(1, min(8, cpus, memory_jobs))


def gpu_plan(total_mib: int, free_mib: int, reserve_mib: int = 4096) -> dict:
    if total_mib <= 0 or not 0 <= free_mib <= total_mib or reserve_mib < 0:
        raise ValueError("Invalid GPU memory snapshot")
    # Fixed 17 GiB GPU cap for the 20 GiB CPU weight-offload profile, even
    # when a larger share is free. Leave
    # another 4 GiB beyond that allocation for snapshot drift/other tenants.
    budget = max(0, min(17408, free_mib - reserve_mib))
    fraction = math.floor(1000 * budget / total_mib) / 1000
    allocated = math.floor(total_mib * fraction)
    return {"budget_mib": allocated, "gpu_memory_utilization": fraction,
            "reserve_mib": reserve_mib,
            "offload20_profile_memory_gate": allocated >= 16384}


def select_gpu(gpus: list[dict], selector: str | None, visible: str | None) -> tuple[dict | None, dict]:
    selection = {"requested": selector, "cuda_visible_devices": visible,
                 "cuda_mapping_verified": False, "warnings": []}
    if selector is None:
        selection["warnings"].append("No GPU selected. Pass --gpu-index PHYSICAL_INDEX or --gpu-uuid UUID after checking nvidia-smi -L.")
        return None, selection
    matches = [gpu for gpu in gpus if selector in (str(gpu["index"]), gpu["uuid"])]
    if len(matches) != 1:
        selection["error"] = f"GPU selection {selector!r} does not uniquely match the nvidia-smi inventory."
        return None, selection
    gpu = dict(matches[0])
    selection.update({"physical_index": gpu["index"], "uuid": gpu["uuid"],
                      "recommended_cuda_visible_devices": gpu["uuid"]})
    tokens = [token.strip() for token in visible.split(",")] if visible is not None else None
    if tokens == [gpu["uuid"]]:
        selection["cuda_mapping_verified"] = True
        selection["selected_cuda_ordinal"] = 0
    else:
        if tokens is not None and gpu["uuid"] in tokens:
            selection["selected_cuda_ordinal"] = tokens.index(gpu["uuid"])
        if tokens and all(token.isdigit() for token in tokens) and str(gpu["index"]) not in tokens:
            selection["warnings"].append("Numeric CUDA_VISIBLE_DEVICES does not include the selected nvidia-smi physical index; numeric CUDA and nvidia-smi ordering can differ.")
        elif tokens is not None and gpu["uuid"] not in tokens and not all(token.isdigit() for token in tokens):
            selection["warnings"].append("CUDA_VISIBLE_DEVICES does not expose the selected GPU by UUID.")
        selection["warnings"].append(f"Launch with CUDA_VISIBLE_DEVICES={gpu['uuid']} to bind the server to this exact GPU; no environment variable was changed.")
    return gpu, selection


def collect(gpu_selector: str | None = None) -> dict:
    affinity = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 1)
    limits = container_limits(affinity, read_text("/proc/meminfo"), read_text("/proc/self/cgroup"),
                              read_text("/proc/self/mountinfo"), reader=read_text)
    cpus, available = limits["effective_cpus"], limits["available_bytes"]
    result = {"captured_at_utc": datetime.now(timezone.utc).isoformat(),
              "effective_cpus": cpus, "available_ram_gib": round(available / GIB, 2) if available is not None else None,
              "host_available_ram_gib": round(limits["host_available_bytes"] / GIB, 2) if limits["host_available_bytes"] is not None else None,
              "cgroup_limits": limits["cgroup_limits"], "warnings": limits["warnings"],
              "recommended_build_jobs": build_jobs(cpus, available), "gpus": [],
              "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
              "note": "Read-only snapshot. Memory/CPU availability can change; no resource reservation is made."}
    try:
        probe = subprocess.run(["nvidia-smi", "--query-gpu=index,uuid,name,memory.total,memory.free",
                                "--format=csv,noheader,nounits"], capture_output=True, text=True,
                               timeout=15, check=True)
        for row in csv.reader(io.StringIO(probe.stdout), skipinitialspace=True):
            if not row:
                continue
            index, uuid, name, total, free = [part.strip() for part in row]
            result["gpus"].append({"index": int(index), "uuid": uuid, "name": name,
                                   "total_mib": int(total), "free_mib": int(free),
                                   **gpu_plan(int(total), int(free))})
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        result["gpu_probe_error"] = str(error)
    result["selected_gpu"], result["gpu_selection"] = select_gpu(
        result["gpus"], gpu_selector, result["cuda_visible_devices"])
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", help="also save the snapshot as JSON")
    parser.add_argument("--gpu-index", "--gpu-uuid", dest="gpu_selector",
                        help="explicit nvidia-smi physical index or full GPU UUID; never selects automatically")
    args = parser.parse_args(argv)
    report = collect(args.gpu_selector)
    text = json.dumps(report, indent=2)
    print(text)
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
    return 2 if "error" in report["gpu_selection"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
