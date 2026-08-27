#!/usr/bin/env python3
"""THE ACTUAL EXPERIMENT: try to fix real LLVM bugs with an AI, six ways.

WHAT THIS SCRIPT DOES
---------------------
For one bug, it runs the standard repair loop::

    1. show the AI the buggy C++ code and a description of the failure
    2. the AI replies with a rewritten version
    3. build LLVM with that patch
    4. run the bug's test AND the full regression test suite
    5. fixed?  stop.  not fixed?  feed the new failure back and go to 2

...up to ``--max-iterations`` times, and records everything that happened.

THE ONE THING THAT VARIES
-------------------------
This file is ``llvm-apr-benchmark/examples/baseline.py`` with **exactly one
line changed**: the line that turns a failure into text for the next prompt.

Everything else is deliberately identical across conditions -- same model, same
temperature, same prompt wording, same code window, same number of attempts.
That is not laziness, it is the point.  If two conditions differed in any other
way, we could not claim that a difference in results came from the feedback.

WHAT COUNTS AS "FIXED"
----------------------
Not "it compiled".  Not even "the bug's own test passes".  A fix counts only if
``env.check_full()`` passes, which means the patch builds AND fixes the bug AND
does not break any of LLVM's existing regression tests.  That standard is
enforced by the benchmark, not by us.

WHAT YOU NEED BEFORE THIS WILL RUN
----------------------------------
  * ``opt`` built for the bug's ``base_commit`` (this is the slow part -- see
    the "Hindrances" section of ``docs/IMPLEMENTATION.md``)
  * ``LAB_LLM_TOKEN`` set to an API key
  * the ``LAB_LLVM_*`` variables, which the container already sets

Usage::

    export LAB_LLM_TOKEN=...            # plus the LAB_LLVM_* variables
    python3 examples/repair_experiment.py --condition iraware-structured 121459
    python3 examples/repair_experiment.py --condition A --all --out results/

Every run writes ``<bug_id>.<condition>.json`` into ``--out``; aggregate them
with ``python3 examples/summarize_results.py results/``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from typing import List, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, os.pardir))

from ce.benchmark import Iteration, RunLog, normalize_feedback  # noqa: E402
from ce.feedback import CONDITIONS, resolve_condition  # noqa: E402

# The benchmark's helpers read LAB_* at import time, so import them only after
# argument parsing has had a chance to fail fast on a missing environment.
llvm_helper = None
Env = None


def _import_benchmark() -> None:
    global llvm_helper, Env
    dataset_dir = os.environ.get("LAB_DATASET_DIR")
    if not dataset_dir:
        raise SystemExit("LAB_DATASET_DIR is not set; see the container README")
    sys.path.append(os.path.join(os.path.dirname(dataset_dir), "scripts"))
    import llvm_helper as _llvm_helper  # noqa: F401
    from lab_env import Environment as _Env  # noqa: F401

    llvm_helper, Env = _llvm_helper, _Env


FORMAT_REQUIREMENT = """
Please answer with the code directly. Do not include any additional information in the output.
Please answer with the complete code snippet (including the unmodified part) that replaces the original code. Do not answer with a diff.
"""

SYSTEM_PROMPT = (
    "You are an LLVM maintainer.\n"
    "You are fixing a middle-end bug in the LLVM project." + FORMAT_REQUIREMENT
)


# --------------------------------------------------------------------------
# LLM client
# --------------------------------------------------------------------------

class Model:
    """Thin OpenAI-compatible wrapper that also reports token usage.

    Usage is part of the result: RQ4 asks whether better feedback lowers cost,
    which cannot be answered from a repair rate alone.
    """

    def __init__(self) -> None:
        from openai import OpenAI

        self.name = os.environ.get("LAB_LLM_MODEL", "deepseek-reasoner")
        # SELF-DECLARED, not verified (docs/IMPLEMENTATION.md Blocker 4):
        # lab_env.Environment.use_knowledge() only compares this string
        # against each bug's knowledge_cutoff -- it has no way to check
        # whether the model's REAL training data actually respects it. This
        # default (2023-12-31) is almost certainly earlier than
        # deepseek-reasoner's actual training cutoff, so treat "benchmark
        # legal" as unverifiable from inside this harness regardless of what
        # this variable is set to. The paper's claims must not depend on
        # legality; see Blocker 4's resolution in context.md/METHODOLOGY.md.
        self.cutoff = os.environ.get("LAB_LLM_BASEMODEL_CUTOFF", "2023-12-31Z")
        self.temperature = float(os.environ.get("LAB_LLM_TEMP", "0.8"))
        self.client = OpenAI(
            api_key=os.environ["LAB_LLM_TOKEN"],
            base_url=os.environ.get("LAB_LLM_URL", "https://api.deepseek.com"),
        )

    def chat(self, messages: List[dict]) -> Tuple[str, dict]:
        response = self.client.chat.completions.create(
            model=self.name,
            messages=messages,
            timeout=300,
            temperature=self.temperature,
        )
        usage = getattr(response, "usage", None)
        stats = {
            "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
        }
        return (response.choices[0].message.content or ""), stats


# --------------------------------------------------------------------------
# Patch handling (kept identical to the benchmark's baseline)
# --------------------------------------------------------------------------

def extract_code(reply: str) -> str:
    if reply.startswith("```"):
        return reply.strip().removeprefix("```cpp").removeprefix("```").removesuffix("```")
    for pattern in (r"```cpp([\s\S]+)```", r"```([\s\S]+)```"):
        matches = re.findall(pattern, reply)
        if matches:
            return matches[-1]
    return reply


def apply_patch(file: str, original_hunk: str, reply: str) -> bool:
    """Replace ``original_hunk`` with the model's code. False if it did not match."""
    path = os.path.join(llvm_helper.llvm_dir, file)
    with open(path, encoding="utf-8") as f:
        code = f.read()
    if original_hunk not in code:
        return False
    with open(path, "w", encoding="utf-8") as f:
        f.write(code.replace(original_hunk, extract_code(reply)))
    return True


def bug_hunk(env, margin: int = 30) -> Tuple[str, str]:
    """The source window the model is asked to rewrite."""
    lineno = env.get_hint_line_level_bug_locations()
    bug_file = next(iter(lineno.keys()))
    ranges = next(iter(lineno.values()))
    lo = min(r[0] for r in ranges)
    hi = max(r[1] for r in ranges)
    source = str(
        llvm_helper.git_execute(["show", f"{env.get_base_commit()}:{bug_file}"])
    ).splitlines()
    lo = max(lo - margin, 1)
    hi = min(hi + margin, len(source))
    return bug_file, "\n".join(source[lo - 1:hi])


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------

def repair(bug_id: str, condition: str, args, model: Model) -> Optional[RunLog]:
    cond = resolve_condition(condition)
    env = Env(bug_id, model.cutoff, max_build_jobs=args.build_jobs)

    if not env.is_single_func_fix():
        print(f"[{bug_id}] skipped: multi-function fix is out of scope")
        return None
    if env.get_bug_type() != "miscompilation" and cond.counterexample:
        # Only miscompilations produce Alive2 counterexamples; running crash or
        # hang bugs under these conditions would compare identical prompts and
        # dilute the effect being measured.
        print(f"[{bug_id}] skipped: bug_type is {env.get_bug_type()}, not miscompilation")
        return None

    run = RunLog(
        bug_id=bug_id,
        condition=cond.name,
        model=model.name,
        max_iterations=args.max_iterations,
        notes={
            "bug_type": env.get_bug_type(),
            "components": env.get_hint_components(),
            "oracle_budget": args.oracle_budget,
            "oracle_strictness": args.strictness,
            "allow_promotion": not args.no_promotion,
        },
    )
    env.use_knowledge("alive2", env.knowledge_cutoff)

    env.reset()
    ok, log = env.check_fast()
    if ok:
        print(f"[{bug_id}] skipped: the bug does not reproduce at base_commit")
        return None

    file, hunk = bug_hunk(env)
    prefix = "\n".join(hunk.splitlines()[:5])
    suffix = "\n".join(hunk.splitlines()[-5:])
    context_requirement = (
        f"Please make sure the answer includes the prefix:\n```cpp\n{prefix}\n```\n"
        f"and the suffix:\n```cpp\n{suffix}\n```\n"
    )

    feedback = normalize_feedback(
        log, cond.name,
        bug_type=env.get_bug_type(),
        oracle_budget=args.oracle_budget,
        oracle_strictness=args.strictness,
        allow_promotion=not args.no_promotion,
    )
    issue = env.get_hint_issue() or {}
    component = next(iter(env.get_hint_components() or ["unknown"]))

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": (
            f"This is a {env.get_bug_type()} bug in {component}.\n"
            f"Issue title: {issue.get('title', '<omitted>')}\n"
            "Detailed information:\n"
            f"{feedback.text}\n"
            f"Please modify the following code in {file} to fix the bug:\n"
            f"```cpp\n{hunk}\n```\n"
            + FORMAT_REQUIREMENT + context_requirement
        )},
    ]

    for index in range(args.max_iterations):
        started = time.time()
        env.reset()
        try:
            reply, llm_stats = model.chat(messages)
        except Exception as e:  # noqa: BLE001 - a provider error ends this run, not the sweep
            run.notes["llm_error"] = str(e)
            break
        messages.append({"role": "assistant", "content": reply})

        if not apply_patch(file, hunk, reply):
            log = "The provided code does not match the region to replace; reply with the full hunk."
            fixed = False
        else:
            fixed, log = env.check_full()

        run.record(Iteration(
            index=index,
            condition=cond.name,
            fixed=fixed,
            feedback=feedback.summary(),
            llm=llm_stats,
            seconds=time.time() - started,
        ))
        print(f"[{bug_id}/{cond.name}] iteration {index + 1}: "
              f"{'FIXED' if fixed else 'not fixed'}")
        if fixed:
            break

        feedback = normalize_feedback(
            log, cond.name,
            bug_type=env.get_bug_type(),
            oracle_budget=args.oracle_budget,
            oracle_strictness=args.strictness,
            allow_promotion=not args.no_promotion,
        )
        messages.append({"role": "user", "content": (
            "Feedback:\n" + feedback.text
            + "\nPlease adjust code according to the feedback."
            + FORMAT_REQUIREMENT + context_requirement
        )})

    try:
        run.certificate = env.dump(log={"model": model.name, "messages": messages})
    except Exception as e:  # noqa: BLE001
        run.notes["dump_error"] = str(e)
    return run


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("bug_ids", nargs="*", help="issue ids; omit with --all")
    parser.add_argument("--all", action="store_true", help="every issue in the dataset")
    parser.add_argument("--condition", default="iraware-structured",
                        help=f"one of {sorted(CONDITIONS)} or a matrix letter A-F")
    parser.add_argument("--out", default="results", help="directory for run records")
    parser.add_argument("--max-iterations", type=int, default=4,
                        help="LLM turns per bug; identical across conditions")
    parser.add_argument("--oracle-budget", type=int, default=400,
                        help="max alive-tv calls per reduction (0 = unlimited)")
    parser.add_argument("--strictness", default="error_class",
                        choices=["any_failure", "error_class", "error_class_and_kind"])
    parser.add_argument("--no-promotion", action="store_true")
    parser.add_argument("--build-jobs", type=int, default=os.cpu_count())
    parser.add_argument("--overwrite", action="store_true",
                        help="redo bugs that already have a record")
    args = parser.parse_args(argv)

    cond = resolve_condition(args.condition)
    args.oracle_budget = args.oracle_budget or None
    _import_benchmark()

    if args.all:
        bug_ids = sorted(
            f.removesuffix(".json") for f in os.listdir(llvm_helper.dataset_dir)
            if f.endswith(".json")
        )
    elif args.bug_ids:
        bug_ids = args.bug_ids
    else:
        parser.error("pass bug ids or --all")

    model = Model()
    for bug_id in bug_ids:
        record = os.path.join(args.out, f"{bug_id}.{cond.name}.json")
        if os.path.exists(record) and not args.overwrite:
            print(f"[{bug_id}] already done under {cond.name}")
            continue
        try:
            run = repair(bug_id, cond.name, args, model)
        except Exception as e:  # noqa: BLE001 - one bad bug must not end the sweep
            print(f"[{bug_id}] error: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        if run is not None:
            print(f"[{bug_id}] -> {run.write(args.out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
