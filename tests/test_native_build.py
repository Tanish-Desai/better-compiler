"""Check native build concurrency without invoking CMake or LLVM."""

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def helper(monkeypatch, tmp_path):
    for key, value in {"LAB_LLVM_DIR": tmp_path / "source",
                       "LAB_LLVM_BUILD_DIR": tmp_path / "build",
                       "LAB_LLVM_ALIVE_TV": tmp_path / "alive-tv",
                       "LAB_DATASET_DIR": tmp_path / "dataset"}.items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace())
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: SimpleNamespace(stderr=b"--quiet"))
    spec = importlib.util.spec_from_file_location(
        "native_build_llvm_helper", ROOT / "llvm-apr-benchmark/scripts/llvm_helper.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("configured,expected", [(None, 4), ("1", 1), ("2", 2)])
def test_link_job_override_preserves_validation_configuration(monkeypatch, helper, configured, expected):
    if configured is None:
        monkeypatch.delenv("LAB_LLVM_PARALLEL_LINK_JOBS", raising=False)
    else:
        monkeypatch.setenv("LAB_LLVM_PARALLEL_LINK_JOBS", configured)
    calls = []

    def checked(command, **kwargs):
        calls.append(command)
        return b"ok\n"

    monkeypatch.setattr(helper.subprocess, "check_output", checked)
    assert helper.build(8)[0]
    assert len(calls) == 2
    cmake, build = calls
    assert f"-DLLVM_PARALLEL_LINK_JOBS={expected}" in cmake
    assert "-DCMAKE_BUILD_TYPE=RelWithDebInfo" in cmake
    assert "-DLLVM_ENABLE_ASSERTIONS=ON" in cmake
    assert "-DLLVM_ABI_BREAKING_CHECKS=WITH_ASSERTS" in cmake
    assert "-DLLVM_TARGETS_TO_BUILD='X86;RISCV;AArch64;SystemZ;Hexagon;PowerPC;NVPTX;'" in cmake
    assert build[:5] == ["cmake", "--build", ".", "-j", "8"]


@pytest.mark.parametrize("configured", ["0", "-1", "not-a-number"])
def test_invalid_link_job_limit_fails_before_build_or_directory_creation(monkeypatch, helper, configured):
    monkeypatch.setenv("LAB_LLVM_PARALLEL_LINK_JOBS", configured)
    monkeypatch.setattr(helper.subprocess, "check_output", lambda *a, **kw: pytest.fail("unexpected CMake"))
    with pytest.raises(ValueError, match="LAB_LLVM_PARALLEL_LINK_JOBS must be a positive integer"):
        helper.build(8)
    assert not Path(helper.llvm_build_dir).exists()
