#!/usr/bin/env python3
"""Run controlled feedback conditions for LLVM repair and record each turn.

WHAT THIS SCRIPT DOES
---------------------
For one bug, it runs the standard repair loop::

    1. show the AI the buggy C++ code and a description of the failure
    2. the AI replies with a rewritten version
    3. build LLVM with that patch
    4. run the bug's reproducer AND its listed regression-test directories
    5. fixed?  stop.  not fixed?  feed the new failure back and go to 2

...up to ``--max-iterations`` times, and records everything that happened.

THE ONE THING THAT VARIES
-------------------------
This runner adapts ``llvm-apr-benchmark/examples/baseline.py``. Within one
versioned experiment, only feedback representation varies across conditions.

Everything else is deliberately identical across conditions -- same model, same
temperature, same prompt wording, same code window, same number of attempts.
That is not laziness, it is the point.  If two conditions differed in any other
way, we could not claim that a difference in results came from the feedback.

WHAT COUNTS AS "FIXED"
----------------------
Not "it compiled".  Not even "the bug's own test passes".  A fix counts only if
``env.check_full()`` passes, which means the patch builds, passes its listed
reproducers and passes the selected LLVM regression directories. That standard is
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
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import List, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, os.pardir))

from ce.benchmark import (  # noqa: E402
    Iteration, RunLog, normalize_feedback, record_is_complete, run_record_path,
)
from ce.feedback import CONDITIONS, resolve_condition  # noqa: E402
from ce.experiment import ExperimentMismatch, prepare_output  # noqa: E402
from ce.patching import PatchFormatError, apply_edits, extract_single_code  # noqa: E402
from ce.protocol import load_protocol  # noqa: E402

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


FORMAT_REQUIREMENT = (
    "\nReturn only the smallest targeted edits in this exact format:\n"
    "<<<<<<< SEARCH\n"
    "exact existing lines from the ORIGINAL source excerpt\n"
    "=======\n"
    "replacement lines\n"
    ">>>>>>> REPLACE\n"
    "You may return multiple non-overlapping blocks. SEARCH must match exactly once\n"
    "in the original excerpt, including indentation. Include enough unchanged context\n"
    "to make each SEARCH unique. Every turn edits the ORIGINAL code, not your previous\n"
    "reply. The excerpt may start or end inside a function; never replace the whole\n"
    "excerpt with a complete function. Do not return explanations, Markdown fences,\n"
    "ellipsis placeholders, an unchanged block, or comment-only/format-only edits.\n"
)

# Increment when windowing, patch handling or validation semantics change.
# Prompt wording is hashed as well, so editing it cannot reuse old cells.
HARNESS_VERSION = "repair-v3"


class BaselineBuildError(RuntimeError):
    """Infrastructure failure: stop the sweep rather than query the model."""

#: Sent back when the model returns the window verbatim. Passed through
#: ``normalize_feedback`` unchanged, like any other non-alive2 failure.
NO_OP_FEEDBACK = (
    "You returned the code exactly as it was given to you, so no patch was "
    "produced and nothing was tested. The bug is still present. Identify the "
    "specific line whose behaviour is wrong and change it."
)

SYSTEM_PROMPT = (
    "You are an LLVM maintainer.\n"
    "You are fixing a middle-end bug in the LLVM project.\n"
    "Make the smallest targeted change that addresses the reported failure. "
    "Preserve unrelated optimizations and IR invariants. A valid repair must "
    "pass both the reproducer and the existing regression tests. When tests "
    "fail, use their specific diagnostics to revise your previous patch; "
    "do not disable the optimization wholesale or weaken the tests."
    + FORMAT_REQUIREMENT
)


@lru_cache(maxsize=8)
def _hash_dataset_records(directory: str, fingerprints: tuple) -> tuple[str, tuple]:
    """Hash actual selected inputs; stat fingerprints avoid rereading each cell.

    Paths only identify the local cache entry. They do not enter the digest,
    so moving an unchanged checkout does not change experiment identity.
    """
    digest = hashlib.sha256()
    records = []
    for bug, _mtime_ns, _size in fingerprints:
        raw = (Path(directory) / f"{bug}.json").read_bytes().replace(b"\r\n", b"\n")
        item_hash = hashlib.sha256(raw).hexdigest()
        digest.update(bug.encode() + b"\0" + item_hash.encode() + b"\0")
        records.append((bug, item_hash))
    return digest.hexdigest(), tuple(records)


def dataset_identity(args) -> dict | None:
    selected = getattr(args, "dataset_bug_ids", None)
    directory = os.environ.get("LAB_DATASET_DIR")
    if not selected or not directory:
        # A plan can be shown before sourcing the native environment. Real
        # generation requires LAB_DATASET_DIR in _import_benchmark().
        return None
    directory = str(Path(directory).resolve())
    fingerprints = []
    for bug in sorted(set(map(str, selected))):
        stat = (Path(directory) / f"{bug}.json").stat()
        fingerprints.append((bug, stat.st_mtime_ns, stat.st_size))
    digest, records = _hash_dataset_records(directory, tuple(fingerprints))
    return {"sha256": digest, "records": dict(records)}


def completion_token_limit() -> int:
    try:
        value = int(os.environ.get("LAB_LLM_MAX_TOKENS", "8192"))
    except ValueError as error:
        raise ValueError("LAB_LLM_MAX_TOKENS must be a positive integer") from error
    if value < 1:
        raise ValueError("LAB_LLM_MAX_TOKENS must be a positive integer")
    return value


def experiment_config(args) -> dict:
    sources = ("examples/repair_experiment.py", "ce/benchmark.py", "ce/feedback.py",
               "ce/patching.py", "ce/oracle.py", "ce/protocol.py", "ce/experiment.py",
               "ce/reduction.py", "ce/reduce_generic.py",
               "ce/reduce_iraware.py", "ce/reduce_llvmreduce.py", "ce/_llvmreduce_test.py",
               "ce/alive.py", "ce/irmodel.py", "ce/structured.py",
               "llvm-apr-benchmark/scripts/llvm_helper.py", "llvm-apr-benchmark/scripts/lab_env.py")
    digest = hashlib.sha256()
    root = os.path.join(_HERE, os.pardir)
    for source in sources:
        with open(os.path.join(root, source), "rb") as handle:
            digest.update(source.encode() + b"\0" + handle.read().replace(b"\r\n", b"\n") + b"\0")
    return {
        "model": os.environ.get("LAB_LLM_MODEL", "deepseek-reasoner"),
        "base_model_cutoff": os.environ.get("LAB_LLM_BASEMODEL_CUTOFF", "2023-12-31Z"),
        "temperature": float(os.environ.get("LAB_LLM_TEMP", "0.8")),
        "max_iterations": args.max_iterations,
        "oracle_budget": args.oracle_budget,
        "oracle_strictness": args.strictness,
        "harness_version": HARNESS_VERSION,
        "prompt_hash": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
        "patch_format": "search-replace-v1",
        "max_completion_tokens": completion_token_limit(),
        "harness_code_hash": digest.hexdigest(),
        "protocol_id": getattr(args, "protocol_id", None),
        "protocol_sha256": getattr(args, "protocol_sha256", None),
        "model_repository": os.environ.get("LAB_LLM_MODEL_REPOSITORY"),
        "model_revision": os.environ.get("LAB_LLM_MODEL_REVISION"),
        "serving_identity": os.environ.get("LAB_LLM_SERVING_ID"),
        "dataset_identity": dataset_identity(args),
    }


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
        self.max_tokens = completion_token_limit()
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

    def check_endpoint(self) -> None:
        served = {entry.id for entry in self.client.models.list(timeout=30).data}
        if self.name not in served:
            raise RuntimeError(f"LAB_LLM_MODEL={self.name!r} is not served by the endpoint "
                               f"(available: {', '.join(sorted(served))}).")

    def chat(self, messages: List[dict]) -> Tuple[str, dict]:
        for attempt in range(_LLM_ATTEMPTS):
            try:
                response = self.client.chat.completions.create(
                    model=self.name,
                    messages=messages,
                    timeout=300,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
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
            "finish_reason": response.choices[0].finish_reason,
            "response_model": getattr(response, "model", self.name),
        }
        return (response.choices[0].message.content or ""), stats


# --------------------------------------------------------------------------
# Explicit edit handling (v3; historical whole-window replies are not applied)
# --------------------------------------------------------------------------

def extract_code(reply: str) -> str:
    return extract_single_code(reply)


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
    try:
        code = (apply_edits(original_hunk, reply) if "<<<<<<< SEARCH" in reply
                else extract_code(reply))
    except PatchFormatError:
        return False
    return normalise_code(code) == normalise_code(original_hunk)


def apply_patch(file: str, original_hunk: str, reply: str) -> bool:
    """Apply validated edits only inside the uniquely identified source excerpt."""
    replacement = apply_edits(original_hunk, reply)
    path = os.path.join(llvm_helper.llvm_dir, file)
    with open(path, encoding="utf-8") as f:
        code = f.read()
    if code.count(original_hunk) != 1:
        return False
    with open(path, "w", encoding="utf-8") as f:
        f.write(code.replace(original_hunk, replacement, 1))
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
    """Bounded source context for exact edits; it may contain partial constructs.

    Brace balancing is best effort, not a C++ parser. The edit protocol preserves
    all text outside explicit SEARCH spans even if the context is incomplete.
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
            "experiment_config": experiment_config(args),
            "build_jobs": args.build_jobs,
            "source_commit": subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=os.path.join(_HERE, os.pardir),
                capture_output=True, text=True, timeout=10,
            ).stdout.strip() or None,
        },
    )
    env.use_knowledge("alive2", env.knowledge_cutoff)

    env.reset()
    ok, log = env.check_fast()
    if env.build_failure_count:
        raise BaselineBuildError("The unpatched LLVM build failed; this is not a repair "
                                 f"outcome. Fix the build first.\n{str(log)[-4000:]}")
    if ok:
        print(f"[{bug_id}] skipped: the bug does not reproduce at base_commit")
        return None

    file, hunk = bug_hunk(env)

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
            + FORMAT_REQUIREMENT
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

        format_error = None
        try:
            apply_edits(hunk, reply)
        except PatchFormatError as error:
            format_error = str(error)

        if llm_stats.get("finish_reason") == "length":
            patch = "truncated"
            log = "Your reply hit the output limit and was not applied. Return smaller SEARCH/REPLACE blocks."
            fixed = False
            validation = {"stage": "no_patch", "passed": False, "failure_kind": "output_limit"}
        elif is_no_op(reply, hunk):
            patch = "unchanged"
            log = NO_OP_FEEDBACK
            fixed = False
            validation = {"stage": "no_patch", "passed": False}
        elif format_error:
            patch = "invalid_format"
            log = f"No source was changed: {format_error}\nReturn exact SEARCH/REPLACE blocks against the original excerpt."
            fixed = False
            validation = {"stage": "no_patch", "passed": False, "failure_kind": "patch_format"}
        elif not apply_patch(file, hunk, reply):
            patch = "mismatch"
            log = "The original source excerpt was not found uniquely; no patch was applied."
            fixed = False
            validation = {"stage": "no_patch", "passed": False}
        else:
            patch = "applied"
            fixed, log = env.check_full()
            validation = dict(env.last_validation)

        run.record(Iteration(
            index=index,
            condition=cond.name,
            fixed=fixed,
            patch=patch,
            feedback=feedback.summary(),
            llm=llm_stats,
            seconds=time.time() - started,
            validation=validation,
        ))
        outcome = "FIXED" if fixed else patch if patch != "applied" else "not fixed"
        detail = "" if fixed or patch != "applied" else f" ({validation.get('stage')}: {validation.get('failure_kind')})"
        print(f"[{bug_id}/{cond.name}/t{trial}] iteration {index + 1}: {outcome}{detail}")
        if fixed:
            break

        if index + 1 == args.max_iterations:
            # Save the actual last diagnostic without reducing feedback that
            # no model will consume. This can otherwise cost hundreds of calls.
            text = log if isinstance(log, str) else json.dumps(log, ensure_ascii=False)
            messages.append({"role": "user", "content": "Feedback:\n" + text})
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
            + FORMAT_REQUIREMENT
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
    supplied = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("bug_ids", nargs="*", help="issue ids; omit with --all/--sample")
    parser.add_argument("--all", action="store_true", help="every issue in the dataset")
    parser.add_argument("--sample", nargs="?", const="data/experiment_sample.json",
                        help="read bug ids from a sample file "
                             "(default data/experiment_sample.json)")
    parser.add_argument("--protocol", help="load a frozen prospective schedule and verify its settings")
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
    parser.add_argument("--plan", action="store_true",
                        help="check experiment compatibility and list remaining cells; "
                             "no endpoint, LLVM imports, builds or writes")
    args = parser.parse_args(argv)

    protocol_bugs = None
    if args.protocol:
        try:
            protocol, protocol_sha = load_protocol(args.protocol)
        except (OSError, ValueError) as error:
            parser.error(str(error))
        if args.bug_ids or args.sample or args.all or args.no_promotion:
            parser.error("--protocol supplies its own bugs and promotion-on schedule")
        if protocol["harness_version"] != HARNESS_VERSION:
            parser.error("Protocol harness_version does not match this runner")
        expected_env = {"LAB_LLM_MODEL": protocol["model"],
                        "LAB_LLM_TEMP": protocol["temperature"],
                        "LAB_LLM_MAX_TOKENS": protocol["max_completion_tokens"]}
        defaults = {"LAB_LLM_MODEL": "deepseek-reasoner", "LAB_LLM_TEMP": "0.8",
                    "LAB_LLM_MAX_TOKENS": "8192"}
        for key, expected in expected_env.items():
            actual = os.environ.get(key, defaults[key])
            if str(actual) != str(expected):
                parser.error(f"{key}={actual!r} differs from protocol value {expected!r}")
        fields = (("--condition", "condition", "conditions"), ("--repeat", "repeat", "repeat"),
                  ("--max-iterations", "max_iterations", "max_iterations"),
                  ("--oracle-budget", "oracle_budget", "oracle_budget"),
                  ("--strictness", "strictness", "oracle_strictness"))
        for option, attribute, key in fields:
            if any(value == option or value.startswith(option + "=") for value in supplied):
                if getattr(args, attribute) != protocol[key]:
                    parser.error(f"{option} differs from the frozen protocol")
            setattr(args, attribute, protocol[key])
        args.protocol_id, args.protocol_sha256 = protocol["id"], protocol_sha
        protocol_bugs = protocol["bugs"]

    conditions = [resolve_condition(c).name for c in args.condition]
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    if args.max_iterations < 1 or args.build_jobs < 1:
        parser.error("--max-iterations and --build-jobs must be positive")
    args.oracle_budget = args.oracle_budget or None

    if protocol_bugs is not None:
        bug_ids = protocol_bugs
    elif args.sample:
        bug_ids = sample_bug_ids(args.sample)
    elif args.all:
        bug_ids = sorted(
            f.removesuffix(".json") for f in os.listdir(os.environ["LAB_DATASET_DIR"])
            if f.endswith(".json")
        )
    elif args.bug_ids:
        bug_ids = args.bug_ids
    else:
        parser.error("pass bug ids, --sample, or --all")

    args.dataset_bug_ids = tuple(bug_ids)
    try:
        prepare_output(args.out, experiment_config(args), write=False)
    except (ExperimentMismatch, OSError, ValueError) as e:
        parser.error(str(e))
    if args.plan:
        total = len(bug_ids) * args.repeat * len(conditions)
        complete = sum(
            record_is_complete(run_record_path(
                args.out, bug, condition, allow_promotion=not args.no_promotion, trial=trial))
            for bug in bug_ids for trial in range(1, args.repeat + 1) for condition in conditions
        )
        print(f"Model: {experiment_config(args)['model']}; harness: {HARNESS_VERSION}")
        print(f"{total} cells: {0 if args.overwrite else complete} skipped, "
              f"{total if args.overwrite else total - complete} to run")
        print(f"Bugs in order: {', '.join(bug_ids)}")
        return 0

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
    if not os.environ.get("LAB_LLM_TOKEN"):
        parser.error("LAB_LLM_TOKEN is not set")
    model = Model()
    try:
        model.check_endpoint()
    except Exception as e:
        parser.error(f"Endpoint preflight failed before any LLVM build: {e}")
    _import_benchmark()
    prepare_output(args.out, experiment_config(args))
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
                except BaselineBuildError as e:
                    print(f"[{bug_id}] stopping sweep: {e}", file=sys.stderr)
                    return 1
                except Exception as e:  # noqa: BLE001 - one bad cell must not end the sweep
                    print(f"[{bug_id}/{condition}/t{trial}] error: "
                          f"{type(e).__name__}: {e}", file=sys.stderr)
                    continue
                if run is not None:
                    print(f"[{bug_id}] -> {run.write(args.out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
