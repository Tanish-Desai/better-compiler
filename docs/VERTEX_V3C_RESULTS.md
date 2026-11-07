# Vertex v3c repair and reducer results

The completed sweep contains 192 usable trials for 16 LLVM bugs. The raw records and the earlier reducer report are archived at commit `fbc772d37116d9336d9527baad9816fb5a8eeacf`. The audit regenerated from the archive matches the saved audit. The three preregistered comparisons use a bug as the unit. A bug counts as repaired for a condition when at least one of its three trials passes the build, reproducer, and selected regression tests.

| Feedback | Passing trials | Bugs repaired in three trials |
|---|---:|---:|
| Raw plain | 13/48 | 7/16 |
| Raw structured | 22/48 | 9/16 |
| Sequential `llvm-reduce`, structured | 11/48 | 6/16 |
| IR-aware, structured | 16/48 | 8/16 |

The IR-aware arm trails raw structured by 6.25 percentage points and leads sequential `llvm-reduce` by 12.5 points. The respective corrected one-sided McNemar p-values are 1.0000 and 0.5156. Raw structured leads raw plain by 12.5 points (corrected p = 0.5156). The paired outcomes provide no statistically significant repair improvement in any registered comparison.

## Reduction cost and output size

Use the first feedback event in each trial. It precedes any model edit, so later patches, build failures, and early stopping cannot change the initial input. For each arm and bug, take the median over the three trials and then summarize across the 16 bugs. Report witness size as the combined size of source and target IR shown to the model. `oracle_calls` includes the reducer's recorded Alive2 calls, including any final pair check charged to that budget. Add `feedback_refresh_calls` to count the additional calls used to regenerate the displayed diagnostic. The original check shared conceptually by both methods is outside these figures.

| Initial feedback measure | IR-aware | Sequential `llvm-reduce` |
|---|---:|---:|
| Median reducer Alive2 calls | 1.5 | 250 |
| Median reducer calls plus feedback refresh | 2.5 | 251 |
| Total reducer calls plus refresh, 48 feedback events per arm | 264 | 13,170 |
| Median reported Alive2 time including refresh | 0.106 s | 11.365 s |
| Median instructions shown, from a common median of 11 | 8 | 2.5 |
| Bugs with fewer instructions shown | 5/16 | 16/16 |

IR-aware used fewer recorded calls, including refresh, on all 16 bugs. Sequential `llvm-reduce` left fewer instructions on 15 bugs. The exploratory two-sided exact sign test for call counts is p = 0.0000305 (16 lower, 0 higher). It compares the costs of the two configured workflows at their observed stopping points. The workflows returned different witness sizes, and the test was selected after viewing the completed sweep. It cannot establish an advantage at equivalent compression.

The earlier development replay on eight saved witnesses independently checked that all outputs still elicited the same function and error class. The IR-aware arm used a median of 3.5 recorded calls including refresh and postcheck versus 278 for the sequential baseline. It reduced the instruction count on 3/8 bugs versus 8/8. Keep these development results separate from v3c. Matching the function and error class is a bounded verifier check. It does not establish that an identical input triggers both violations or that both failures have the same root cause.

The candidate contribution is a paired reduction strategy that uses the Alive2 diagnostic and IR dependencies to propose changes on both sides. The evidence so far supports a cost versus compression tradeoff for the implemented workflows. Prior work already uses compiler-specific LLM repair and verifier feedback. Compare against [llvm-harness](https://arxiv.org/abs/2603.20075), [LPO](https://arxiv.org/abs/2508.16125), [Alive2](https://doi.org/10.1145/3453483.3454030), and the [LLVM reducer](https://llvm.org/docs/CommandGuide/llvm-reduce.html) when positioning the contribution. The tested baseline invokes the LLVM tool sequentially on the two sides. Results concern this integration.

## Repair bottlenecks

- The output cap rejected 189/620 replies (30.5%). Rates were 45/153 for IR-aware, 46/168 for sequential LLVM reduction, 53/161 for raw plain, and 45/138 for raw structured. This occurred in every arm.
- There were 91 turns with a regression failure across 66 trials. Of the 125 trials with a reproducer pass, 62 eventually passed full validation and 63 ended without a full pass.
- On 116553 the IR-aware reducer shrank the witness from 23 to 3 instructions, yet no IR-aware trial passed full validation. One candidate fixed the reproducer and narrowed where the compiler adds a loop exit fact. That edit then failed an existing `FileCheck` expectation in `induction-condition-in-loop-exit.ll`. Raw structured repaired the bug in all three trials. This is a concrete case where better compression and better repair outcomes diverge.
- Six IR-aware initial pairs retained the original instruction and character counts with no accepted reduction across the three trials. Five more had no instruction reduction despite a character reduction. The initial audit recorded no fallback errors. These counts measure search behavior on these inputs and do not establish minimality.

## Next CPU experiment

Replay the same 16 initial pairs under a shared cap of 32 Alive2 calls charged to the reducer. The cap is exploratory and was chosen after the completed sweep. It is just above the largest observed IR-aware first-turn call count of 27. Compare the fraction of original instructions removed per bug under the shared cap, with a fresh postcheck for each method. Count a verified unchanged pair as zero reduction, retain the identities of all failed checks or missing records, and report wins, ties and losses for each pairwise comparison. Report costs and sizes at the original cap of 400 as a reference point. Record verifier seconds and total seconds because equal call caps can hide large differences in per-call cost.

Run this from the benchmark repository in a tmux session on the existing Linux container. If the original results directory is unavailable, recover it with `tar -xzf vertex-v3c-records.tar.gz` in the repository. The archive has 192 cell records plus the hidden experiment manifest.

```bash
source "$HOME/better-compiler-runtime/env.sh"
set -o pipefail
python3 scripts/compare_saved_witnesses.py --directory results-vertex-v3c --plan
python3 -u scripts/compare_saved_witnesses.py \
  --directory results-vertex-v3c --budget 32 \
  --out witness-comparison-v3c-b32 \
  2>&1 | tee run-provenance/witnesses-v3c-b32.log
```

The inventory should show 16 agreed witnesses and zero extraction issues. Keep the report and inspect unsuccessful rows, including any budget exhaustion. The existing sequential wrapper gives the source side first access to the budget. A small cap may starve the target side. Treat that outcome as a result for the existing wrapper and include a tuned sequential control with an explicit split and a fixed pass schedule before attributing an advantage to diagnostic guidance.

```bash
python3 -u scripts/compare_saved_witnesses.py \
  --directory results-vertex-v3c --budget 400 \
  --out witness-comparison-v3c-b400 \
  2>&1 | tee run-provenance/witnesses-v3c-b400.log
```

These replays use the existing Alive2 and `llvm-reduce` binaries. They make no LLM requests and perform no compiler rebuilds. Keep the replay separate from the completed repair sweep. If the low-budget advantage survives the tuned baseline, freeze one endpoint, the cap, and the baselines before collecting an independent witness cohort. A seed-blind version of the same paired reducer would test whether guidance from the counterexample contributes beyond pairing and dependency closure. The existing no-promotion condition isolates a different design choice.

For a repair-rate follow-up, raw structured is the strongest observed control. A separate pilot could compare it with feedback that retains the original witness and adds a concise contrast between the original failing case and a regression that must remain optimized. The current harness already returns regression diagnostics. This extension would need to identify the changed IR behavior explicitly. The pilot should establish that the output truncation rate is under control for both arms before a new matched evaluation.
