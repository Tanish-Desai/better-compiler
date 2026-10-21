# Author checklist for the interim v2 manuscript

Updated 2026-10-22. The paper source is `our_paper/main.tex`. Results refer to
**data snapshot `73f2ba151742c44cf88878b50c027ef7f06293c6`**, not the changing
working directory. The evaluated runner is `repair-v2`, anchored at `38b381b`.
The revised `repair-v3` implementation has not yet been evaluated on the H100.

## What the paper currently establishes

- An implemented counterexample-processing layer, with a stated preservation
  criterion and nine feedback conditions.
- A reproducible interim snapshot of 205 v2 runs across eight observed bugs.
- A complete common panel of seven bugs, 189 runs, and 21 runs per condition.
  All seven are in the first size tier; this is not the completed 24-bug sample.
- Ten harness-passing runs across three bugs in the entire snapshot; nine
  passing runs across two bugs in the common panel.
- No observed improvement from IR-aware reduction in this panel. All four
  descriptive primary BH-adjusted p-values are 1.0.
- Concrete response-format and compilation failures requiring a separate
  harness evaluation. These do not justify changing or hiding v2 outcomes.

The manuscript does not claim a completed sweep, statistical superiority,
contamination-free evaluation, or proof that a passing patch is correct.

## Before freezing the final result

- [ ] Obtain the exact source commit and a source-diff capture from the H100.
  A harness label and prompt hash do not pin every relevant implementation file.
- [ ] Archive the model snapshot revision, quantization invocation, server
  version and command, effective sampling/output defaults, CUDA/PyTorch
  versions, native Alive2 commit and binary, compiler, CMake, Ninja, and ccache.
- [ ] Record CPU count, cgroup memory, H100 variant, and build concurrency.
  GPU inference and CPU compilation are different resources.
- [ ] Reconcile all planned bug-condition-trial identities. Treat endpoint
  interruptions and missing records separately from completed model failures.
- [ ] Freeze a dated data snapshot before generating final tables. Record its
  Git SHA and checksums. Preserve v2, development pilots, and v3 separately.
- [ ] Generate the matched tables only from equally completed trials. Do not
  compare three-trial success in one condition to a single trial in another.
- [ ] Record any runtime exclusions and their reasons independent of whether
  the condition passed. Retain diagnostic results rather than silently dropping
  compilation failures.
- [ ] Review every harness-passing candidate for semantic correctness and
  preservation of useful optimization behavior. Recover the relevant reply
  for reproducer-only successes; the final dumped patch can be a different turn.
- [ ] Report the promotion-off ablation as absent unless it is actually run.
  Do not use an empty table to imply that the evaluation is complete.

## Revised evaluation: status and amendments

V3 is a planned follow-up prompted by this audit. Its implementation changes
include strict output parsing, atomic SEARCH/REPLACE edits, an explicit
8,192-token output limit, rejection of length-truncated replies, per-test
verifier-argument propagation, refresh-class checks, fallback display metrics,
verified-candidate caching, a reserved final LLVM-reduce validation call, and
avoiding feedback reduction after the last unused reply.

- [ ] Validate v3 on development bug `115575` and known human patches before
  spending the remaining H100 budget.
- [ ] Record the protocol amendment, reason, source commit, model configuration,
  evaluation subset, and planned comparisons before the follow-up starts.
- [ ] Verify that `data/repair_v3_protocol.json` is committed before any v3
  outcomes are collected. The Oct22 amendment specifies the original remaining
  16 medium-/hard-tier bugs, six conditions retaining all four primary
  comparisons, three trials, and four replies: 288 planned runs.
- [ ] Keep the original four primary directions visible. The follow-up uses
  the original remaining two size tiers after data-informed harness debugging;
  these bugs are not all pristine or unseen. In particular, 115149 had earlier
  Qwen3 diagnostics and known-patch checks. Describe prospectivity with respect
  to future v3 outcomes, not first exposure to all bugs. This is not completion
  of the unchanged September nine-condition protocol, does not support all
  factorial interactions, and does not guarantee significance.
- [ ] Distinguish a harness improvement from evidence that counterexample
  reduction improves repair. The latter requires within-version comparisons.
- [ ] Apply changes uniformly across the compared conditions and use a fresh
  output directory. Never relabel v2 records as v3 completions.

## Implementation statements that were corrected

- Feedback is present before the first model reply and on later Alive2-failure
  turns. Each iteration stores the input feedback consumed by that reply.
- V2 uses a bounded brace heuristic, not guaranteed whole functions. The old
  fixed-window range of 66--149 lines is not the v2 window-size result.
- V2 skips builds for whitespace-normalized unchanged replies. It cannot
  recognize every comment-only or semantically unchanged edit.
- Reduction performs at most four rounds, not a guaranteed fixed point.
  Dependency closure does not by itself guarantee valid LLVM IR.
- The oracle preserves a failure class in a function, not the same root cause
  or the original execution. Promotion may produce unreachable examples.
- The round-trip comparison ignores trailing newline characters. It is not
  literal byte-for-byte identity, and does not validate edits.
- Plain rendering is not the original benchmark's exact JSON prompt.
  Structured feedback changes content as well as layout.
- The v2 400-call reduction budget excludes the renderer's refresh call and
  is not a complete repair-run verifier budget.
- V3 must report physical candidate-oracle and refresh calls separately, plus
  their feedback-verification total. That total does not include the benchmark's
  original reproducer checks or lit work.
- V2 omits per-test verifier arguments in reduction/refresh for five sampled
  bugs. Its records remain a measurement of that actual implementation.
- `check_full()` covers listed reproducers and regression directories, not
  the full LLVM regression suite. Certificate pass flags are sticky across turns.
- Interim matched tables require all three trials. Existing progress summaries
  that accept any attempted record must not be used as final pass@3 evidence.

## Sources and remaining review

The new Qwen model-card reference and the PAIR-Bench / GRPO preprint metadata
were checked against their primary pages. The original bibliography is retained;
this update is not an exhaustive revalidation of every related-work number.
`EVIDENCE_MAP.md` links implementation and snapshot claims to their sources.
The initial draft's 45 passed / 18 skipped tests are historical, and do not
certify the revised source or the native H100 installation.

The PDF needs compilation and visual inspection after all source and generated
files have been finalized. Check citation resolution, generated table labels,
long code names, algorithm wrapping, table fit, references, page limit, author
information, and the venue's disclosure and submission requirements. The current
source uses the authors and affiliations supplied in the original draft.
