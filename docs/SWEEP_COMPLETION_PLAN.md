# Completing the Qwen study by 28 October 2026

> Superseded on 2026-10-22 by [H100_V3.md](H100_V3.md). Inspection of the
> uploaded 205-run snapshot confirmed patch-handling and verifier-context
> faults. Pause v2 at a completed-cell boundary and preserve its records;
> do not launch the old full sweep from this historical plan.

This plan is dated 21 October 2026. It is based on an operator-provided
snapshot summary, not on inspection of the current H100 run records. No
outcome table or positive method claim is finalized by this document.

## Current evidence and the next decision

The reported snapshot contains 198 of 648 main run cells, ten harness-passing
runs on three distinct bugs, and 448 failed builds out of 702 attempted builds
(63.8%). Eighty of 782 replies (10.2%) were unchanged. Thirty runs passed a
reproducer at some turn, of which twenty ultimately failed the selected
regression checks. Those twenty runs are not full repairs.

Assuming uninterrupted bug-major order, 198 records correspond to seven
complete 27-cell bugs and the first nine cells of the eighth. The existing
triage counts bugs seen, not fully completed three-trial comparisons. Only
the raw snapshot can confirm actual coverage and interrupted cells.

With at most three repaired bugs, a current paired comparison can have at
most three discordant bugs. Even three wins and no losses give an exact
one-sided p-value of 0.125. Ten passing runs do not supply ten independent
bugs. This limits present inference; it does not predict the remaining bugs.

Preserve the active configuration while obtaining the snapshot. Do not
change prompts, reducers, iteration budgets, serving versions or model
weights in this directory. Do not overwrite completed cells. The accepted-
pass no-op proxy is three bugs at the checkpoint's judgment-call value:
continue only long enough to collect the current bug and the saved evidence,
then make a documented decision by about ten bugs. Four unreduced bugs
trigger the registered pause-and-inspect checkpoint. A tiny witness can
legitimately be irreducible; no-op counts alone do not diagnose broken code.

## Share the raw snapshot now

In another shell on the H100, the following copies completed atomic JSON
records into Git without resetting or building LLVM:

```bash
git add -f results-qwen3-v2/*.json results-qwen3-v2/.experiment.json
git commit -m "Snapshot partial Qwen3 v2 sweep for audit"
git push
```

Also share `validation-checks/` and `results-qwen3-v2-pilot/` if they exist.
They are ignored by default, so adding them requires `git add -f`. Do not
include credentials or the vLLM environment file. Raw conversations and
per-turn validation are necessary: aggregate triage cannot identify compiler
diagnostics, patch quality, unique fixes, or treatment differences.

After receiving updated analysis code, these commands read records only and
can run alongside the sweep:

```bash
python3 scripts/audit_sweep.py results-qwen3-v2 --repeat 3
python3 scripts/audit_sweep.py results-qwen3-v2 --repeat 3 --json > sweep-audit.json
python3 examples/summarize_results.py results-qwen3-v2 --repeat 3 --json > sweep-summary.json
python3 examples/analyze_significance.py results-qwen3-v2 --repeat 3 --json > sweep-comparisons.json
```

Interim p-values are diagnostic, not final evidence or a stopping rule.
The first summary's "all" table may have unequal trials; the strict paired
table requires all nine conditions and all three completed trials on each
included bug. Use `--repeat 1` only for the actual one-trial development pilot.

Record host provenance separately:

```bash
git rev-parse HEAD
git status --short --untracked-files=no
nvidia-smi --query-gpu=name,memory.total --format=csv
ccache -s
python3 - <<'PY'
import importlib.metadata as md
import os, platform
print("Python:", platform.python_version())
for name in ("vllm", "torch", "openai"):
    try:
        print(name + ":", md.version(name))
    except md.PackageNotFoundError:
        print(name + ": not installed in this shell's environment")
for key in ("LAB_LLM_MODEL", "LAB_LLM_TEMP", "LAB_LLM_BASEMODEL_CUTOFF"):
    print(key + ":", os.environ.get(key, "<unset>"))
PY
```

Capture the inference-library versions from the vLLM environment as well;
the runner's virtual environment may not contain them. Preserve the exact
model checkpoint cache revision, launch command, effective decoding defaults,
Alive2 and llvm-reduce versions, CPU/memory allocations, and LLVM CMake cache.
None of those is fully pinned by the model alias or prompt hash.

## What the audit must resolve

1. **Compilation failures.** Separate C++ diagnostics such as undeclared
   identifiers, duplicate definitions and malformed syntax from killed
   compiler processes, disk exhaustion, missing tools and timeouts. The
   `build: error` label does not make this distinction. Inspect complete logs
   and representative replies before blaming FP8 or switching models.
2. **Reproducer failures.** `reproducer: error` also covers ordinary Alive2
   refinement violations. Separate those from verifier crashes, unsupported
   cases, timeouts and invalid generated IR.
3. **Regression near-fixes.** Recover the exact turn that passed the
   reproducer. Its reply may differ from the final stored patch. Classify
   actual FileCheck/assertion failures separately from missing infrastructure.
4. **Actual reduction.** Compare `original_*` and `shown_*` metrics and
   fallback errors on feedback consumed by the model, including iteration
   zero. `passes_applied` and attempted `size_after` can misrepresent a
   re-verification fallback. Report instructions and characters separately.
5. **Treatment limitation.** In current source, per-test Alive2 arguments
   are omitted during reduction/refresh for five sampled bugs: `126012`,
   `114879`, `91369`, `180870`, `91957`. Validation uses those arguments.
   Confirm the actual H100 code before drawing conclusions. A plumbing fix
   changes treatment semantics and requires separate versioned records and
   a dated amendment, not selective replacement of unfavorable v2 outcomes.

The fact that all three currently repaired bugs appear on the no-reduction
list does not establish a reduction advantage. Structured presentation can
still differ, and the unreduced cases may be legitimate. No conclusion about
the method follows without the condition-by-bug matrix.

## One-week work schedule

| Window | Required deliverable |
|---|---|
| 21–22 Oct | Raw snapshot, strict coverage matrix, diagnostic groups, exact provenance, and documented checkpoint decision. |
| 22–25 Oct | Finish the frozen main sample if diagnostics support it and actual throughput fits; otherwise report the incomplete planned coverage and reason. Any revised method is a separately labelled follow-up. |
| 25–26 Oct | Review every distinct passing patch, recover reproducer-only cases, and verify representative patches independently after the active runner is stopped or in an isolated LLVM checkout/build. |
| 26–27 Oct | Final matched outcome and reducer tables, fixed-family statistics, limitations, and measured case studies. Report any missing optional ablation. |
| 27–28 Oct | Incorporate verified methods and outcomes into the editable paper, check every number against saved analysis outputs, compile and review the PDF. |

There are 450 cells remaining at the quoted snapshot, not a known number of
hours. Saved wall durations permit a conditional workload estimate, not a
reliable deadline prediction across new LLVM revisions. Source-revision
changes can require much larger builds than later cells on the same bug.
Do not run concurrent repair workers against the same LLVM checkout or build.

## Final reporting

```bash
python3 examples/summarize_results.py results-qwen3-v2 --repeat 3 --json > final-summary.json
python3 examples/analyze_significance.py results-qwen3-v2 --repeat 3 --final --json > final-comparisons.json
```

`--final` refuses a main analysis until every planned bug has complete matched
three-trial cells under all nine conditions. It also keeps extra diagnostic
bugs out of the planned sample. A nonreproducing or inaccessible bug must be
documented in a dated exclusion/amendment ledger; do not edit the planned
sample silently to bypass this check.

The four registered primary comparisons and their directions remain fixed.
Benjamini–Hochberg uses the full family. Report all four paired outcome
counts and rate differences regardless of p-values. Exploratory contrasts
remain exploratory. Do not add trials or stop collection because a p-value
crosses a threshold, count correlated trials as independent bugs, choose a
different success criterion, or omit adverse runs to obtain significance.

Novelty is the specific tandem, verifier-guided reduction and deterministic
presentation method and the question tested. Evidence for improvement must
come from measured repair success or a clearly specified cost/size outcome,
with tradeoffs and validation limits stated. Until that evidence exists, the
paper describes an implemented method and an empirical study, not an
established repair improvement. Use `METHODOLOGY_CURRENT_DRAFT.md` as the
current-method source pending verification of the H100 snapshot and the
editable LaTeX source.
