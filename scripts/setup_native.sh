#!/usr/bin/env bash
# Native (no-Docker) setup for a machine where you can't start a Docker daemon
# but do have root inside your own container (e.g. a shared cluster that hands
# out container permissions, not host permissions).
#
# This does exactly what Dockerfile does -- system toolchain, llvm-project
# partial clone, Alive2 build, Python deps -- as plain shell commands against
# whatever filesystem you already have, instead of building an image. See
# docs/RUNBOOK_NATIVE.md for the full walkthrough this script is step 1 of.
#
# Usage:
#   ./scripts/setup_native.sh [install-root]
#
# install-root defaults to $HOME/better-compiler-runtime. Everything this
# script creates goes under there; nothing outside it is touched except
# system packages via apt.
set -euo pipefail

ROOT="${1:-$HOME/better-compiler-runtime}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "== better-compiler native setup =="
echo "install root: $ROOT"
echo "repo root:    $REPO_ROOT"
echo

# ---------------------------------------------------------------------------
# 0. Diagnostics -- find out what you actually have before assuming anything.
#    A container with "container perms only" can still vary a lot: some allow
#    apt, some don't; some pass the GPU through, some don't. Fail loud and
#    early rather than half-installing.
# ---------------------------------------------------------------------------
echo "-- checking environment --"
if [ "$(id -u)" -ne 0 ]; then
    echo "WARNING: not running as root ($(id -un)). apt-get install below will" \
         "fail unless this container grants passwordless sudo." >&2
fi

if command -v apt-get >/dev/null 2>&1; then
    echo "apt-get: available"
else
    echo "FATAL: no apt-get. This script targets Debian/Ubuntu containers." >&2
    echo "        If yours is a different base image, translate step 1 below" >&2
    echo "        to that distro's package manager and re-run from step 2." >&2
    exit 1
fi

if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
    echo "GPU: visible (nvidia-smi works) -- vLLM can run in this same container"
else
    echo "GPU: NOT visible from inside this container." >&2
    echo "     The LLVM build below does not need it, but vLLM (docs/RUNBOOK_NATIVE.md" >&2
    echo "     step 5) does. If nvidia-smi fails here, find out from whoever manages" >&2
    echo "     this cluster where the GPU-passthrough container is, and run vLLM there" >&2
    echo "     instead -- the LLM server only needs to be reachable over HTTP from here." >&2
fi
echo

mkdir -p "$ROOT"

# ---------------------------------------------------------------------------
# 1. System packages (Dockerfile steps 1 + 3's apt half)
# ---------------------------------------------------------------------------
echo "-- installing system packages --"
apt-get update
apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg
install -d /etc/apt/keyrings
curl -fsSL "https://keyserver.ubuntu.com/pks/lookup?op=get&search=0xC8EC952E2A0E1FBDC5090F6A2C277A0A352154E5" \
    -o /etc/apt/keyrings/ubuntu-toolchain-r.asc
echo "deb [signed-by=/etc/apt/keyrings/ubuntu-toolchain-r.asc] https://ppa.launchpadcontent.net/ubuntu-toolchain-r/test/ubuntu jammy main" \
    > /etc/apt/sources.list.d/ubuntu-toolchain-r.list

curl -fsSL https://apt.llvm.org/llvm-snapshot.gpg.key -o /etc/apt/keyrings/llvm.asc
echo "deb [signed-by=/etc/apt/keyrings/llvm.asc] http://apt.llvm.org/jammy/ llvm-toolchain-jammy-21 main" \
    > /etc/apt/sources.list.d/llvm.list

apt-get update
apt-get install -y --no-install-recommends \
    ninja-build build-essential cmake gcc-13 g++-13 z3 libz3-dev re2c \
    git python3 python3-pip python3-venv ccache \
    llvm-21-dev liblld-21-dev zlib1g-dev

# ---------------------------------------------------------------------------
# 2. llvm-project partial clone (Dockerfile step 2)
#    Same retry-on-flaky-connection loop as the image build.
# ---------------------------------------------------------------------------
echo "-- cloning llvm-project (metadata only; blobs fetched on demand) --"
git config --global http.postBuffer 1048576000
git config --global http.lowSpeedLimit 0
git config --global http.lowSpeedTime 999999

mkdir -p "$ROOT/work"
if [ ! -d "$ROOT/work/llvm-project/.git" ]; then
    cd "$ROOT/work"
    for i in 1 2 3 4 5; do
        git clone --progress --filter=blob:none https://github.com/llvm/llvm-project.git && break
        echo "Clone attempt $i failed, retrying..."
        rm -rf llvm-project
        sleep 5
    done
    test -d llvm-project/.git
else
    echo "already cloned, skipping"
fi

# ---------------------------------------------------------------------------
# 3. Alive2, pinned commit (Dockerfile step 3's build half)
# ---------------------------------------------------------------------------
echo "-- building Alive2 --"
if [ ! -x "$ROOT/alive2/build/alive-tv" ]; then
    if [ ! -d "$ROOT/alive2" ]; then
        git clone https://github.com/AliveToolkit/alive2.git "$ROOT/alive2"
    fi
    cd "$ROOT/alive2"
    git checkout f9a4f02f
    cmake -B build -GNinja -DCMAKE_BUILD_TYPE=Release -DBUILD_TV=1 \
        -DLLVM_DIR=/usr/lib/llvm-21/lib/cmake/llvm
    cmake --build build -j"$(nproc)"
else
    echo "already built, skipping"
fi

# ---------------------------------------------------------------------------
# 4. Python deps (Dockerfile step 5)
#
#    A venv, not system pip3 install: modern Debian/Ubuntu (PEP 668,
#    "externally-managed-environment") refuses a bare `pip3 install` outright.
#    The Dockerfile never hit this because Ubuntu 22.04 shipped Python 3.10,
#    before this policy existed -- a newer base image here means Python 3.12+
#    and the restriction applies. `--break-system-packages` would silence it,
#    but pip's own error says that risks the OS's Python tooling on a
#    container you don't own; a venv sidesteps the question entirely.
# ---------------------------------------------------------------------------
echo "-- installing Python deps (venv) --"
if [ ! -d "$ROOT/venv" ]; then
    python3 -m venv "$ROOT/venv"
fi
"$ROOT/venv/bin/pip" install --no-cache-dir --upgrade pip
"$ROOT/venv/bin/pip" install --no-cache-dir -r "$REPO_ROOT/llvm-apr-benchmark/requirements.txt"
"$ROOT/venv/bin/pip" install --no-cache-dir pytest

mkdir -p "$REPO_ROOT/llvm-apr-benchmark/examples/fixes" "$REPO_ROOT/results"
mkdir -p "$ROOT/llvm-build" "$ROOT/ccache"

# ---------------------------------------------------------------------------
# 5. Environment (Dockerfile step 6, plus ccache pointed into $ROOT so it
#    isn't lost if /root or $HOME gets reset between sessions on a shared box)
#
#    EVERY variable anything in this repo reads goes in here -- the LAB_LLVM_*
#    paths the benchmark needs, the LAB_LLM_* endpoint settings the repair
#    loop needs, and the two vLLM knobs the server needs. One file, sourced
#    once, so no command in the docs ever needs an `env VAR=... ` prefix.
# ---------------------------------------------------------------------------
ENV_FILE="$ROOT/env.sh"
cat > "$ENV_FILE" <<EOF
# Generated by scripts/setup_native.sh -- safe to edit, re-running the script
# regenerates it. Sourced automatically from ~/.bashrc; to load it by hand:
#     source $ENV_FILE

# The venv holding this repo's Python deps. Sourcing this file activates it,
# so plain 'python3' and 'pip' resolve to it from then on.
source $ROOT/venv/bin/activate

# --- Where the benchmark's pieces live (read by llvm-apr-benchmark) ---------
export LAB_LLVM_DIR=$ROOT/work/llvm-project
export LAB_LLVM_BUILD_DIR=$ROOT/llvm-build
export LAB_LLVM_ALIVE_TV=$ROOT/alive2/build/alive-tv
export LAB_DATASET_DIR=$REPO_ROOT/llvm-apr-benchmark/dataset
export LAB_FIX_DIR=$REPO_ROOT/llvm-apr-benchmark/examples/fixes
export CCACHE_DIR=$ROOT/ccache

# --- The model endpoint (read by examples/repair_experiment.py) -------------
# LAB_LLM_URL must point at wherever vLLM is listening. 127.0.0.1 is right
# when vLLM runs in this same container; if the GPU is elsewhere, put that
# machine's address here -- the runner only needs HTTP.
# LAB_LLM_TOKEN and LAB_LLM_MODEL must match the --api-key and
# --served-model-name you pass to 'vllm serve'.
# LAB_LLM_TEMP MUST stay above 0: docs/ANALYSIS_PLAN.md fixes k = 3, and three
# trials at temperature 0 are three identical answers.
export LAB_LLM_URL=http://127.0.0.1:8000/v1
export LAB_LLM_TOKEN=local-sweep
export LAB_LLM_MODEL=qwen2.5-coder-14b
export LAB_LLM_TEMP=0.8

# --- vLLM server settings (read by 'vllm serve') ----------------------------
# This container has CUDA runtime but no nvcc, so vLLM's default sampler
# cannot JIT its kernel. Forces the built-in PyTorch sampler instead.
# See docs/IMPLEMENTATION.md Blocker 13.
export VLLM_USE_FLASHINFER_SAMPLER=0
EOF

# ---------------------------------------------------------------------------
# 6. Load it automatically in every new shell.
#
#    Idempotent: the marker line is grepped for first, so re-running this
#    script does not stack up duplicate sourcing lines in ~/.bashrc.
# ---------------------------------------------------------------------------
BASHRC="$HOME/.bashrc"
MARKER="# >>> better-compiler environment >>>"
if [ -f "$BASHRC" ] && grep -qF "$MARKER" "$BASHRC"; then
    echo "-- ~/.bashrc already loads $ENV_FILE, leaving it alone --"
else
    echo "-- adding $ENV_FILE to ~/.bashrc --"
    cat >> "$BASHRC" <<EOF

$MARKER
# Added by scripts/setup_native.sh. Delete this block to opt out.
if [ -f "$ENV_FILE" ]; then
    . "$ENV_FILE"
fi
# <<< better-compiler environment <<<
EOF
fi

echo
echo "== done =="
echo "Environment written to $ENV_FILE and loaded from ~/.bashrc, so every"
echo "new shell already has it -- no 'export' lines needed before any command."
echo
echo "For THIS shell (which started before that was true), load it once:"
echo "    source $ENV_FILE"
echo
echo "Then continue with docs/RUNBOOK_NATIVE.md from 'Give ccache room'."
