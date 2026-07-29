# better-compiler: containerized environment for llvm-apr-benchmark + Alive2
#
# Scope of this image:
#   - System toolchain (gcc-13, cmake, ninja, z3, re2c) baked in ONCE per `docker build`
#   - llvm-apr-benchmark repo (copied in from this repo checkout) + its Python deps
#   - llvm-project partial clone (metadata only — building `opt` for a specific
#     base_commit is done manually per-bug, via `docker exec`, since each of the
#     295 bugs checks out a different commit)
#   - Alive2, pinned to a known-good commit, built from scratch on every image build
#
# NOT baked in: any specific bug's `opt` build. That happens inside a running
# container and is expected to be redone per bug / per machine (see README).

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# ---------------------------------------------------------------------------
# 1. System packages
# ---------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    ninja-build \
    build-essential \
    cmake \
    gcc-13 \
    g++-13 \
    z3 \
    libz3-dev \
    re2c \
    git \
    python3 \
    python3-pip \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# 2. Copy in this repo (expects llvm-apr-benchmark/ at repo root)
#
#    llvm-apr-benchmark/ is a PLAIN FOLDER absorbed into this repo's own
#    history (its nested .git was removed after cloning) — NOT a git
#    submodule. A plain `COPY` is therefore sufficient; no
#    `git submodule update --init` step is needed. If this ever changes
#    back to a submodule, this step must be updated accordingly.
# ---------------------------------------------------------------------------
WORKDIR /workspace
COPY . /workspace

# ---------------------------------------------------------------------------
# 3. llvm-apr-benchmark Python deps + required directories
# ---------------------------------------------------------------------------
RUN pip3 install --no-cache-dir -r /workspace/llvm-apr-benchmark/requirements.txt
RUN mkdir -p /workspace/llvm-apr-benchmark/examples/fixes

# ---------------------------------------------------------------------------
# 4. Partial clone of llvm-project
#    --filter=blob:none pulls history/tree metadata only; blobs for whichever
#    base_commit you check out later are fetched on-demand at that point.
#    No `opt` build happens here on purpose — that's per-bug, done manually.
# ---------------------------------------------------------------------------
RUN mkdir -p /workspace/llvm-apr-benchmark/work && \
    cd /workspace/llvm-apr-benchmark/work && \
    git clone --filter=blob:none https://github.com/llvm/llvm-project.git

# ---------------------------------------------------------------------------
# 5. Alive2 — pinned commit, built from scratch every image build (no ccache)
# ---------------------------------------------------------------------------
RUN git clone https://github.com/AliveToolkit/alive2.git /workspace/alive2 && \
    cd /workspace/alive2 && \
    git checkout f9a4f02f && \
    cmake -B build -GNinja -DCMAKE_BUILD_TYPE=Release -DBUILD_TV=1 && \
    cmake --build build -j"$(nproc)"

# ---------------------------------------------------------------------------
# 6. Environment variables required by lab_env.Environment
# ---------------------------------------------------------------------------
ENV LAB_LLVM_DIR=/workspace/llvm-apr-benchmark/work/llvm-project
ENV LAB_LLVM_BUILD_DIR=/workspace/llvm-apr-benchmark/work/llvm-project/build
ENV LAB_LLVM_ALIVE_TV=/workspace/alive2/build/alive-tv
ENV LAB_DATASET_DIR=/workspace/llvm-apr-benchmark/dataset
ENV LAB_FIX_DIR=/workspace/llvm-apr-benchmark/examples/fixes

WORKDIR /workspace
CMD ["/bin/bash"]
