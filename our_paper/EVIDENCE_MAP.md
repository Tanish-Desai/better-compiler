# Evidence map

Paths are relative to the repository clone (`../repo`) at commit `b1d6bfe`. "Run" means I executed it on
2026-10-06; "read" means I checked the code; "doc only" means the claim rests on documentation.

## A. Implementation claims

| Manuscript claim | Evidence | Status |
|---|---|---|
| Witness = reproducer `src`, `opt`-printed `tgt`, `alive-tv` report; `noalias`/`nofree` removed | `llvm-apr-benchmark/scripts/llvm_helper.py` (`verify_dispatch`, `alive2_check`, `filter_out_unsupported_feats`); `ce/alive.py:348-400` | read |
| Parser fields: error class, example, traces with jumps, values, memory | `ce/alive.py` | read; tests on saved outputs pass (run) |
| IR model is line-oriented; round-trips 1462/1462 reproducers | `ce/irmodel.py`; `scripts/check_ir_roundtrip.py` | run (1462 ok, 0 mismatched, 0 crashed) |
| Oracle: three strictness levels, budget, tool errors rejected | `ce/oracle.py` | read; `tests/test_reduction.py` pass (run) |
| `generic` reducer: line-level `ddmin`, units from both files | `ce/reduce_generic.py`, `ce/reduction.py` | read |
| `ddmin` jumps to single-element granularity | `ce/reduction.py:162` | read |
| `llvmreduce`: src then tgt, final joint check, 600 s cap, exit-code-only test | `ce/reduce_llvmreduce.py`, `ce/_llvmreduce_test.py` | read; not run (no binary) |
| IR-aware passes, order, seeds, `max_rounds=4`, bounds 16/32, promotion rules | `ce/reduce_iraware.py` | read |
| Dependency closure: keep-set + terminator operands + returns + branch conditions of reaching blocks | `ce/irmodel.py:483-527`, `reduce_iraware.py:240-249` | read |
| Budget exhaustion discards reduction | `reduce_iraware.py:651-654`, `oracle.py:145-148` | **run with stubbed verifier** (28->28 at budgets 1-3; 28->4 at 400/None) |
| Fallbacks (parse, function missing, final check, re-verification) | `reduce_iraware.py:615-627`, `feedback.py:241-255` | read |
| Plain = IR pair + Alive2 text from "Transformation doesn't verify!" | `ce/structured.py:293-309` | read; test passes (run) |
| Structured sections, six error-class templates, no model text | `ce/structured.py:57-290` | read; tests pass (run) |
| Structured omits per-value traces, adds derived sections | compare `render_plain` and `render_structured` | read |
| `baseline` sentence; non-Alive2 failures pass through | `ce/feedback.py:181-232`, `ce/benchmark.py:78-108` | read; tests pass (run) |
| Oracle runs without per-test `additional_args` | no `extra_args` in `repair_experiment.py`; `normalize_feedback` forwards only given kwargs; dataset lists args | read + counted (5 of 24) |
| Runner: prompts, window (+/-30 lines), reset per turn, 4 turns, no-record rule, temperature default | `examples/repair_experiment.py` | read |
| Differences from `baseline.py` | diff by reading both | read |
| `check_full()` = build + reproducers via Alive2 + `llvm-lit` on `lit_test_dir`, 300 s, max-failures 1 | `lab_env.py:145-165`, `llvm_helper.py:113-150,252-342,345-369` | read |
| Build flags/targets | `llvm_helper.py:113-146` | read |
| Funnel 491/142/135/103/100/99/24 | dataset JSON files; `scripts/select_*.py` | **run** (counts; selection reproduces committed list) |
| Tiers of 33, 8 each; 16 component labels incl. `unknown`; 11 bugs with 2025+ cutoffs; hint spans 6-37 (89 for 114901); window 66-149 | `data/experiment_sample.json`, dataset | run/counted |
| 792 runs = 648 + 144; k=3; 4 iterations | `docs/ANALYSIS_PLAN.md` s5; runner defaults | read (plan, not executed) |
| McNemar exact one-sided, BH, four primary pairs, ablation family, outputs b/c/diff/OR/p | `examples/analyze_significance.py` | read |
| Preregistration date and absence of results | `git log -- docs/ANALYSIS_PLAN.md` (0c4b854, 2026-09-04); `git ls-tree` (no `results/`) | run |
| <5 discordant cannot reach p<0.05 | `scripts/power_analysis.py` (`minimum_b`) | run |
| Power 0.10-0.57 at k=3 | same script, 20,000 simulations | run (0.32 vs documented 0.33 in one cell) |
| 63 tests; 45 pass, 18 skipped without alive-tv/llvm-reduce | `pytest tests` | run |
| Docs describe other branch / other model | `git diff HEAD origin/feat/h100-sweep` (stat and runner diff) | run (read-only fetch) |

## B. Literature claims

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
