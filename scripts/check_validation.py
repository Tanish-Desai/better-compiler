#!/usr/bin/env python3
"""Check a known human repair against the exact build/reproducer/lit harness.

This is an infrastructure diagnostic, NOT an LLM repair result. Stop every
repair runner first: both commands use and reset the same LLVM checkout.

    python3 scripts/check_validation.py --bug-id 115149 --build-jobs 64
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "examples")]


def check_known_fix(env, helper) -> dict:
    report = {"diagnostic": "known_human_patch", "passed": False}
    try:
        env.reset()
        passed, log = env.check_fast()
        report["baseline"] = {"passed": passed, "validation": dict(env.last_validation), "log": log}
        if env.build_failure_count:
            report["error"] = "Unpatched LLVM failed to build."
            return report
        if passed:
            report["error"] = "The bug did not reproduce at base_commit."
            return report
        # A crash, timeout or tool error is not evidence of the semantic bug.
        from ce.benchmark import first_failed_test, is_alive2_log
        from ce.alive import parse_alive_output
        failed = first_failed_test(log)
        inner = failed.get("log") if failed else log
        if not is_alive2_log(inner) or parse_alive_output(inner.get("log", "")).num_incorrect < 1:
            report["error"] = "Baseline did not produce an Alive2 incorrect-transformation report."
            return report
        applied, apply_log = helper.apply(env.data["patch"])
        if not applied:
            report["error"] = "The dataset human patch did not apply."
            report["apply_log"] = apply_log
            return report
        passed, log = env.check_full()
        report["human_patch"] = {"passed": passed, "validation": dict(env.last_validation), "log": log}
        report["passed"] = passed
        return report
    except Exception as e:
        report["error"] = f"{type(e).__name__}: {e}"
        return report
    finally:
        env.reset()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bug-id", default="115149")
    parser.add_argument("--build-jobs", type=int, default=os.cpu_count())
    parser.add_argument("--out", default="validation-checks", help="diagnostic logs, separate from repair records")
    args = parser.parse_args(argv)
    if args.build_jobs < 1:
        parser.error("--build-jobs must be positive")
    required = ("LAB_LLVM_DIR", "LAB_LLVM_BUILD_DIR", "LAB_LLVM_ALIVE_TV", "LAB_DATASET_DIR")
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        parser.error("Missing environment: " + ", ".join(missing))
    import repair_experiment as rx
    rx._import_benchmark()
    env = rx.Env(args.bug_id, "2000-01-01Z", max_build_jobs=args.build_jobs)
    print(f"[{args.bug_id}] checking the baseline and known human patch (no LLM)", flush=True)
    report = check_known_fix(env, rx.llvm_helper)
    report["bug_id"] = args.bug_id
    root = Path(args.out)
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{args.bug_id}.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2), encoding="utf-8")
    temporary.replace(target)
    print(f"{'PASS' if report['passed'] else 'FAIL'}: {target}")
    if not report["passed"]:
        print(report.get("error") or json.dumps(report.get("human_patch"), indent=2)[-5000:])
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
