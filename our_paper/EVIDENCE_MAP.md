# Evidence map for the interim manuscript

Updated 2026-10-22. Paths are relative to this repository. The evaluated method
is `repair-v2`, with code anchored at `38b381b`; numerical results are frozen at
`73f2ba151742c44cf88878b50c027ef7f06293c6`. The working tree may contain v3
changes, which are not evidence of an evaluated v2 behavior or repair outcome.

## A. Manuscript and implementation evidence

| Manuscript claim | Evidence | Scope / limitation |
|---|---|---|
| 491 issue files, 142 miscompilations, 135 Alive2 cases, 100 single-function eligible, 99 sampling pool, 24 selected | `llvm-apr-benchmark/dataset/`; `scripts/select_bootstrap_bug.py`; `scripts/select_experiment_sample.py`; `data/experiment_sample.json` | Dataset-specific counts, not all LLVM issues |
| Deterministic three size tiers, eight per tier, component round-robin | Selection script and committed sample | Size tiers are not measured model difficulty |
| Initial counterexample plus later feedback in one conversation | v2 `repair()` in `examples/repair_experiment.py`; saved `certificate.log.messages` | `iterations[i].feedback` is input to proposal i, not that proposal's output |
| V2 balanced-window heuristic, full-window replacement, no-op guard | v2 `bug_hunk`, `_balance_braces`, `extract_code`, `is_no_op`, `apply_patch` | Bounded heuristic; partial functions and response-format failures remain |
| Four replies, temperature 0.8, 400-call reducer budget, error-class strictness | `results-qwen3-v2/.experiment.json`; saved record notes | Alias and prompt hash do not identify checkpoint/server/source fully |
| Qwen identity, FP8, H100, vLLM 0.28.0, context 32768, eager, memory 0.55, sampler flag | Operator's server invocation and pasted startup log | Reported settings; exact checkpoint and complete host environment pending |
| Fresh conversations, reset to base before every proposal, code relative to original window | v2 runner and `lab_env.Environment.reset` | Does not mean a reference patch is shown to the model |
| Atomic records and incomplete-cell resume | `ce/benchmark.py` `RunLog.write`, `record_is_complete`; runner preflight | Does not make malformed / absent records scientific failures |
| Nine conditions; no-counterexample sentence | v2 `ce/feedback.py` `CONDITIONS`, `build_feedback` | Baseline still receives issue title and localized source |
| Generic line-level reduction; LLVM-reduce source then target, exit-status oracle | `ce/reduce_generic.py`; `ce/reduce_llvmreduce.py`; `ce/_llvmreduce_test.py` | Different reducer mechanics; no claim of equal achieved compression |
| Tandem edits, dependency closure, report-derived seeds; eight passes, at most four rounds | `ce/reduce_iraware.py`; `ce/irmodel.py` | Not a full parser, valid-IR guarantee, or global-minimality proof |
| Same error class / function is the preservation rule | `ce/oracle.py` `Oracle._match` | Does not prove same root cause or execution |
| Plain vs structured changes both content and organization | `ce/structured.py` | Structured omits full per-value traces and adds derived sections |
| Trailing-newline-normalized round trips for 1462 reproducers | `scripts/check_ir_roundtrip.py`; earlier check recorded in draft | Identity-only check; not literal byte equality or edit validation |
| V2 missing per-test verifier arguments | v2 `normalize_feedback`, `build_feedback_from_check`, benchmark `alive2_check`; sample data | Five sampled IDs: 126012,114879,91369,180870,91957 |
| 400 reducer calls do not cover refresh or all benchmark verification | v2 `ce/feedback.py`; `ce/oracle.py`; reducers | Terminal unused feedback also omitted from consumed-feedback totals |
| Build, reproducer, then selected lit validation; 300-second lit cap | `lab_env.py` `check_full`; `llvm_helper.py` `build`, `alive2_check`, `verify_lit` | Harness plausibility, not full-compiler correctness |
| Certificate pass flags are sticky; final patch may differ from reproducer-passing turn | `lab_env.py` and saved conversations | Recover the corresponding turn before discussing near fixes |
| Primary family and directions, k=3, exact McNemar, BH | `docs/ANALYSIS_PLAN.md`; `examples/analyze_significance.py` | Repository-history record, not an external registration |
| Strict common three-trial coverage and frozen generated tables | `scripts/report_snapshot.py`; `scripts/audit_sweep.py`; `our_paper/generated/v2_snapshot_tables.tex` | Generator reads committed snapshot, not future working-tree data |

## B. Numerical snapshot evidence

| Value / statement | Evidence | Denominator |
|---|---|---|
| 205 runs, eight observed bugs, ten passing runs, three bugs with passes | Snapshot `73f2ba1`, `results-qwen3-v2/*.json` | All saved v2 records; incomplete eighth bug included |
| Seven complete bugs, 189 runs, nine passing runs across two bugs | Strict panel in generated table `tab:v2-matched` | Same seven bugs, three trials per condition |
| Raw structured: 2 bugs and 4 runs; IR-aware plain: 1 bug and 1 run; IR-aware structured: zero | `tab:v2-matched` | Bugs / 7 and runs / 21 respectively |
| Generic plain: 1 bug, 1 run in panel; two passing runs in all records | Strict filter vs all-record diagnostics | Extra passing bug126012 is not in panel |
| P1 b=c=1; P2 no discordance; P3 b=0,c=1; P4 no discordance; all BH p=1 | `tab:v2-pairs` | Seven complete bugs; interim descriptive statistics |
| 810 replies; 87 unchanged; 723 builds; 467 build failures; 205 reproducer failures; 41 lit failures; ten full passes | `tab:v2-diagnostics` and saved per-turn validation | All205 records, not just the strict panel |
| 71 fence-contamination symptoms; 124 undefined-symbol symptoms | `scripts/report_snapshot.py` and saved diagnostics | Diagnostic subsets; not independent failures or proof of one cause |
| Initial witness instruction counts | `tab:v2-reducers`; first trial, plain condition per strategy | Eight observed initial bugs; no claim about tiers outside the v2 snapshot |
| No accepted IR-aware reductions on recorded witnesses for the three bugs repaired somewhere | Saved consumed-feedback metadata and triage audit | 89390,135182,126012; pass names are distinct from shown-size checks |
| Two passing 89390 proposals suppress all affected integer div/rem folding rather than only poison-mask cases | `89390.llvmreduce-plain.json`, `89390.llvmreduce-structured.t2.json` certificate patches; dataset reference patch; generated `v2_patch_review.md` | Offline source comparison; optimization loss not measured, not a proof of invalidity |

The raw structured count is an exploratory observation. No improvement in repair
from IR-aware reduction, significant positive result, or completed promotion
ablation follows from this snapshot. Never replace these tables with mixed v2
and v3 records.

## C. New literature checks for this revision

| Reference | Primary source checked | Claim used |
|---|---|---|
| `qwen3coder30b` | https://huggingface.co/Qwen/Qwen3-Coder-30B-A3B-Instruct | 30.5B total / 3.3B activated parameters; official model identity |
| `le2026pairbench` | https://arxiv.org/abs/2607.01360 and HTML Table III / evaluation setup | Exact Qwen model studied with controlled feedback on Python submissions; not LLVM |
| `li2026signal` | https://arxiv.org/abs/2605.07276 and HTML protocol summary | Exact Qwen model is the base for RL compilation-repair experiments; not the same frozen model/method |

Authors, titles, and arXiv identifiers were checked on the primary pages.
No unverified conference venue, DOI, or page numbers were added for these
preprints. Their result percentages are not imported as an expected LLVM rate.

## D. Contribution and novelty boundary

| Proposed claim | Closest cited work | Difference being studied | Current support |
|---|---|---|---|
| Reduce a paired verifier witness before C++ repair | ReduceFix; PGS | Edit source/target IR with an error-class oracle rather than reduce input data or select from a pool | Implemented; usefulness not established by interim repairs |
| Guide reduction with dependencies and report-derived seeds | Delta debugging; LLVM-reduce | Tandem candidate construction and report-derived choices | Code and oracle checks; no global-minimality claim |
| Deterministic structured verifier feedback | Conversational APR; PAIR-Bench | No second model generates hints; IR/report-derived sections | Implemented; presentation also changes content |
| Improve repair success | The above methods and ordinary feedback | Same model and budget, altered feedback | Not established in the current snapshot |

This is a bounded distinction from the sources examined, not an exhaustive claim
that no related method exists. The final novelty wording must follow the measured
outcomes and the closest literature rather than the desired significance level.

## E. V3 status

V3 changes are implementation work prompted by the v2 audit. Their intended
mechanisms are documented in the manuscript and author checklist. The Oct22
amendment reserves the original remaining 16 medium-/hard-tier bugs for a
prospective six-condition follow-up: baseline, raw plain, generic plain,
LLVM-reduce plain, IR-aware plain, and IR-aware structured. Three trials and
four replies permit 288 runs and preserve the original four primary
comparisons. They do not preserve the full nine-condition factorial or its
interactions. `data/repair_v3_protocol.json` must be committed before its
outcomes are collected. No H100 v3 repair result is present in this evidence
map. A changed edit interface or verifier path cannot be credited as a
counterexample-method improvement without within-v3 comparisons.

The two remaining tiers are absent from this v2 snapshot but are not all unseen:
`115149` appears in earlier clean Qwen3 diagnostic results and known-patch
validation. The paper discloses that prior exposure. Prospective v3 outcome
collection does not imply a pristine held-out bug set. Physical candidate-oracle
and trace-refresh costs must be reported separately from the reducer's configured
budget and from the benchmark validation cost.

## F. Literature mapping retained from the earlier draft

The earlier draft's source mappings are retained below for traceability. This
revision did not repeat a full reading or exhaustive verification of every
older quantitative literature claim.


| Manuscript statement | Source | Where |
|---|---|---|
| Refinement vs equivalence; undef/poison/UB; `nsw` -> poison | Alive2 | Sec. 1-2, 5, 8.2 |
| Bounded unrolling can miss bugs; avoids false alarms by over-approximation and declining to report | Alive2 | Sec. 1, 3.8, 7 |
| Check as sequence of queries (UB, return domain, poison, undef, value, memory) | Alive2 | Sec. 5.3 |
| 47 bugs reported, 28 fixed | Alive2 | Sec. 1, 8.2 |
| ddmin: 1-minimal; structure knowledge helps; reduce-to-subset/complement/granularity rule | Zeller & Hildebrandt | Sec. II-B, Fig. 5 |
| C reducers introduced UB; relied on checkers/manual work | Csmith | Discussion paragraph "Creating reportable bugs" |
| `llvm-reduce`: `--test` script, exit 0, delta passes at several granularities | LLVM online docs (fetched 2026-10-06) | page text |
| ReduceFix: LLM-generated ddmin reducer, preserves buggy/reference disagreement, 89.1% mean compression on successful reductions, median input >1 MB, up to 53.8% pass@10 gain, shortening beats output diffs | ReduceFix | Abstract, Sec. 3-5 |
| PGS: selects min-size counterexample from a pool; min beats median/max; token count best proxy | PGS | Sec. 2-3, Table 1 |
| Conversational repair; QuixBugs; feedback = test I/O and errors | ChatRepair | Sec. 1, 3-4 |
| RepairAgent chooses tools, vs fixed feedback loops | RepairAgent | Abstract, Sec. I |
| 66 systems, four control categories | Survey | Abstract |
| Representation affects fine-tuned repair | RepairLLaMA | Abstract |
| llvm-harness: 334 bugs; >35% (mean 60%) drop vs SWE-bench Verified; >60% of passed patches invalid for best pair; below 21% true capability; one-sided McNemar | llvm-harness | Abstract, Sec. 4-5, App. B.1 |
| LLVM-Bench: 423 tasks; <5% / <11%; patch invalidity and build failure dominant | LLVM-Bench | Abstract, Sec. I |
| LLM Compiler: 546B tokens of LLVM-IR/assembly; bespoke license | Meta LLM Compiler | Abstract |
| McNemar + BH + odds ratios in a code study | SLM-as-a-judge | Sec. on statistics |
| Fine-tuned Java repair models lose >50% under some transformations | HEJ-Robust | Abstract |
| pass@k over k samples | ReduceFix | Sec. 4.3 |
