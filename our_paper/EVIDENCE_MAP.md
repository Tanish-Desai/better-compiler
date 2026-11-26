# Evidence map for the manuscript

## Completed evaluations

| Manuscript claim | Evidence | Scope |
|---|---|---|
| 192 complete v3c repair trials over 16 bugs. Pass@3 is 7, 9, 6, and 8 for raw plain, raw structured, default source-first sequential reduction, and IR-aware reduction. | [`vertex-v3c-records.tar.gz`](../vertex-v3c-records.tar.gz) at `fbc772d`, [`repair-v3c-audit.json`](../run-provenance/repair-v3c-audit.json), [results](../docs/VERTEX_V3C_RESULTS.md) | Localized edits from Gemini via Vertex. A plausible repair passed the build, reproducer, and selected regression checks. The panel includes 36 compatible trials carried over after two cohort substitutions. |
| No significant v3c repair contrast. | `examples/analyze_significance.py --final` with [`repair_v3c_vertex_resolved.json`](../run-provenance/repair_v3c_vertex_resolved.json) | Three prespecified, bug-paired, one-sided exact McNemar tests with BH correction. The outcomes leave the repair effect uncertain. |
| 189 of 620 v3c replies hit the output cap. Of 125 trials with a recorded reproducer pass, 62 passed all checks and 63 ended without a full pass. | [Audit](../run-provenance/repair-v3c-audit.json) and [repair bottlenecks](../docs/VERTEX_V3C_RESULTS.md#repair-bottlenecks) | Descriptive across all four arms. The last patch in a reproducer-only trial may differ from the version that passed the reproducer. |
| 24 fresh cases and 192 verified outputs. The hybrid gains 0.7 points over default source-first sequential `llvm-reduce`, with 8 wins, 12 ties, 4 losses, and a two-sided sign-test p-value of 0.388. | [Frozen protocol](../data/fresh_witness_b32_protocol.json) at `fc8c671` and [report](../witness-comparison-fresh-b32/report.json) at `520e08d`, `analysis.primary` | Both reducers had a 32-call search ceiling. The primary comparison fell short of its five-point and significance criteria. The independent check preserves the named violation class in the same function. |
| Standalone IR-aware uses fewer verifier calls on all 24 cases. The medians are 4.5 versus 34 calls for default source-first sequential `llvm-reduce`. Mean compression is 8.6% versus 8.9%. | [Fresh report](../witness-comparison-fresh-b32/report.json), using per-case `total_physical_calls` and combined instruction counts | Descriptive at the methods' observed stopping points under a 32-call reducer ceiling. Measured calls include diagnostic refresh and independent recheck and exclude the shared establishment call. The study did not test equivalence or compare cost at a fixed output size. |
| The balanced hybrid gains 6.1 points over balanced sequential reduction, with 8 wins, 15 ties and 1 loss. | [Fresh report](../witness-comparison-fresh-b32/report.json), `analysis.balanced_secondary` | Secondary comparison. The balanced sequential baseline averaged 4.2% removal versus 8.9% for the default source-first baseline. |
| The standalone reducer shrinks ten witnesses with operand promotion enabled and four with it disabled. | [Fresh report](../witness-comparison-fresh-b32/report.json), paired reducer variants | Reducer-only comparison under the same Alive2 preservation rule. Promotion can introduce inputs that cannot arise in the original execution. The effect on model repair was unmeasured. |
| Default sequential `llvm-reduce` shrinks 10 of 16 development witnesses at 32 calls and 16 of 16 at 400 calls. Standalone IR-aware reduction stops below 32 reducer calls on every case in that panel. | [32-call replay](../witness-comparison-v3c-b32/report.json) and [400-call replay](../witness-comparison-v3c-b400/report.json) | Uses the same 16 initial v3c witnesses for both budgets. The fresh 24-witness panel has a separate frozen protocol. |

## Exploratory follow-up on the archived fresh witnesses

The [local replay summary](../run-provenance/witness-guidance-local-summary.json)
records a CPU replay of the same 24 witnesses with different Alive2 and
`llvm-reduce` binaries from the frozen study. All 120 outputs passed the
independent check across five arms. At the 32-call reducer ceiling, default
source-first `llvm-reduce` averaged 8.9% instruction removal and the focused
source-first `llvm-reduce` pass schedule averaged 42.8%. The focused schedule
left fewer instructions on 20 witnesses and tied on four. Both used a median
of 33 calls including the independent recheck. The replay has no diagnostic
refresh in these totals.

Guided paired reduction averaged 7.4% removal at a median of 3.5 calls.
Disabling trace-derived seed selection and path folding averaged 14.4% at a
median of 4 calls while retaining trace-derived promotion types. The guided
arm left fewer instructions on one case, tied on 15, and left more on eight.
The retry-on-stall variant averaged 12.2% removal at a median of 4 calls. These
estimates are exploratory because the witnesses were reused after inspecting
their original results. The replay supports testing the focused baseline and
the guidance ablation under a frozen protocol on new witnesses. [Replay design
and container command](../docs/FRESH_WITNESS_EVALUATION.md#trace-guidance-follow-up)

The [archived H100 replay](../witness-comparison-guidance-h100-b32/report.json)
at `ecd51cc` verifies 120 outputs on the same 24 pairs using the original
binary hashes. Focused and default LLVM again averaged 42.8% and 8.9% removal
at a median of 33 calls each. The focused schedule left fewer instructions
on 20 cases and tied on four. Disabling trace seeds and path folding increased
mean removal by 5.78 points. The postcheck requires the same function and
violation class. The cohort was reused after the original results were
inspected. [H100 replay summary](../docs/FRESH_WITNESS_EVALUATION.md#h100-replay-outcome)

## Implementation checks

The [native integration tests](../tests/test_integration.py) use small
fixtures to check preservation, reduction, and rendering with the real
verifier. Those tests are conditional on the installed binaries. The
[guidance tests](../tests/test_reducer_guidance.py) use mocks to check that the
retry respects the remaining call budget and that the final check rejects a
different failure class or an unresolved proof. The 1,462-reproducer
[round-trip check](../scripts/check_ir_roundtrip.py) compares unchanged text
after stripping trailing newlines. The November 26 Windows test run on
`ca7657a` reported 389 passing and 19 skipped tests. The fresh report supplies
corpus-level evidence for the tested saved outputs under its recorded
binaries. Record the native test results separately on the container.

## Historical v2

Updated 2026-10-22. Paths are relative to this repository. The evaluated method
is `repair-v2`, with code anchored at `38b381b`. Its numerical results are frozen
at `73f2ba151742c44cf88878b50c027ef7f06293c6`.

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
| Reduce a paired verifier witness before C++ repair | ReduceFix and PGS | Edit source and target IR with an error-class oracle | Implemented. The fresh evaluation supports a low-call first pass against default sequential reduction. |
| Guide reduction with dependencies and report-derived seeds | Delta debugging and `llvm-reduce` | Tandem candidate construction and report-derived choices | Implemented and checked with the oracle. The exploratory ablation leaves the contribution of guidance open and favors testing a seed-and-path-disabled variant on new witnesses. |
| Deterministic structured verifier feedback | Conversational APR; PAIR-Bench | No second model generates hints; IR/report-derived sections | Implemented; presentation also changes content |
| Improve repair success | The above methods and ordinary feedback | Same model and budget, altered feedback | The completed v3c comparisons did not establish a gain. |

This is a bounded distinction from the sources examined, not an exhaustive claim
that no related method exists. The final novelty wording must follow the measured
outcomes and the closest literature rather than the desired significance level.

## E. Protocol history between v2 and v3c

The [October six-condition plan](../data/repair_v3_protocol.json) proposed 288
runs on the remaining two size tiers. The [November shared-H100 amendment](../data/repair_v3_shared_h100_protocol.json)
proposed four arms and 192 trials, plus an eight-witness CPU diagnostic. Those
plans preceded the [resolved Vertex v3c protocol](../run-provenance/repair_v3c_vertex_resolved.json)
used for the completed repair panel above. The eight-witness development
diagnostic is archived in [its own report](../witness-comparison-v2-cpu/report.json).
Bug `115149` appeared in prior Qwen diagnostics and known-patch validation.
The v3c comparisons concern feedback changes within the revised harness.

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
