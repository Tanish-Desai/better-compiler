#!/usr/bin/env python3
"""Resolve Blocker 1 for one bug: build `opt`, confirm it reproduces, repair it.

docs/IMPLEMENTATION.md, Blocker 1: `/workspace/llvm-build` is empty. Nobody has
ever built LLVM for a specific `base_commit` and pushed one bug through the
full loop (build -> Alive2 check -> LLM patch -> rebuild -> verify). Every
other piece of this project has been tested in isolation; none of it has
produced a single repair-rate number yet.

This script is the smallest thing that resolves that: pick one already-chosen
bug (see `scripts/select_bootstrap_bug.py` for how `115575` was picked - a
single-function, single-file VectorCombine miscompilation with a 3-line
reproducer and no other bugs mixed in), build LLVM for its `base_commit`,
confirm the bug actually reproduces there, and optionally run the real repair
loop against it.

It deliberately does NOT introduce a new code path: phase 2 calls
`examples/repair_experiment.py`'s own `repair()` function. This script only
adds the missing preflight (clear errors instead of a wall of stack trace when
an env var or API key is missing) and splits "does the build/harness work at
all" from "spend LLM budget on a repair attempt", because the first one is
what has actually never been exercised.

WHAT THIS NEEDS
---------------
This is infrastructure, not a unit test - it needs the real container:
  * running inside `better-compiler` (docker compose up -d && docker compose
    exec better-compiler bash), so LAB_LLVM_DIR / LAB_LLVM_BUILD_DIR /
    LAB_LLVM_ALIVE_TV / LAB_DATASET_DIR are set and llvm-project is cloned
  * hours of build time on first run (subsequent ones reuse ccache)
  * LAB_LLM_TOKEN, but only for `--full` (phase 1 needs no API key at all)

USAGE
-----
    # Phase 1 only: build opt, confirm the bug reproduces. No API key needed.
    # This is the actual Blocker 1 - run this first.
    python3 scripts/bootstrap_first_repair.py

    # Phase 2: also run the real repair loop for one condition.
    export LAB_LLM_TOKEN=...
    python3 scripts/bootstrap_first_repair.py --full --condition iraware-structured
"""

from __future__ import annotations

import argparse
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.join(_HERE, os.pardir)
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "examples"))

#: Picked by scripts/select_bootstrap_bug.py: simplest qualifying candidate
#: (miscompilation, single-function fix, checked by Alive2 not just `lli`).
#: A 3-instruction VectorCombine bug, one lit dir, no other bugs bundled in.
DEFAULT_BUG_ID = "115575"

REQUIRED_ENV = [
    "LAB_LLVM_DIR",
    "LAB_LLVM_BUILD_DIR",
    "LAB_LLVM_ALIVE_TV",
    "LAB_DATASET_DIR",
]


def _check_env(need_llm_token: bool) -> None:
    missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
    if need_llm_token and not os.environ.get("LAB_LLM_TOKEN"):
        missing.append("LAB_LLM_TOKEN")
    if missing:
        raise SystemExit(
            "Missing environment variable(s): " + ", ".join(missing) + "\n"
            "This script must run inside the better-compiler container "
            "(`docker compose up -d`, then `docker compose exec "
            "better-compiler bash`) — see README.md 'Getting started'."
        )


def phase1_build_and_verify(bug_id: str, build_jobs: int):
    """Build `opt` for `bug_id`'s base_commit; confirm the bug reproduces.

    This is the actual Blocker 1: the first time `env.build()` succeeds for
    any bug, `/workspace/llvm-build` stops being empty. Everything downstream
    (the repair loop, the six conditions) has been ready and untested until
    this runs.
    """
    import repair_experiment as rx  # noqa: PLC0415 (needs env vars set first)

    rx._import_benchmark()  # puts llvm-apr-benchmark/scripts on sys.path
    Env = rx.Env

    env = Env(bug_id, base_model_knowledge_cutoff="2099-01-01Z",
              max_build_jobs=build_jobs)
    print(f"[{bug_id}] base_commit={env.get_base_commit()} "
          f"bug_type={env.get_bug_type()}")

    if not env.is_single_func_fix():
        raise SystemExit(
            f"[{bug_id}] is not a single-function fix; "
            "pick a different bug from scripts/select_bootstrap_bug.py"
        )

    print(f"[{bug_id}] resetting llvm-project to base_commit "
          "(this is the FIRST checkout — expect a slow initial fetch of "
          "blobs the partial clone deferred)...")
    env.reset()

    print(f"[{bug_id}] building opt — this is the actual Blocker 1. "
          "First build is the slow one; ccache makes later ones cheap.")
    started = time.time()
    ok, log = env.check_fast()
    elapsed = time.time() - started
    print(f"[{bug_id}] check_fast finished in {elapsed:.0f}s "
          f"(builds so far: {env.build_count}, "
          f"build failures: {env.build_failure_count})")

    if env.build_failure_count > 0 and env.build_count == env.build_failure_count:
        print("----- build log (tail) -----")
        print(log[-4000:] if isinstance(log, str) else log)
        raise SystemExit(f"[{bug_id}] the LLVM build itself failed — see log above")

    if ok:
        # check_fast() passing means the bug's own test did NOT fail at
        # base_commit — i.e. this dataset entry doesn't reproduce as expected.
        raise SystemExit(
            f"[{bug_id}] built fine, but the bug did not reproduce at "
            "base_commit (check_fast passed). This dataset entry is not a "
            "good bootstrap candidate — pick another from "
            "scripts/select_bootstrap_bug.py."
        )

    print(f"[{bug_id}] opt builds, and the bug reproduces as expected at "
          "base_commit. Blocker 1 is resolved for this bug: the build and "
          "verification machinery works end-to-end.")
    return env


def phase2_repair(bug_id: str, condition: str, out_dir: str, max_iterations: int,
                   build_jobs: int) -> None:
    """Run the real repair loop via examples/repair_experiment.py, unmodified."""
    import repair_experiment as rx  # noqa: PLC0415 (needs env vars set first)

    rx._import_benchmark()
    args = argparse.Namespace(
        max_iterations=max_iterations,
        oracle_budget=400,
        strictness="error_class",
        no_promotion=False,
        build_jobs=build_jobs,
    )
    model = rx.Model()
    print(f"[{bug_id}/{condition}] running the real repair loop "
          f"(model={model.name}, max_iterations={max_iterations})...")
    run = rx.repair(bug_id, condition, args, model)
    if run is None:
        raise SystemExit(f"[{bug_id}] repair() skipped this bug — see message above")
    path = run.write(out_dir)
    totals = run.totals()
    print(f"[{bug_id}/{condition}] -> {path}")
    print(f"[{bug_id}/{condition}] fixed={totals['fixed']} "
          f"iterations={totals['iterations']} "
          f"wall_seconds={totals['wall_seconds']}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bug-id", default=DEFAULT_BUG_ID)
    parser.add_argument("--full", action="store_true",
                        help="also run the real repair loop (needs LAB_LLM_TOKEN); "
                             "omit for build+verify only")
    parser.add_argument("--condition", default="iraware-structured",
                        help="only used with --full")
    parser.add_argument("--max-iterations", type=int, default=4)
    parser.add_argument("--build-jobs", type=int, default=os.cpu_count())
    parser.add_argument("--out", default="results")
    args = parser.parse_args(argv)

    _check_env(need_llm_token=args.full)
    phase1_build_and_verify(args.bug_id, args.build_jobs)

    if args.full:
        phase2_repair(args.bug_id, args.condition, args.out,
                      args.max_iterations, args.build_jobs)
    else:
        print("\nSmoke phase only (default). To spend LLM budget on an actual "
              "repair attempt against this same build, rerun with --full and "
              "LAB_LLM_TOKEN set.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
