#!/usr/bin/env python3
"""Compare reducers on real initial witnesses saved by a repair sweep.

This experiment measures reducer behavior on the selected witness corpus.
No model, GPU, compiler rebuild, reference patch, or generated mutation is used.
The default input is the frozen, incomplete v2 snapshot, with one witness per
bug. Initial raw-plain/raw-structured copies must agree across conditions and
trials; conflicting cases are reported and excluded rather than selected.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import time
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ce.alive import FunctionResult, parse_extra_args, run_alive_tv  # noqa: E402
from ce.feedback import Feedback, build_feedback  # noqa: E402
from ce.irmodel import measure_pair  # noqa: E402
from ce.oracle import Oracle, Violation  # noqa: E402
from ce.reduce_iraware import reduce_iraware  # noqa: E402
from ce.reduce_llvmreduce import DEFAULT_SUBPROCESS_TIMEOUT, default_llvm_reduce, reduce_llvmreduce  # noqa: E402
from ce.reduction import make_reduction  # noqa: E402
from ce.structured import render_plain  # noqa: E402

FROZEN_V2 = "73f2ba151742c44cf88878b50c027ef7f06293c6"
METHODS = ("raw", "generic", "llvmreduce", "iraware", "iraware-no-promotion")
HYBRID = "iraware-then-llvmreduce"
CORPUS_NOTE = (
    "Development/diagnostic corpus selected by the incomplete v2 sweep: real "
    "initial compiler witnesses, one per observed bug. These tiny witnesses "
    "were used during harness development. Results do not establish repair "
    "improvement, independence from development, or generalization."
)
EXPLICIT_CORPUS_NOTE = (
    "Reducer comparison on saved initial witnesses, one per observed bug. "
    "The source selection determines the scope. This replay does not estimate "
    "the effect of reduction on repair."
)
PLAIN = re.compile(
    r"(?:^|\n)Source:\n(.*?)\n\nTarget:\n(.*?)\n\n"
    r"(?=Transformation doesn.t verify!)", re.S,
)
STRUCTURED = re.compile(
    r"(?:^|\n)SOURCE \(pre-transformation IR\):\n(.*?)\n\n"
    r"TARGET \(post-transformation IR\):\n(.*?)\n\n"
    r"(?=WHAT THE TRANSFORMATION CHANGED:)", re.S,
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_ir(text: str) -> str:
    """Normalize transport line endings and outer whitespace only."""
    return text.replace("\r\n", "\n").strip() + "\n"


def pair_id(src: str, tgt: str, args: tuple[str, ...]) -> str:
    return digest(json.dumps([src, tgt, list(args)], ensure_ascii=False).encode())


@dataclass
class Witness:
    bug_id: str
    src: str
    tgt: str
    extra_args: tuple[str, ...]
    test_name: str
    origins: list[str] = field(default_factory=list)
    dataset_files: list[str] = field(default_factory=list)
    dataset_sha256: str = ""

    @property
    def identity(self) -> str:
        return pair_id(self.src, self.tgt, self.extra_args)


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(ROOT), *args])


def load_snapshot(revision: str, results: str) -> tuple[dict, dict, dict]:
    """Read immutable blobs in memory; never check out or extract the snapshot."""
    revision = git("rev-parse", f"{revision}^{{commit}}").decode().strip()
    results = results.replace("\\", "/").strip("/")
    if not results or ".." in results.split("/"):
        raise ValueError("--results must name a repository-relative directory")
    archive = git("archive", "--format=tar", revision, "--", results,
                  "llvm-apr-benchmark/dataset")
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        blobs = {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}
    prefix = results + "/"
    records = {name: body for name, body in blobs.items()
               if name.startswith(prefix) and name.endswith(".json")
               and "/" not in name[len(prefix):] and not Path(name).name.startswith(".")}
    dataset = {name: body for name, body in blobs.items()
               if name.startswith("llvm-apr-benchmark/dataset/") and name.endswith(".json")}
    return records, dataset, {"mode": "git_snapshot", "revision": revision,
                              "results": results}


def load_directory(directory: Path, dataset_dir: Path) -> tuple[dict, dict, dict]:
    if not directory.is_dir() or not dataset_dir.is_dir():
        raise ValueError("both the result directory and dataset directory must exist")
    records = {str(p.resolve()): p.read_bytes() for p in sorted(directory.glob("*.json"))
               if not p.name.startswith(".")}
    dataset = {str(p.resolve()): p.read_bytes() for p in sorted(dataset_dir.glob("*.json"))}
    return records, dataset, {"mode": "directory", "results": str(directory.resolve()),
                              "dataset": str(dataset_dir.resolve())}


def initial_pair(record: dict) -> tuple[str, str]:
    """Extract only the first user turn; later model-induced witnesses are excluded."""
    messages = ((record.get("certificate") or {}).get("log") or {}).get("messages") or []
    initial = next((m.get("content", "") for m in messages if m.get("role") == "user"), "")
    if not isinstance(initial, str):
        raise ValueError("initial user message is not text")
    initial = initial.replace("\r\n", "\n")
    # Exclude the editable C++ window, which can itself contain label-like text.
    initial = initial.split("\nPlease modify the following code in ", 1)[0]
    if "Detailed information:\n" in initial:
        initial = initial.split("Detailed information:\n", 1)[1]
    pattern = PLAIN if record.get("condition") == "raw-plain" else STRUCTURED
    matches = list(pattern.finditer(initial))
    if len(matches) != 1:
        raise ValueError(f"expected one initial raw pair, found {len(matches)}")
    return tuple(canonical_ir(part) for part in matches[0].groups())


def verifier_context(record: dict, src: str, issue: dict) -> tuple[tuple[str, ...], str, list[str]]:
    iterations = record.get("iterations") or []
    test_name = (iterations[0].get("feedback") or {}).get("failed_test") if iterations else None
    if not test_name:
        raise ValueError("initial feedback has no failed_test; verifier arguments cannot be recovered")
    candidates = [(test, group) for group in issue.get("tests", [])
                  for test in group.get("tests", []) if test.get("test_name") == test_name]
    if not candidates:
        raise ValueError(f"dataset has no test {test_name!r}")
    # Some old adapters removed these attributes before verification. Prefer an
    # exact matching body, but allow repeated test names when their args agree.
    def body(text):
        return canonical_ir(text).replace(" noalias ", " ").replace(" nofree ", " ")
    matching = [(test, group) for test, group in candidates
                if body(test.get("test_body", "")) == body(src)]
    candidates = matching or candidates
    contexts = {tuple(parse_extra_args(test.get("additional_args") or ""))
                for test, _ in candidates}
    if len(contexts) != 1:
        raise ValueError(f"ambiguous verifier arguments for test {test_name!r}")
    return next(iter(contexts)), str(test_name), sorted({g.get("file", "") for _, g in candidates})


def collect_witnesses(records: dict[str, bytes], dataset: dict[str, bytes]) -> tuple[list[Witness], list[dict]]:
    """Reject disagreements; retain extraction errors in the report denominator."""
    issues: list[dict] = []
    datasets = defaultdict(list)
    for path, blob in sorted(dataset.items()):
        try:
            issue = json.loads(blob)
            datasets[str(issue["bug_id"])].append((path, issue, digest(blob)))
        except (ValueError, TypeError, KeyError) as exc:
            issues.append({"stage": "dataset", "file": path, "error": str(exc)})
    candidates = defaultdict(list)
    observed: set[str] = set()
    invalid: set[str] = set()
    for path, blob in sorted(records.items()):
        bug = None
        try:
            record = json.loads(blob)
            bug = str(record["bug_id"])
            observed.add(bug)
            if record.get("condition") not in ("raw-plain", "raw-structured"):
                continue
            src, tgt = initial_pair(record)
            entries = datasets.get(bug, [])
            if len(entries) != 1:
                raise ValueError(f"expected one dataset record for bug, found {len(entries)}")
            _, issue, data_hash = entries[0]
            args, test_name, files = verifier_context(record, src, issue)
            candidates[bug].append(Witness(bug, src, tgt, args, test_name, [path], files, data_hash))
        except (ValueError, TypeError, KeyError, AttributeError, IndexError) as exc:
            if bug is not None:
                invalid.add(bug)
            issues.append({"stage": "extract", "bug_id": bug, "file": path, "error": str(exc)})
    witnesses = []
    for bug in sorted(observed, key=lambda b: (not b.isdigit(), int(b) if b.isdigit() else b)):
        copies = candidates.get(bug, [])
        identities = {w.identity for w in copies}
        if not copies:
            issues.append({"stage": "corpus", "bug_id": bug,
                           "error": "no usable initial raw witness"})
        elif len(identities) != 1:
            issues.append({"stage": "corpus", "bug_id": bug,
                           "error": "initial raw witnesses disagree across conditions/trials",
                           "identities": sorted(identities),
                           "origins": [w.origins[0] for w in copies]})
        elif bug in invalid:
            issues.append({"stage": "corpus", "bug_id": bug,
                           "error": "excluded because at least one raw copy could not be checked"})
        else:
            witness = copies[0]
            witness.origins = sorted(w.origins[0] for w in copies)
            witnesses.append(witness)
    return witnesses, issues


@contextmanager
def llvm_reduce_override(path: str):
    old = os.environ.get("LAB_LLVM_LLVM_REDUCE")
    os.environ["LAB_LLVM_LLVM_REDUCE"] = path
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("LAB_LLVM_LLVM_REDUCE", None)
        else:
            os.environ["LAB_LLVM_LLVM_REDUCE"] = old


def run_details(run) -> dict:
    violation = run.first_violation()
    return {"tool_error": run.tool_error, "num_incorrect": run.num_incorrect,
            "num_errors": run.num_errors,
            "violation": asdict(Violation.from_result(violation)) if violation else None,
            "raw": run.raw}


def hybrid_feedback(witness: Witness, violation: FunctionResult, *, alive_tv: str, timeout: float,
                    budget: int, prefix_calls: int, verifier: Callable) -> Feedback:
    """Start LLVM reduction from the best pair found under a small paired cap."""
    original = witness.src, witness.tgt
    oracle = Oracle(target=Violation.from_result(violation), strictness="error_class",
                    extra_args=witness.extra_args, alive_tv=alive_tv,
                    timeout=timeout, max_calls=prefix_calls)

    def size_key(pair: tuple[str, str]) -> tuple[int, int]:
        size = measure_pair(*pair)
        return size["instructions"], size["chars"]

    started = time.perf_counter()
    paired = reduce_iraware(*original, oracle, violation)
    paired_calls = oracle.calls
    seed = (paired.src, paired.tgt)
    use_seed = not paired.error and size_key(seed) < size_key(original)
    pair = seed if use_seed else original
    selected = "iraware" if use_seed else "original"
    passes = paired.passes_applied if use_seed else []

    oracle.max_calls = budget
    sequential = reduce_llvmreduce(*pair, oracle)
    candidate = sequential.src, sequential.tgt
    if not sequential.error and size_key(candidate) < size_key(pair):
        pair = candidate
        selected = "llvmreduce"
        passes = passes + sequential.passes_applied

    metrics = {"hybrid_prefix_cap": prefix_calls, "hybrid_seed_used": use_seed,
               "hybrid_selected_stage": selected, "feedback_refresh_calls": 0,
               "hybrid_stages": [
                   {"method": "iraware", "candidate_calls": paired_calls,
                    "size_after": paired.size_after, "error": paired.error},
                   {"method": "llvmreduce", "candidate_calls": oracle.calls - paired_calls,
                    "size_after": sequential.size_after, "error": sequential.error},
               ]}
    reduction_seconds = time.perf_counter() - started
    shown, error = violation, None
    if pair != original:
        refresh_start = time.perf_counter()
        refresh = verifier(*pair, witness.extra_args, alive_tv=alive_tv, timeout=timeout)
        metrics.update(feedback_refresh_calls=1,
                       feedback_refresh_seconds=time.perf_counter() - refresh_start,
                       feedback_refresh_tool_failures=int(bool(refresh.tool_error)))
        match = (oracle._match(refresh) if not refresh.tool_error and not refresh.num_errors
                 and refresh.num_incorrect else None)
        if match is None:
            pair, passes = original, []
            error = "hybrid pair failed re-verification; reverted"
        else:
            shown = match
    metrics["reduction_fallback"] = bool(error)
    reduction = make_reduction(HYBRID, *original, *pair, passes_applied=passes,
                               oracle_stats=oracle.stats(), seconds=reduction_seconds, error=error)
    return Feedback(render_plain(shown, *pair), HYBRID, violation=shown,
                    reduction=reduction, metrics=metrics, error=error)


def compare_witness(witness: Witness, *, alive_tv: str, budget: int = 400,
                    timeout: float = 120, verifier: Callable | None = None,
                    feedback_builder: Callable | None = None,
                    hybrid_prefix_calls: int | None = None) -> dict:
    """One common establishment call, equal search caps, separate fresh postchecks."""
    verifier = verifier or run_alive_tv
    feedback_builder = feedback_builder or build_feedback
    methods = METHODS
    if hybrid_prefix_calls is not None:
        if not 1 <= hybrid_prefix_calls <= budget - 2:
            raise ValueError("hybrid prefix must leave at least two calls for sequential reduction")
        methods += (HYBRID,)
    case = {**asdict(witness), "witness_sha256": witness.identity,
            "original_size": measure_pair(witness.src, witness.tgt), "methods": []}
    started = time.perf_counter()
    original = verifier(witness.src, witness.tgt, witness.extra_args,
                        alive_tv=alive_tv, timeout=timeout)
    case["establishment_seconds"] = round(time.perf_counter() - started, 6)
    case["establishment_calls"] = 1
    case["establishment"] = run_details(original)
    violation = original.first_violation()
    if (original.tool_error or not original.num_incorrect or original.num_errors or violation is None
            or not violation.name or not violation.error_class):
        case["status"] = "skipped"
        case["reason"] = "original pair did not establish a refinement failure with a parsed function and error class"
        case["methods"] = [{"method": method, "status": "skipped", "reason": case["reason"],
                            "candidate_budget": budget} for method in methods]
        return case
    case["status"] = "completed"
    target = Violation.from_result(violation)
    matcher = Oracle(target=target, strictness="error_class")
    for method in methods:
        row = {"method": method, "candidate_budget": budget,
               "strictness": "error_class", "allow_promotion": method != "iraware-no-promotion"}
        start = time.perf_counter()
        try:
            if method == HYBRID:
                feedback = hybrid_feedback(witness, violation, alive_tv=alive_tv,
                                           timeout=timeout, budget=budget,
                                           prefix_calls=hybrid_prefix_calls, verifier=verifier)
            else:
                strategy = "iraware" if method == "iraware-no-promotion" else method
                feedback = feedback_builder(
                    witness.src, witness.tgt, f"{strategy}-plain", extra_args=witness.extra_args,
                    alive_tv=alive_tv, timeout=timeout, oracle_budget=budget,
                    oracle_strictness="error_class", allow_promotion=row["allow_promotion"],
                    alive_output=original.raw,
                )
            row["pipeline_seconds"] = round(time.perf_counter() - start, 6)
            summary = feedback.summary()
            row["feedback"] = summary
            src, tgt = ((feedback.reduction.src, feedback.reduction.tgt)
                        if feedback.reduction else (witness.src, witness.tgt))
            row.update({"displayed_src": src, "displayed_tgt": tgt,
                        "displayed_pair_sha256": pair_id(src, tgt, witness.extra_args),
                        "displayed_size": measure_pair(src, tgt),
                        "feedback_chars": len(feedback.text), "feedback_text": feedback.text,
                        "fallback": bool(summary.get("reduction_fallback", False)),
                        "candidate_calls": int(summary.get("oracle_calls", 0)),
                        "candidate_seconds": float(summary.get("oracle_seconds", 0)),
                        "candidate_tool_failures": int(summary.get("oracle_tool_failures", 0)),
                        "refresh_calls": int(summary.get("feedback_refresh_calls", 0)),
                        "refresh_seconds": float(summary.get("feedback_refresh_seconds", 0)),
                        "refresh_tool_failures": int(summary.get("feedback_refresh_tool_failures", 0))})
            check_start = time.perf_counter()
            final = verifier(src, tgt, witness.extra_args, alive_tv=alive_tv, timeout=timeout)
            row["postcheck_seconds"] = round(time.perf_counter() - check_start, 6)
            row["postcheck_calls"] = 1
            row["postcheck"] = run_details(final)
            matched = (not final.tool_error and not (final.num_errors and not final.num_incorrect)
                       and matcher._match(final) is not None)
            row["same_violation_fresh"] = matched
            row["total_physical_calls"] = row["candidate_calls"] + row["refresh_calls"] + 1
            row["total_seconds"] = round(row["pipeline_seconds"] + row["postcheck_seconds"], 6)
            if row["candidate_calls"] > budget:
                row["status"] = "failed"
                row["reason"] = "candidate-call budget exceeded"
            elif not matched:
                row["status"] = "failed"
                row["reason"] = "displayed witness failed the independent same-function/error-class check"
            elif feedback.error:
                row["status"] = "fallback" if row["fallback"] else "failed"
                row["reason"] = feedback.error
            else:
                row["status"] = "fallback" if row["fallback"] else "ok"
        except Exception as exc:  # Keep other methods and the failed row in the report.
            row.update(status="error", reason=f"{type(exc).__name__}: {exc}",
                       pipeline_seconds=round(time.perf_counter() - start, 6),
                       cost_complete=False)
        case["methods"].append(row)
    return case


def binary_path(value: str | None, fallback_name: str) -> str:
    candidate = value or shutil.which(fallback_name)
    resolved = shutil.which(candidate) if candidate else None
    if not resolved and candidate and Path(candidate).is_file():
        resolved = str(Path(candidate).resolve())
    if not resolved:
        raise ValueError(f"cannot find {fallback_name}; supply its explicit binary path")
    return str(Path(resolved).resolve())


def binary_identity(path: str) -> dict:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    out = {"path": path, "sha256": hasher.hexdigest()}
    try:
        result = subprocess.run([path, "--version"], capture_output=True, text=True,
                                errors="replace", timeout=15)
        out.update(version_exit=result.returncode, version=(result.stdout + result.stderr)[:2048])
    except (OSError, subprocess.TimeoutExpired) as exc:
        out["version_error"] = str(exc)
    return out


def source_identity() -> dict:
    paths = [Path(__file__), *sorted((ROOT / "ce").glob("*.py"))]
    return {str(path.relative_to(ROOT)).replace("\\", "/"): digest(path.read_bytes()) for path in paths}


def write_report(path: Path, report: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--snapshot", metavar="GIT_REF", help=f"default: frozen v2 {FROZEN_V2[:7]}")
    source.add_argument("--directory", type=Path, help="explicit result directory; use its matching dataset")
    parser.add_argument("--results", default="results-qwen3-v2", help="result path inside a Git snapshot")
    parser.add_argument("--dataset", type=Path, default=ROOT / "llvm-apr-benchmark" / "dataset",
                        help="dataset directory for --directory mode only")
    parser.add_argument("--out", type=Path, default=Path("witness-comparison-v2"))
    parser.add_argument("--budget", type=int, default=400, help="equal candidate-call cap per reducer")
    parser.add_argument("--hybrid-prefix-calls", type=int,
                        help="add an IR-aware prefix followed by llvm-reduce under the same total cap")
    parser.add_argument("--timeout", type=float, default=120, help="per alive-tv call timeout in seconds")
    parser.add_argument("--alive-tv", default=os.environ.get("LAB_LLVM_ALIVE_TV"))
    parser.add_argument("--llvm-reduce", default=default_llvm_reduce())
    parser.add_argument("--bug", nargs="+", help="explicitly limit bug IDs; recorded as a selected subset")
    parser.add_argument("--plan", action="store_true", help="inventory only; no binaries or output directory needed")
    args = parser.parse_args(argv)
    if args.budget <= 0 or args.timeout <= 0:
        parser.error("budget and timeout must be positive")
    if args.hybrid_prefix_calls is not None and not 1 <= args.hybrid_prefix_calls <= args.budget - 2:
        parser.error("--hybrid-prefix-calls must leave at least two calls for sequential reduction")
    methods = METHODS if args.hybrid_prefix_calls is None else (*METHODS, HYBRID)
    try:
        records, dataset, source_info = (load_directory(args.directory, args.dataset) if args.directory
                                        else load_snapshot(args.snapshot or FROZEN_V2, args.results))
        witnesses, issues = collect_witnesses(records, dataset)
        if args.bug:
            requested = set(args.bug)
            available = {w.bug_id for w in witnesses}
            issues.extend({"stage": "selection", "bug_id": bug, "error": "requested bug has no usable witness"}
                          for bug in sorted(requested - available))
            witnesses = [w for w in witnesses if w.bug_id in requested]
        corpus_note = (CORPUS_NOTE if source_info.get("revision") == FROZEN_V2
                       and source_info.get("results") == "results-qwen3-v2"
                       else EXPLICIT_CORPUS_NOTE)
        print(corpus_note)
        print(f"{len(records)} saved records; {len(witnesses)} agreed initial witnesses; {len(issues)} extraction issues")
        for witness in witnesses:
            size = measure_pair(witness.src, witness.tgt)
            print(f"  {witness.bug_id}: {size['instructions']} instructions, {size['chars']} chars, "
                  f"{len(witness.origins)} agreeing copies, args={list(witness.extra_args)}")
        if args.plan:
            for issue in issues:
                print(f"  ISSUE: {json.dumps(issue, ensure_ascii=False)}")
            return 0 if witnesses and not issues else 1
        alive_tv = binary_path(args.alive_tv, "alive-tv")
        llvm_reduce = binary_path(args.llvm_reduce, "llvm-reduce")
        if not witnesses:
            raise ValueError("no usable witnesses; inspect --plan and extraction issues")
        report_path = args.out / "report.json"
        if report_path.exists():
            raise ValueError(f"{report_path} already exists; use a new --out to preserve the earlier run")
        report = {
            "schema": "saved-witness-comparison-v1", "corpus_note": corpus_note,
            "started_utc": datetime.now(timezone.utc).isoformat(),
            "host": {"platform": platform.platform(), "machine": platform.machine(),
                     "python": platform.python_version(), "logical_cpus": os.cpu_count()},
            "source": source_info, "initial_only": True,
            "selection": {"bugs": args.bug, "rule": "one initial raw pair per bug; all raw copies must agree"},
            "record_sha256": {path: digest(blob) for path, blob in sorted(records.items())},
            "implementation_sha256": source_identity(),
            "binaries": {"alive_tv": binary_identity(alive_tv), "llvm_reduce": binary_identity(llvm_reduce)},
            "settings": {"methods": list(methods), "candidate_budget": args.budget,
                         "oracle_strictness": "error_class", "alive_tv_timeout": args.timeout,
                         "llvmreduce_subprocess_timeout": DEFAULT_SUBPROCESS_TIMEOUT,
                         "equal_wall_budget": False,
                         "ablation": "iraware-no-promotion disables operand promotion only; other IR-aware passes remain enabled",
                         "interpretation": "same function/error class is not proof of the same semantic root cause or minimality; one timing run per method/case",
                         "cost_policy": "shared establishment once per case; candidate search, pipeline refresh, and independent postcheck separately counted"},
            "extraction_issues": issues, "cases": [], "complete": False,
        }
        if args.hybrid_prefix_calls is not None:
            report["settings"]["hybrid_prefix_calls"] = args.hybrid_prefix_calls
        args.out.mkdir(parents=True, exist_ok=True)
        write_report(report_path, report)
        with llvm_reduce_override(llvm_reduce):
            for witness in witnesses:
                print(f"Comparing {witness.bug_id} ...", flush=True)
                case = compare_witness(witness, alive_tv=alive_tv, budget=args.budget,
                                       timeout=args.timeout, hybrid_prefix_calls=args.hybrid_prefix_calls)
                report["cases"].append(case)
                write_report(report_path, report)
                for row in case["methods"]:
                    size = row.get("displayed_size", {})
                    print(f"  {row['method']}: {row['status']}; instructions={size.get('instructions', '?')}; "
                          f"calls={row.get('candidate_calls', '?')}+{row.get('refresh_calls', '?')}+"
                          f"{row.get('postcheck_calls', '?')}; seconds={row.get('total_seconds', '?')}", flush=True)
        statuses = Counter(row["status"] for case in report["cases"] for row in case["methods"])
        report.update(complete=True, completed_utc=datetime.now(timezone.utc).isoformat(),
                      method_status_counts=dict(statuses))
        write_report(report_path, report)
        print(f"Saved {report_path}; statuses: {dict(statuses)}")
        return 0 if not issues and not any(statuses[s] for s in ("failed", "error", "skipped")) else 1
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
