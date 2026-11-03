#!/usr/bin/env python3
"""Pair saved repair patches with their own diagnostics; no builds or API calls."""
from __future__ import annotations

import argparse
import difflib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ce.experiment import ExperimentMismatch  # noqa: E402
from ce.patching import PatchFormatError, apply_edits  # noqa: E402
from scripts.audit_sweep import diagnostic, feedback_after_replies, read_records  # noqa: E402
from scripts.inspect_patches import conversation, proposed_source  # noqa: E402


def diagnose_run(path: Path, run: dict) -> dict:
    hunk, replies = conversation(run)
    logs = feedback_after_replies(run)
    iterations = run.get("iterations") or []
    config = run.get("notes", {}).get("experiment_config") or {}
    explicit = config.get("patch_format") == "search-replace-v1" or config.get("harness_version") == "repair-v3"
    aligned = len(replies) == len(iterations)
    rows = []
    for index, iteration in enumerate(iterations):
        status = iteration.get("patch")
        reply = replies[index] if aligned else None
        log = logs[index] if aligned and index < len(logs) else ""
        validation = iteration.get("validation") or {}
        replay_error, patch_diff = None, ""
        if hunk is not None and reply is not None:
            try:
                if explicit:
                    source = apply_edits(hunk, reply)
                else:
                    source, replay_error = proposed_source(run, hunk, index, reply)
                # A parseable prefix of a truncated/rejected reply is not an
                # applied candidate. Preserve its text and recorded status.
                if status == "applied" and source is not None:
                    patch_diff = "\n".join(difflib.unified_diff(
                        hunk.splitlines(), source.splitlines(), fromfile="original-excerpt",
                        tofile=f"turn-{index + 1}", lineterm="", n=3))
            except PatchFormatError as error:
                replay_error = str(error)
        elif hunk is None:
            replay_error = "Original source excerpt is absent from the saved conversation."
        else:
            replay_error = "Reply and iteration counts differ; no unique turn alignment is established."
        category, signature = diagnostic(log) if validation.get("stage") == "build" else (None, None)
        rows.append({"turn": index + 1, "patch": status, "validation": validation,
                     "finish_reason": iteration.get("llm", {}).get("finish_reason"),
                     "replay_error": replay_error, "patch_diff": patch_diff,
                     "reply": reply, "returned_feedback": log, "feedback_available": bool(log),
                     "regression_tests": sorted(set(re.findall(
                         r"^\s*(?:FAIL|UNRESOLVED):\s*(.+)$", log, re.MULTILINE)))
                         if validation.get("stage") == "regression" else [],
                     "build_diagnostic_category": category, "build_diagnostic_signature": signature})
    return {"file": path.name, "bug_id": str(run["bug_id"]), "condition": run["condition"],
            "trial": run.get("trial", 1), "model": run.get("model"),
            "fixed": bool(run.get("totals", {}).get("fixed")), "original_excerpt": hunk,
            "reply_iteration_count_match": aligned,
            "unaligned_saved_replies": replies if not aligned else [],
            "unaligned_saved_feedback": logs if not aligned else [], "turns": rows}


def excerpt(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    lines = value.splitlines(keepends=True)
    selected = set(range(min(3, len(lines))))
    for index, line in enumerate(lines):
        if re.search(r"FAIL:|UNRESOLVED:|error:|timed out|killed|FileCheck|LLVM ERROR:", line, re.IGNORECASE):
            selected.update(range(max(0, index - 2), min(len(lines), index + 12)))
    selected.update(range(max(0, len(lines) - 3), len(lines)))
    compact = "".join(line if index in selected else "" for index, line in enumerate(lines))
    return compact[:limit] + "\n[Console excerpt shortened; --json retains the complete saved text.]"


def print_report(report: dict, limit: int) -> None:
    print("Saved-turn diagnostics only: no new validation, builds or model requests.")
    print("Inspect the patch and feedback from the SAME turn; final certificate flags can be sticky.")
    for run in report["runs"]:
        print(f"\n=== {run['file']} | {run['model']} | trial {run['trial']} ===")
        if not run["reply_iteration_count_match"]:
            print("WARNING: reply and iteration counts differ; inspect the full saved conversation.")
        for turn in run["turns"]:
            validation = turn["validation"]
            stage, kind = validation.get("stage", "unknown"), validation.get("failure_kind")
            passed = validation.get("passed")
            print(f"turn {turn['turn']}: {turn['patch']}; {stage}; passed={passed}; reason={kind}")
            interesting = ((stage in ("regression", "build") and passed is not True)
                           or turn["patch"] in ("invalid_format", "truncated", "mismatch"))
            if not interesting:
                continue
            if turn["replay_error"]:
                print("  Edit replay: " + turn["replay_error"])
            if turn["regression_tests"]:
                print("  Failed tests: " + "; ".join(turn["regression_tests"]))
            if turn["patch_diff"]:
                print("  Applied patch for THIS turn:\n" + excerpt(turn["patch_diff"], limit))
            elif turn["patch"] in ("invalid_format", "truncated", "mismatch"):
                print("  Rejected model reply:\n" + excerpt(turn["reply"] or "<missing>", limit))
            print("  Returned feedback for THIS turn:\n" +
                  (excerpt(turn["returned_feedback"], limit) if turn["feedback_available"]
                   else "<not saved; do not substitute another turn's diagnostics>"))
    if report["unreadable_files"]:
        print("Unreadable records: " + ", ".join(report["unreadable_files"]))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory")
    parser.add_argument("--bug", help="optional exact bug ID")
    parser.add_argument("--model")
    parser.add_argument("--max-chars", type=int, default=2500, help="console limit per patch/reply/feedback; JSON is complete")
    parser.add_argument("--json", action="store_true", help="export complete saved replies, diffs and feedback")
    args = parser.parse_args(argv)
    if args.max_chars < 100:
        parser.error("--max-chars must be at least 100")
    try:
        records, unreadable = read_records(args.directory, args.model)
    except (ExperimentMismatch, OSError, ValueError) as error:
        parser.error(str(error))
    report = {"runs": [diagnose_run(path, run) for path, run in records
                       if args.bug is None or str(run["bug_id"]) == args.bug],
              "unreadable_files": unreadable}
    if not report["runs"]:
        print("No selected repair records found.", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_report(report, args.max_chars)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
