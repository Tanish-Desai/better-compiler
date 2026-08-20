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
#
#    gcc-13/g++-13 are NOT in Ubuntu 22.04's default repos (jammy ships
#    gcc-12) — they require the ubuntu-toolchain-r/test PPA.
#
#    NOTE: we add the PPA manually (curl + keyring file) rather than via
#    `add-apt-repository`, because that tool shells out to gpg-agent, which
#    doesn't exist yet in a bare Ubuntu image and fails with
#    "gpg: failed to start agent". Curl-ing the key directly avoids gpg
#    entirely.
# ---------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg \
    && install -d /etc/apt/keyrings \
    && curl -fsSL https://keyserver.ubuntu.com/pks/lookup?op=get\&search=0xC8EC952E2A0E1FBDC5090F6A2C277A0A352154E5 \
       -o /etc/apt/keyrings/ubuntu-toolchain-r.asc \
    && echo "deb [signed-by=/etc/apt/keyrings/ubuntu-toolchain-r.asc] https://ppa.launchpadcontent.net/ubuntu-toolchain-r/test/ubuntu jammy main" \
       > /etc/apt/sources.list.d/ubuntu-toolchain-r.list \
    && apt-get update && apt-get install -y --no-install-recommends \
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
    ccache \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# 2. Partial clone of llvm-project
#    --filter=blob:none pulls history/tree metadata only; blobs for whichever
#    base_commit you check out later are fetched on-demand at that point.
#    No `opt` build happens here on purpose — that's per-bug, done manually.
#
#    This clone is large/long-running enough that flaky connections can
#    drop it mid-transfer (GnuTLS recv errors, "unexpected disconnect").
#    The git config tuning + retry loop below make it resilient to that.
#
#    NOTE: deliberately placed BEFORE `COPY . /workspace` (see step 5) —
#    this step has zero dependency on repo contents, so it must not sit
#    downstream of a layer that invalidates on every repo edit.
# ---------------------------------------------------------------------------
RUN git config --global http.postBuffer 1048576000 && \
    git config --global http.lowSpeedLimit 0 && \
    git config --global http.lowSpeedTime 999999 && \
    mkdir -p /workspace/llvm-apr-benchmark/work && \
    cd /workspace/llvm-apr-benchmark/work && \
    for i in 1 2 3 4 5; do \
        git clone --progress --filter=blob:none https://github.com/llvm/llvm-project.git && break; \
        echo "Clone attempt $i failed, retrying..."; \
        rm -rf llvm-project; \
        sleep 5; \
    done && \
    test -d llvm-project/.git

# ---------------------------------------------------------------------------
# 3. Alive2 — pinned commit, built from scratch every image build (no ccache)
#
#    Alive2's CMake needs a system-installed LLVM dev package (providing
#    LLVMConfig.cmake) via find_package(LLVM) — separate from the
#    llvm-project source clone in step 2, which is source-only and has no
#    installed CMake config.
#
#    NOTE: LLVM 18 was tried first and FAILED — commit f9a4f02f uses APIs
#    that don't exist until later LLVM versions (llvm::Attribute::Range,
#    Intrinsic::getOrInsertDeclaration, a newer ICmpInst constructor, and
#    a ThinOrFullLTOPhase-aware registerOptimizerLastEPCallback signature).
#    LLVM 21 is used instead, matching the version confirmed to work when
#    this was built locally (per the setup guide: system LLVM 21.1.8,
#    auto-detected, no CMAKE_PREFIX_PATH override needed).
# ---------------------------------------------------------------------------
RUN curl -fsSL https://apt.llvm.org/llvm-snapshot.gpg.key -o /etc/apt/keyrings/llvm.asc && \
    echo "deb [signed-by=/etc/apt/keyrings/llvm.asc] http://apt.llvm.org/jammy/ llvm-toolchain-jammy-21 main" \
       > /etc/apt/sources.list.d/llvm.list && \
    apt-get update && apt-get install -y --no-install-recommends \
    llvm-21-dev \
    liblld-21-dev \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

RUN git clone https://github.com/AliveToolkit/alive2.git /workspace/alive2 && \
    cd /workspace/alive2 && \
    git checkout f9a4f02f && \
    cmake -B build -GNinja -DCMAKE_BUILD_TYPE=Release -DBUILD_TV=1 \
      -DLLVM_DIR=/usr/lib/llvm-21/lib/cmake/llvm && \
    cmake --build build -j"$(nproc)"

# ---------------------------------------------------------------------------
# 4. Copy in this repo (expects llvm-apr-benchmark/ at repo root)
#
#    llvm-apr-benchmark/ is a PLAIN FOLDER absorbed into this repo's own
#    history (its nested .git was removed after cloning) — NOT a git
#    submodule. A plain `COPY` is therefore sufficient; no
#    `git submodule update --init` step is needed. If this ever changes
#    back to a submodule, this step must be updated accordingly.
#
#    Placed AFTER steps 2-3 on purpose: this layer invalidates on every
#    repo file change, so anything downstream of it re-runs every build.
#    Keeping it late means only the two small steps below (pip install,
#    mkdir) pay that cost — not the multi-minute clone/build steps.
# ---------------------------------------------------------------------------
WORKDIR /workspace
COPY . /workspace

# ---------------------------------------------------------------------------
# 5. llvm-apr-benchmark Python deps + required directories
# ---------------------------------------------------------------------------
RUN pip3 install --no-cache-dir -r /workspace/llvm-apr-benchmark/requirements.txt
# pytest is for this repo's own `ce` package test suite, not the benchmark's.
RUN pip3 install --no-cache-dir pytest
RUN mkdir -p /workspace/llvm-apr-benchmark/examples/fixes /workspace/results

# ---------------------------------------------------------------------------
# 6. Environment variables required by lab_env.Environment
#    (numbering here is intentionally sequential with steps 1-5 above)
# ---------------------------------------------------------------------------
ENV LAB_LLVM_DIR=/workspace/llvm-apr-benchmark/work/llvm-project
ENV LAB_LLVM_BUILD_DIR=/workspace/llvm-build
ENV LAB_LLVM_ALIVE_TV=/workspace/alive2/build/alive-tv
ENV LAB_DATASET_DIR=/workspace/llvm-apr-benchmark/dataset
ENV LAB_FIX_DIR=/workspace/llvm-apr-benchmark/examples/fixes

WORKDIR /workspace
CMD ["/bin/bash"]
