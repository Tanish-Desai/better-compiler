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

    # the preregistered sweep: 9 conditions x 24 bugs x k, bug-major
    python3 examples/repair_experiment.py --sample --repeat 3 --out results/         --condition baseline raw-plain generic-plain llvmreduce-plain iraware-plain                     raw-structured generic-structured llvmreduce-structured                     iraware-structured

Every run writes ``<bug_id>.<condition>[.no-promotion][.tN].json`` into
``--out``; aggregate them with ``python3 examples/summarize_results.py
results/`` and test them with ``python3 examples/analyze_significance.py
results/``.

Passing several ``--condition`` values runs them **bug-major** -- every
condition and trial for one bug before moving to the next -- because the
base_commit switch between bugs is the expensive part of this sweep, not the
per-iteration rebuild. See the comment in ``main`` for the arithmetic.
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

from ce.benchmark import (  # noqa: E402
    Iteration, RunLog, normalize_feedback, record_is_complete, run_record_path,
)
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
Your answer must differ from the code you were given. Returning it unchanged is not a valid answer, and neither is a change that only touches comments or formatting.
"""

#: Sent back when the model returns the window verbatim. Passed through
#: ``normalize_feedback`` unchanged, like any other non-alive2 failure.
NO_OP_FEEDBACK = (
    "You returned the code exactly as it was given to you, so no patch was "
    "produced and nothing was tested. The bug is still present. Identify the "
    "specific line whose behaviour is wrong and change it."
)

SYSTEM_PROMPT = (
    "You are an LLVM maintainer.\n"
    "You are fixing a middle-end bug in the LLVM project." + FORMAT_REQUIREMENT
)


# --------------------------------------------------------------------------
# LLM client
# --------------------------------------------------------------------------

#: Attempts per LLM call, and the waits between them.
#:
#: The OpenAI client already retries connection errors and 5xx twice by
#: itself, but with a sub-second backoff sized for a hiccup -- not for a
#: server that is *gone*. A self-hosted vLLM that OOMs or gets restarted is
#: unavailable for minutes while it reloads 30B of weights, and the sweep runs
#: for days, so this is a when and not an if.
#:
#: What makes it worth guarding is not the lost run, it is the *silently
#: wrong* one. ``repair()`` below breaks out of its loop on an LLM error; a
#: break on turn 1 leaves no iterations and the cell is correctly dropped, but
#: a break on turn 2 of 4 writes a record with ``fixed: false`` that nothing
#: downstream distinguishes from the model genuinely failing to fix the bug.
#: One restart would quietly cost that condition a repair it might have made.
_LLM_ATTEMPTS = 3
_LLM_BACKOFF = (30.0, 120.0)


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
        for attempt in range(_LLM_ATTEMPTS):
            try:
                response = self.client.chat.completions.create(
                    model=self.name,
                    messages=messages,
                    timeout=300,
                    temperature=self.temperature,
                )
                break
            except Exception as e:  # noqa: BLE001 - see _LLM_ATTEMPTS
                status = getattr(e, "status_code", None)
                permanent = (
                    status is not None
                    and 400 <= status < 500
                    and status not in (408, 409, 429)
                )
                # A prompt over the context limit, or a model name that is not
                # served, fails identically on every retry. Waiting 150s to
                # confirm that only slows the sweep down.
                if permanent or attempt == _LLM_ATTEMPTS - 1:
                    raise
                delay = _LLM_BACKOFF[min(attempt, len(_LLM_BACKOFF) - 1)]
                print(f"  LLM call failed ({type(e).__name__}: {e}); retrying "
                      f"in {delay:.0f}s [attempt {attempt + 2}/{_LLM_ATTEMPTS}]",
                      file=sys.stderr)
                time.sleep(delay)

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


def normalise_code(text: str) -> str:
    """Whitespace-insensitive form, so reformatting is not read as an edit."""
    return "\n".join(line.strip() for line in text.strip().splitlines() if line.strip())


def is_no_op(reply: str, original_hunk: str) -> bool:
    """True when the model handed the window straight back.

    Worth its own check rather than letting ``apply_patch`` succeed on it.
    Replacing the hunk with an identical string *does* apply cleanly, so the
    turn goes on to reset the tree, rebuild LLVM and run lit -- several
    minutes spent confirming that unmodified source does not fix the bug.
    That is what 555 of the first sweep's 861 turns did (Blocker 16).
    """
    return normalise_code(extract_code(reply)) == normalise_code(original_hunk)


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


_LINE_COMMENT = re.compile(r"//.*")
_STRING = re.compile(r'"(?:\\.|[^"\\])*"')
_CHAR = re.compile(r"'(?:\\.|[^'\\])*'")


def _brace_delta(line: str) -> int:
    """Net brace depth a line contributes, ignoring comments and literals.

    Crude on purpose -- this is a windowing heuristic, not a C++ parser. Its
    only job is to avoid cutting a function in half, and it is bounded, so
    when it is wrong the result is the old fixed-margin window.
    """
    line = _LINE_COMMENT.sub("", line)
    line = _STRING.sub('""', line)
    line = _CHAR.sub("''", line)
    return line.count("{") - line.count("}")


def _balance_braces(source: List[str], lo: int, hi: int,
                    limit: int = 200) -> Tuple[int, int]:
    """Grow ``[lo, hi]`` (1-based, inclusive) until its braces balance.

    A fixed +/-30 margin cuts wherever it lands, which routinely produces a
    window that opens mid-function and ends on an unterminated brace. Handed
    that, `Qwen2.5-Coder-14B` spent 64.5% of its turns returning the excerpt
    verbatim, and the one edit it did make on bug 89390 was to close the
    dangling function rather than to fix anything (Blocker 16). Extending to
    a brace-balanced window costs a few dozen lines of context and gives the
    model something it can actually parse.

    Balanced means two things, not one. A net depth of zero is not enough: a
    window can close a brace opened above it and open another it never closes
    and still sum to zero, which is how the +/-30 margin produced excerpts that
    both start and end mid-function. So the running depth must never dip below
    zero (nothing closed that was not opened here) *and* must finish at zero.
    """
    for _ in range(limit):
        depth = lowest = 0
        for line in source[lo - 1:hi]:
            depth += _brace_delta(line)
            lowest = min(lowest, depth)
        if lowest < 0 and lo > 1:
            lo -= 1          # reach up for the opening brace we are closing
        elif depth > 0 and hi < len(source):
            hi += 1          # reach down for the closing brace we are missing
        else:
            break
    return lo, hi


def bug_hunk(env, margin: int = 30) -> Tuple[str, str]:
    """The source window the model is asked to rewrite.

    Brace-balanced, so the model receives complete functions rather than an
    excerpt starting mid-statement. Applied identically under every condition,
    so it changes the task's difficulty without touching what is being varied.
    """
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
    lo, hi = _balance_braces(source, lo, hi)
    return bug_file, "\n".join(source[lo - 1:hi])


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------

def repair(bug_id: str, condition: str, args, model: Model,
           trial: int = 1) -> Optional[RunLog]:
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
        trial=trial,
        notes={
            "bug_type": env.get_bug_type(),
            "components": env.get_hint_components(),
            "oracle_budget": args.oracle_budget,
            "oracle_strictness": args.strictness,
            "allow_promotion": not args.no_promotion,
            "temperature": model.temperature,
            "trial": trial,
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

        if is_no_op(reply, hunk):
            patch = "unchanged"
            log = NO_OP_FEEDBACK
            fixed = False
        elif not apply_patch(file, hunk, reply):
            patch = "mismatch"
            log = "The provided code does not match the region to replace; reply with the full hunk."
            fixed = False
        else:
            patch = "applied"
            fixed, log = env.check_full()

        run.record(Iteration(
            index=index,
            condition=cond.name,
            fixed=fixed,
            patch=patch,
            feedback=feedback.summary(),
            llm=llm_stats,
            seconds=time.time() - started,
        ))
        print(f"[{bug_id}/{cond.name}/t{trial}] iteration {index + 1}: "
              f"{'FIXED' if fixed else patch if patch != 'applied' else 'not fixed'}")
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

    if not run.iterations:
        # The provider failed before the model ever answered, so nothing about
        # this bug was tested.  Writing the record anyway would file an
        # infrastructure outage as a failed repair and drag the condition's
        # rate down; returning None leaves the cell empty, which the paired
        # table already knows how to exclude.
        print(f"[{bug_id}/{cond.name}/t{trial}] no iterations ran "
              f"({run.notes.get('llm_error', 'unknown reason')}); not recorded",
              file=sys.stderr)
        return None

    try:
        run.certificate = env.dump(log={"model": model.name, "messages": messages})
    except Exception as e:  # noqa: BLE001
        run.notes["dump_error"] = str(e)
    return run


def sample_bug_ids(path: str) -> List[str]:
    """The stratified sample's bug ids, in ``select_experiment_sample.py`` order.

    Accepts either that script's full output (``{"tiers": {...}}``) or a bare
    list of ids, so a hand-written subset file works for a smoke run.
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return [str(x) for x in data]
    if "bug_ids" in data:
        return [str(x) for x in data["bug_ids"]]
    return [str(row["bug_id"]) for tier in data["tiers"].values() for row in tier]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("bug_ids", nargs="*", help="issue ids; omit with --all/--sample")
    parser.add_argument("--all", action="store_true", help="every issue in the dataset")
    parser.add_argument("--sample", nargs="?", const="data/experiment_sample.json",
                        help="read bug ids from a sample file "
                             "(default data/experiment_sample.json)")
    parser.add_argument("--condition", nargs="+", default=["iraware-structured"],
                        help=f"one or more of {sorted(CONDITIONS)} or a matrix letter A-F")
    parser.add_argument("--repeat", type=int, default=1, metavar="K",
                        help="pass@k repeats per (bug, condition); see docs/ANALYSIS_PLAN.md")
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
                        help="redo cells that already have a record")
    args = parser.parse_args(argv)

    conditions = [resolve_condition(c).name for c in args.condition]
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    args.oracle_budget = args.oracle_budget or None
    _import_benchmark()

    if args.sample:
        bug_ids = sample_bug_ids(args.sample)
    elif args.all:
        bug_ids = sorted(
            f.removesuffix(".json") for f in os.listdir(llvm_helper.dataset_dir)
            if f.endswith(".json")
        )
    elif args.bug_ids:
        bug_ids = args.bug_ids
    else:
        parser.error("pass bug ids, --sample, or --all")

    # BUG-MAJOR ORDER, DELIBERATELY.
    #
    # Every cell of this sweep rebuilds LLVM, but not every rebuild costs the
    # same. Within one bug, all conditions and all trials share a base_commit,
    # so ninja and ccache only have to redo the one patched translation unit
    # and a relink. Moving to the next bug means checking out a base_commit
    # months away in LLVM's history, which is a near-full rebuild (Blocker 9
    # measured 18 min to 2h49m each).
    #
    # Iterating condition-major -- the loop README.md used to show -- pays that
    # switch once per (bug, condition, trial); iterating bug-major pays it once
    # per bug. At 24 bugs x 9 conditions x k that is the difference between
    # 24 expensive rebuilds and 216k of them.
    #
    # Trials sit outside conditions so that an interrupted sweep leaves whole
    # (bug, trial) rows covering every condition, which is exactly what the
    # paired table and the significance test can still use.
    model = Model()
    total = len(bug_ids) * args.repeat * len(conditions)
    done = 0
    for bug_id in bug_ids:
        for trial in range(1, args.repeat + 1):
            for condition in conditions:
                done += 1
                record = run_record_path(args.out, bug_id, condition,
                                         allow_promotion=not args.no_promotion,
                                         trial=trial)
                if os.path.exists(record) and not args.overwrite:
                    if record_is_complete(record):
                        print(f"[{done}/{total}] [{bug_id}] already done under "
                              f"{condition} t{trial}")
                        continue
                    # An endpoint error or a truncated write left a record
                    # behind for a run that never finished. Redo it rather
                    # than inherit it -- see ce.benchmark.record_is_complete.
                    print(f"[{done}/{total}] [{bug_id}/{condition}/t{trial}] "
                          f"previous attempt did not finish; redoing")
                print(f"[{done}/{total}] [{bug_id}/{condition}/t{trial}] starting")
                try:
                    run = repair(bug_id, condition, args, model, trial=trial)
                except Exception as e:  # noqa: BLE001 - one bad cell must not end the sweep
                    print(f"[{bug_id}/{condition}/t{trial}] error: "
                          f"{type(e).__name__}: {e}", file=sys.stderr)
                    continue
                if run is not None:
                    print(f"[{bug_id}] -> {run.write(args.out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
