#!/usr/bin/env python3
"""Generate reproducible diagnostic tables from a frozen Git sweep snapshot.

Reads the committed snapshot, including its manifest and sample, with git archive.
No model, LLVM build, or live verifier is invoked. Reference fixes are read only
for offline review; they are never supplied to a model. The tables describe an
incomplete v2 experiment whose repair protocol has known parsing/scope defects.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import io
import json
from pathlib import Path
import re
import statistics
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ce.analysis import select_complete_runs  # noqa: E402
from ce.feedback import CONDITIONS  # noqa: E402
from examples.analyze_significance import PRIMARY, benjamini_hochberg, mcnemar_exact  # noqa: E402
from scripts.audit_sweep import diagnostic, feedback_after_replies  # noqa: E402
from scripts.inspect_patches import conversation  # noqa: E402


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(ROOT), *args])


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def historical_v2_extract(reply: str) -> str:
    """Freeze the v2 parser, so later runner repairs cannot reinterpret v2 data."""
    if reply.startswith("```"):
        return reply.strip().removeprefix("```cpp").removeprefix("```").removesuffix("```")
    for pattern in (r"```cpp([\s\S]+)```", r"```([\s\S]+)```"):
        matches = re.findall(pattern, reply)
        if matches:
            return matches[-1]
    return reply


def braces(text: str) -> dict:
    depth = lowest = 0
    for line in text.splitlines():
        line = re.sub(r"//.*", "", line)
        line = re.sub(r'"(?:\\.|[^"\\])*"', '""', line)
        line = re.sub(r"'(?:\\.|[^'\\])*'", "''", line)
        depth += line.count("{") - line.count("}")
        lowest = min(depth, lowest)
    return {"net": depth, "minimum": lowest,
            "caveat": "Lexical heuristic only; balanced braces do not imply a complete C++ declaration or function."}


def error_signature(text: str) -> str:
    # A linker's final collect2 error obscures the causal undefined symbol.
    for line in text.splitlines():
        if "undefined reference" in line or "multiple definition" in line:
            return line[line.find("undefined reference"):] if "undefined reference" in line else line.strip()
    return diagnostic(text)[1]


def median(values):
    return statistics.median(values) if values else None


def snapshot_report(revision: str, results: str) -> dict:
    revision = git("rev-parse", f"{revision}^{{commit}}").decode().strip()
    paths = [results, "data/experiment_sample.json", "examples/repair_experiment.py",
             "llvm-apr-benchmark/dataset"]
    archive = git("archive", "--format=tar", revision, "--", *paths)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        blobs = {member.name: tar.extractfile(member).read() for member in tar.getmembers() if member.isfile()}
    record_blobs = {path: body for path, body in blobs.items()
                    if path.startswith(results.rstrip("/") + "/") and path.endswith(".json")
                    and not Path(path).name.startswith(".")}
    records = []
    for path, data in sorted(record_blobs.items()):
        run = json.loads(data)
        if isinstance(run, dict) and "bug_id" in run:
            run["_snapshot_file"] = path
            records.append(run)
    sample_blob = blobs["data/experiment_sample.json"]
    sample = json.loads(sample_blob)
    sample_ids = [str(row["bug_id"]) for tier in sample["tiers"].values() for row in tier]
    selected, coverage = select_complete_runs(records, repeats=3, conditions=tuple(CONDITIONS))
    common = set(coverage["complete_bugs"])
    seen = [bug for bug in sample_ids if any(str(run["bug_id"]) == bug for run in records)]
    per_condition = {}
    for condition in CONDITIONS:
        all_records = [run for run in records if run["condition"] == condition]
        matched = [run for run in selected if run["condition"] == condition]
        fixed_bugs = sorted({str(run["bug_id"]) for run in matched if run["totals"]["fixed"]})
        per_condition[condition] = {
            "all_records": len(all_records),
            "all_fixed_records": sum(bool(run["totals"]["fixed"]) for run in all_records),
            "matched_records": len(matched), "matched_bugs": len(common),
            "matched_fixed_records": sum(bool(run["totals"]["fixed"]) for run in matched),
            "matched_fixed_bugs": fixed_bugs,
        }
    outcomes = {condition: {bug: any(str(run["bug_id"]) == bug and run["condition"] == condition
                                   and run["totals"]["fixed"] for run in selected)
                            for bug in common} for condition in CONDITIONS}
    comparisons = []
    for better, worse, question in PRIMARY:
        counts = Counter((outcomes[better][bug], outcomes[worse][bug]) for bug in common)
        one, two = mcnemar_exact(counts[(True, False)], counts[(False, True)])
        comparisons.append({"better": better, "worse": worse, "question": question, "n": len(common),
                            "both": counts[(True, True)], "only_better": counts[(True, False)],
                            "only_worse": counts[(False, True)], "neither": counts[(False, False)],
                            "p_one_sided": one, "p_two_sided": two})
    for row, adjusted in zip(comparisons, benjamini_hochberg([row["p_one_sided"] for row in comparisons])):
        row["p_bh"] = adjusted
    by_bug = {bug: {"records": 0, "turns": 0, "stages": Counter(), "first_build_errors": Counter(),
                    "fence_contaminated_proposals": 0, "fence_build_failures": 0,
                    "undefined_reference_build_failures": 0, "missing_member_build_failures": 0,
                    "reply_window_line_ratios": [], "passing_files": []} for bug in seen}
    first_error_counts = Counter()
    passing = []
    stages = Counter()
    exposures = Counter()
    reductions = defaultdict(dict)
    prompt_groups = defaultdict(lambda: defaultdict(list))
    bug_wall_seconds = Counter()
    total_wall = prompt_tokens = completion_tokens = oracle_calls = 0
    for run in records:
        bug = str(run["bug_id"])
        row = by_bug[bug]
        row["records"] += 1
        file = run["_snapshot_file"]
        messages = ((run.get("certificate") or {}).get("log") or {}).get("messages", [])
        if run.get("trial", 1) == 1 and len(messages) >= 2:
            payload = json.dumps(messages[:2], ensure_ascii=False, sort_keys=True).encode()
            prompt_groups[bug][sha256(payload)].append(run["condition"])
        hunk, replies = conversation(run)
        logs = feedback_after_replies(run)
        if hunk:
            row["window_lines"] = len(hunk.splitlines())
            row["window_first_line"] = hunk.splitlines()[0]
            row["window_last_line"] = hunk.splitlines()[-1]
            row["window_braces"] = braces(hunk)
        for index, iteration in enumerate(run["iterations"]):
            row["turns"] += 1
            stage = iteration.get("validation", {}).get("stage", "unknown")
            fixed = iteration.get("fixed", False)
            outcome = "fixed" if fixed else stage
            row["stages"][outcome] += 1
            stages[outcome] += 1
            fb = iteration.get("feedback") or {}
            phase = "initial" if index == 0 else "subsequent"
            exposures[f"{phase}_turns"] += 1
            exposures[f"{phase}_alive2_routed"] += bool(fb.get("counterexample"))
            exposures[f"{phase}_witness_eligible"] += bool(fb.get("counterexample")) and run["condition"] != "baseline" and not fb.get("error") and not fb.get("verified")
            reply_code = historical_v2_extract(replies[index]) if index < len(replies) else ""
            contaminated = bool(re.search(r"^\s*```", reply_code, re.MULTILINE))
            row["fence_contaminated_proposals"] += contaminated
            if hunk:
                row["reply_window_line_ratios"].append(len(reply_code.splitlines()) / len(hunk.splitlines()))
            log = logs[index] if index < len(logs) else ""
            if stage == "build":
                signature = error_signature(log)
                first_error_counts[signature] += 1
                row["first_build_errors"][signature] += 1
                row["fence_build_failures"] += contaminated and "stray" in log and "`" in log
                row["undefined_reference_build_failures"] += "undefined reference" in log
                row["missing_member_build_failures"] += "no member named" in log or "is not a member of" in log
            if index == 0 and run.get("trial", 1) == 1 and run["condition"].endswith("-plain") and fb.get("strategy"):
                reductions[bug][run["condition"].split("-", 1)[0]] = {
                    key: fb.get(key) for key in ("original_instructions", "shown_instructions", "original_chars",
                                                "shown_chars", "oracle_calls", "oracle_tool_failures",
                                                "passes_applied", "error")}
        totals = run["totals"]
        total_wall += totals.get("wall_seconds", 0)
        bug_wall_seconds[bug] += totals.get("wall_seconds", 0)
        prompt_tokens += totals.get("llm_prompt_tokens", 0)
        completion_tokens += totals.get("llm_completion_tokens", 0)
        oracle_calls += totals.get("oracle_calls", 0)
        if totals["fixed"]:
            patch = (run.get("certificate") or {}).get("patch", "")
            dataset = json.loads(blobs[f"llvm-apr-benchmark/dataset/{bug}.json"])
            passing.append({"file": file, "bug": bug, "condition": run["condition"], "trial": run.get("trial", 1),
                            "matched_panel": bug in common, "patch_sha256": sha256(patch.encode()),
                            "reference_patch_sha256": sha256(dataset["patch"].encode()),
                            "patch": patch,
                            "status": "harness-passing; expert correctness review and additional semantic tests pending"})
            row["passing_files"].append(file)
    for row in by_bug.values():
        ratios = row.pop("reply_window_line_ratios")
        row["reply_window_line_ratio"] = {"min": min(ratios) if ratios else None,
                                          "median": median(ratios), "max": max(ratios) if ratios else None}
        row["first_build_errors"] = [{"signature": error, "turns": count}
                                     for error, count in row["first_build_errors"].most_common()]
    manifest_blob = blobs[results.rstrip("/") + "/.experiment.json"]
    return {
        "provenance": {"snapshot_commit": revision,
                       "snapshot_committed_at": git("show", "-s", "--format=%cI", revision).decode().strip(),
                       "results_directory": results, "manifest_sha256": sha256(manifest_blob),
                       "manifest": json.loads(manifest_blob), "sample_sha256": sha256(sample_blob),
                       "runner_sha256": sha256(blobs["examples/repair_experiment.py"]),
                       "generator_sha256": sha256(Path(__file__).read_bytes()),
                       "records": {path: sha256(body) for path, body in sorted(record_blobs.items())}},
        "status": "Incomplete diagnostic v2 snapshot; known parser/window confounds; not confirmatory evidence of method effectiveness.",
        "all_records": len(records), "observed_bugs": seen,
        "all_fixed_records": len(passing), "all_fixed_bugs": sorted({row["bug"] for row in passing}),
        "matched_records": len(selected), "matched_bugs": [bug for bug in sample_ids if bug in common],
        "matched_fixed_records": sum(row["matched_panel"] for row in passing),
        "matched_fixed_bugs": sorted({row["bug"] for row in passing if row["matched_panel"]}),
        "coverage": coverage, "per_condition": per_condition, "primary_comparisons_descriptive": comparisons,
        "stages": dict(stages), "feedback_routing": dict(exposures),
        "by_bug": by_bug, "initial_reductions_first_trial_plain": dict(reductions),
        "identical_initial_prompt_groups_trial_one": {
            bug: [{"prompt_sha256": digest, "conditions": conditions}
                  for digest, conditions in groups.items() if len(conditions) > 1]
            for bug, groups in prompt_groups.items()},
        "top_build_errors": [{"signature": error, "turns": count} for error, count in first_error_counts.most_common()],
        "passing_patches_for_offline_review": passing,
        "recorded_cost": {"wall_seconds_sum": round(total_wall, 3), "llm_prompt_tokens": prompt_tokens,
                          "llm_completion_tokens": completion_tokens, "oracle_calls": oracle_calls,
                          "by_bug_wall_seconds": {bug: round(seconds, 3) for bug, seconds in bug_wall_seconds.items()},
                          "dollars": None, "eta": None,
                          "caveat": "Durations include build/check/reduction/generation; no wall-clock throughput or API-equivalent dollar cost inferred."},
    }


def tex_escape(text: str) -> str:
    return str(text).replace("_", r"\_").replace("%", r"\%").replace("&", r"\&").replace("#", r"\#")


def review_notes(report: dict) -> str:
    """Case-specific offline source observations, explicitly not expert verdicts."""
    lines = ["# Frozen v2 diagnostic and passing-patch review", "",
             f"Snapshot: `{report['provenance']['snapshot_commit']}`.", "",
             "This is an offline source-inspection audit, not an independent human expert review or a new semantic validation run. "
             "Reference patches were inspected only after generation and were not sent to a model. All ten saved passing patches remain harness-plausible, with correctness review pending.", ""]
    if report["provenance"]["snapshot_commit"].startswith("73f2ba1"):
        lines.extend([
            "The seven passing records for bug 89390 fall into three guard families:", "",
            "- IR-aware plain trial 2 and all three raw structured trials skip signed-remainder folding when the shuffle mask has poison. "
            "The reference handles all integer division/remainder opcodes. The narrower model guard needs additional opcode-generalization tests.",
            "- LLVM-reduce plain trial 1 and LLVM-reduce structured trial 2 skip all integer division/remainder folding, irrespective of the mask. "
            "This is broader optimization suppression than the reference; passing tests do not measure the lost optimization opportunities.",
            "- LLVM-reduce plain trial 2 skips signed/unsigned remainder folding for vector operands. "
            "This also suppresses folding beyond the poison-mask condition and needs review.", "",
            "Both passing records for bug 135182 add a self-addition-specific guard. The reference guards the relevant same-operand widening case more generally. "
            "The proposals may repair the listed reproducer while leaving related cases uncovered; no additional semantic check has been run here.", "",
            "The passing generic plain record for bug 126012 adds an identity-based nonrecursive-reasoning guard. "
            "The reference checks whether an incoming value has computable evolution in the loop. "
            "These are different mechanisms; reproducer and selected lit success do not establish their equivalence.", "",
            "No passing record is promoted to expert-correct by this audit. No record is silently relabelled as incorrect solely because it differs from the reference.", "",
        ])
    lines.extend(["Recorded resource totals (all saved runs):", "",
                  f"- Wall-duration sum: {report['recorded_cost']['wall_seconds_sum'] / 3600:.2f} hours.",
                  f"- Model input/output tokens: {report['recorded_cost']['llm_prompt_tokens']:,} / {report['recorded_cost']['llm_completion_tokens']:,}.",
                  f"- Recorded oracle calls: {report['recorded_cost']['oracle_calls']:,.0f}.", "",
                  "The duration sum is not a timestamp-based throughput measurement or an ETA. "
                  "No API-equivalent dollar cost is inferred for locally served Qwen.", ""])
    return "\n".join(lines)


def tex_tables(report: dict) -> str:
    n_bugs = len(report["matched_bugs"])
    lines = ["% Generated by scripts/report_snapshot.py; edit the generator rather than these values.",
             f"% Snapshot {report['provenance']['snapshot_commit']}",
             r"\begin{table}[t]", r"\centering\scriptsize",
             r"\caption{Diagnostic v2 outcomes on " + str(n_bugs) + r" fully matched bugs (three trials each). These are harness-passing outcomes, not expert-certified repairs. Incomplete bugs are excluded.}",
             r"\label{tab:v2-matched}", r"\begin{tabular}{@{}lrr@{}}\toprule",
             f"Condition & Bugs / {n_bugs} & Runs / {3 * n_bugs} " + r"\\ \midrule"]
    for condition, row in report["per_condition"].items():
        lines.append(f"\\texttt{{{tex_escape(condition)}}} & {len(row['matched_fixed_bugs'])} & {row['matched_fixed_records']} \\")
    lines.extend([r"\bottomrule\end{tabular}", r"\end{table}",
                  r"\begin{table}[t]", r"\centering\scriptsize",
                  r"\caption{Descriptive paired counts for the four prespecified comparisons on " + str(n_bugs) + r" complete bugs. $b$ favors the hypothesized direction, $c$ opposes it. P1: IR-aware vs generic; P2: IR-aware vs llvm-reduce (both plain); P3: IR-aware structured vs plain; P4: raw plain vs baseline. This interim snapshot is not a significance-based stopping analysis.}",
                  r"\label{tab:v2-pairs}", r"\begin{tabular}{@{}lrrrrr@{}}\toprule",
                  r"Pair & Both & $b$ & $c$ & Neither & $p_{\rm BH}$ \\ \midrule"])
    for index, row in enumerate(report["primary_comparisons_descriptive"], 1):
        lines.append(f"P{index} & {row['both']} & {row['only_better']} & {row['only_worse']} & {row['neither']} & {row['p_bh']:.3f} \\")
    turns = sum(report["stages"].values())
    builds = turns - report["stages"].get("no_patch", 0)
    lines.extend([r"\bottomrule\end{tabular}", r"\end{table}",
                  r"\begin{table}[t]", r"\centering\scriptsize",
                  r"\caption{Turn diagnostics over all " + str(report["all_records"]) + r" saved runs, including incomplete bugs. Categories are mutually exclusive validation endpoints; parser and linker symptoms are diagnostic subsets, not model-correctness judgments.}",
                  r"\label{tab:v2-diagnostics}", r"\begin{tabular}{@{}lr@{}}\toprule",
                  r"Measure & Count \\ \midrule", f"Model turns & {turns} \\",
                  f"Applied proposals reaching a build & {builds} \\",
                  f"Build-stage failures & {report['stages'].get('build', 0)} \\",
                  f"Reproducer-stage failures & {report['stages'].get('reproducer', 0)} \\",
                  f"Regression-stage failures & {report['stages'].get('regression', 0)} \\",
                  f"Full-validation passes & {report['stages'].get('fixed', 0)} \\",
                  f"Unchanged proposals & {report['stages'].get('no_patch', 0)} \\",
                  f"Build failures with fence contamination & {sum(row['fence_build_failures'] for row in report['by_bug'].values())} \\",
                  f"Build failures with undefined symbols & {sum(row['undefined_reference_build_failures'] for row in report['by_bug'].values())} \\",
                  r"\bottomrule\end{tabular}", r"\end{table}",
                  r"\begin{table}[t]", r"\centering\scriptsize",
                  r"\caption{Initial witness instruction counts from trial 1 of each plain condition, over " + str(len(report["observed_bugs"])) + r" observed bugs. $I_0$: original; G: generic; L: llvm-reduce; A: IR-aware. Counts alone do not establish preservation of the original bug or repair usefulness.}",
                  r"\label{tab:v2-reducers}", r"\begin{tabular}{@{}lrrrr@{}}\toprule",
                  r"Bug & $I_0$ & G & L & A \\ \midrule"])
    for bug in report["observed_bugs"]:
        reductions = report["initial_reductions_first_trial_plain"].get(bug, {})
        first = next(iter(reductions.values()), {})
        values = [first.get("original_instructions")]
        values.extend(reductions.get(strategy, {}).get("shown_instructions") for strategy in ("generic", "llvmreduce", "iraware"))
        lines.append(bug + " & " + " & ".join(str(value) if value is not None else "--" for value in values) + r" \\")
    lines.extend([r"\bottomrule\end{tabular}", r"\end{table}"])
    # Generated row literals must end in TeX's two-backslash separator.
    lines = [line + "\\" if line.endswith("\\") and not line.endswith("\\\\") else line for line in lines]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--snapshot", required=True, help="frozen Git commit containing the result records")
    parser.add_argument("--results", default="results-qwen3-v2")
    parser.add_argument("--out", default="our_paper/generated")
    args = parser.parse_args(argv)
    report = snapshot_report(args.snapshot, args.results)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "v2_snapshot.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (out / "v2_snapshot_tables.tex").write_text(tex_tables(report), encoding="utf-8")
    (out / "v2_patch_review.md").write_text(review_notes(report), encoding="utf-8")
    print(f"Saved {report['all_records']}-record snapshot; {len(report['matched_bugs'])} matched bugs; "
          f"{report['matched_fixed_records']} matched harness passes. Tables are diagnostic only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
