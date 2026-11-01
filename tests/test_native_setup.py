"""Exercise native setup decisions without apt, downloads or system changes."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "setup_native.sh"
GIT_BASH = Path("C:/Program Files/Git/bin/bash.exe")
BASH = str(GIT_BASH) if os.name == "nt" and GIT_BASH.is_file() else shutil.which("bash")
pytestmark = pytest.mark.skipif(not BASH, reason="native setup tests require Bash")


def shell(body, *arguments):
    return subprocess.run(
        [BASH, "-c", 'source "$1"; shift; ' + body, "setup-test", SCRIPT.as_posix(),
         *[str(item) for item in arguments]], text=True, capture_output=True, timeout=15)


def test_script_syntax_and_sourcing_have_no_install_side_effects():
    checked = subprocess.run([BASH, "-n", SCRIPT.as_posix()], capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr
    sourced = shell("printf 'loaded\\n'")
    assert sourced.returncode == 0, sourced.stderr
    assert sourced.stdout == "loaded\n"


@pytest.mark.parametrize("version,codename", [("22.04", "jammy"), ("24.04", "noble")])
def test_ubuntu_release_detection(tmp_path, version, codename):
    release = tmp_path / "os-release"
    release.write_text(f'ID=ubuntu\nVERSION_ID="{version}"\nVERSION_CODENAME={codename}\n')
    result = shell('detect_platform "$1"; printf "%s\\n" "$CODENAME"', release.as_posix())
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == codename


@pytest.mark.parametrize("distro,version,codename", [
    ("debian", "12", "bookworm"), ("ubuntu", "26.04", "resolute"),
    ("ubuntu", "24.04", "jammy"),
])
def test_other_releases_fail_instead_of_mixing_apt_sources(tmp_path, distro, version, codename):
    release = tmp_path / "os-release"
    release.write_text(f'ID={distro}\nVERSION_ID="{version}"\nVERSION_CODENAME={codename}\n')
    result = shell('detect_platform "$1"', release.as_posix())
    assert result.returncode != 0
    assert "do not mix" in result.stderr


@pytest.mark.parametrize("requested,expected", [(None, "4"), ("2", "2"), ("64", "8")])
def test_setup_jobs_are_bounded_and_explicit(requested, expected):
    assignment = "unset LAB_SETUP_JOBS;" if requested is None else f"LAB_SETUP_JOBS={requested};"
    result = shell('nproc() { printf "8\\n"; }; ' + assignment +
                   ' select_setup_jobs; printf "%s\\n" "$SETUP_JOBS"')
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


@pytest.mark.parametrize("invalid", ["0", "-1", "many", "9999999999999999999999"])
def test_invalid_setup_job_limit_fails_early(invalid):
    result = shell('LAB_SETUP_JOBS="$1"; select_setup_jobs', invalid)
    assert result.returncode != 0
    assert "positive integer" in result.stderr


def test_check_mode_never_calls_an_installer_or_creates_install_root(tmp_path):
    destination = tmp_path / "not-created"
    result = shell(
        'detect_platform() { CODENAME=noble; }; '
        'apt-get() { echo "unexpected package mutation" >&2; return 99; }; '
        'git() { echo "unexpected clone" >&2; return 99; }; '
        'cmake() { echo "unexpected build" >&2; return 99; }; '
        'main --check "$1"', destination.as_posix())
    assert result.returncode == 0, result.stderr
    assert "Check only: no packages" in result.stdout
    assert "unexpected" not in result.stderr
    assert not destination.exists()


def test_generated_environment_round_trips_shell_metacharacters_and_tool_paths(tmp_path):
    runtime = tmp_path / "runtime $cash ; with spaces"
    repository = tmp_path / "repo with spaces"
    activate = runtime / "venv" / "bin" / "activate"
    activate.parent.mkdir(parents=True)
    activate.write_text("export TEST_ACTIVATED=yes\n")
    result = shell(
        'ROOT="$1"; REPO_ROOT="$2"; LAB_LLM_MODEL=pilot-model; LAB_LLM_TOKEN=test-token; '
        'unset LAB_LLVM_PARALLEL_LINK_JOBS; '
        'write_environment "$ROOT/env.sh"; source "$ROOT/env.sh"; '
        'printf "%s\\n" "$TEST_ACTIVATED" "$LAB_LLVM_DIR" "$LAB_DATASET_DIR" '
        '"$LAB_LLVM_LLVM_REDUCE" "$LAB_LLM_MODEL" "$CC" "$CXX" "$LAB_LLVM_PARALLEL_LINK_JOBS"',
        runtime.as_posix(), repository.as_posix())
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "yes", f"{runtime.as_posix()}/work/llvm-project",
        f"{repository.as_posix()}/llvm-apr-benchmark/dataset", "/usr/bin/llvm-reduce-21",
        "pilot-model", "/usr/bin/gcc-13", "/usr/bin/g++-13", "1"]


def test_setup_rerun_preserves_existing_endpoint_and_provenance(tmp_path):
    environment = tmp_path / "env.sh"
    original = "export LAB_LLM_MODEL_REVISION=recorded-revision\nexport LAB_LLM_URL=https://private.example/v1\n"
    environment.write_text(original)
    result = shell('write_environment "$1"', environment.as_posix())
    assert result.returncode == 0, result.stderr
    assert "preserving existing" in result.stdout
    assert environment.read_text() == original
