# Methodology draft for the active Qwen experiment

> Historical v2 draft. The evaluated snapshot and prospective v3 amendment
> are now described in `our_paper/main.tex`, with frozen snapshot tables in
> `our_paper/generated/`. Follow [H100_V3.md](H100_V3.md) for new runs.

Prepared 2026-10-21 from the implementation at repository commit `38b381b`.
This is a draft of the method, rather than a report of experimental outcomes.
The source revision and configuration actually running on the H100 must be
confirmed against the experiment snapshot before this text is incorporated
into the paper. In particular, the server settings below were reported by
the operator; they are not all captured in the run manifest.

## Study design and scope

We study whether the representation of Alive2 failure evidence changes the
ability of a fixed language model to repair LLVM middle-end miscompilations.
The repair target is the C++ implementation of an optimization, while the
artifact processed by our method is a pair of LLVM IR functions and the
verifier report demonstrating a refinement violation. Alive2, LLVM, the
benchmark dataset, and the language model are existing components. The
intervention consists of reducing the IR witness and constructing the text
shown to the repair model.

The experiment crosses four reduction choices (`raw`, `generic`,
`llvmreduce`, and `iraware`) with two presentation choices (`plain` and
`structured`). A ninth condition, `baseline`, removes the Alive2
counterexample and substitutes a fixed sentence stating that the
transformation is incorrect. The conditions retain the issue title, the
localized C++ window, and the same generation and validation procedure.
Build errors, patch-window mismatches, unchanged replies, and regression-test
failures receive the same feedback-handling policy in every condition. Their
actual diagnostics can differ because the proposed patches differ.

Treatment is present in the initial prompt, which contains feedback from the
unpatched compiler, and in later turns that produce a parseable Alive2
refinement violation. A turn spent on a compilation failure does not receive
a newly reduced counterexample, although its conversation retains earlier
counterexample feedback. The effect under study therefore concerns the whole
repair conversation, rather than an isolated prompt or every validation turn
equally.

## Benchmark population and deterministic sample

The vendored dataset contains 491 issue files. Of 142 miscompilation issues,
135 have reproducer groups checked with Alive2. Requiring a single-function
reference fix leaves 100 eligible issues. Development bug `115575` is excluded
from the evaluation pool, leaving 99 issues. The held-out nine-condition
pilot on that bug is a harness and model check and is not pooled into the
main repair table.

The selection script sorts eligible issues lexicographically by the number
of lines covered by localization hints, total reproducer lines, and issue
identifier. It partitions the sorted pool into three contiguous tiers of
33 issues. Within each tier, a round-robin selection across primary component
labels selects eight issues, yielding 24 issues overall. Component buckets
and their members follow the sorted order. The tier labels `easy`, `medium`,
and `hard` describe this size-based ordering; they do not establish measured
repair difficulty. The committed sample file records the exact identifiers,
base commits, components, and size measures.

All selected issues are evaluated in the same prescribed order. The runner
processes bugs first, trials second, and conditions third so that the build
directory and compiler cache can be reused within an LLVM revision. This is
a paired comparison on a deterministic, restricted sample, not a random
sample of all compiler defects. An issue that passes the unpatched fast check
is skipped. A failed unpatched build stops the sweep as an infrastructure
problem; it is not counted as a failed model repair. Other setup exceptions
may leave a cell without a record and must be reconciled in the final
coverage table.

## Model, environment, and repair loop

The active model is reported as `Qwen/Qwen3-Coder-30B-A3B-Instruct`, served
under the alias `qwen3-coder-30b` by vLLM on an NVIDIA H100. The architecture
has approximately 30.5 billion total parameters and 3.3 billion activated
parameters per token; it should not be described as a dense 3-billion-
parameter model. The reported invocation uses online FP8 quantization,
`--max-model-len 32768`, GPU-memory utilization `0.55`, eager execution,
and `VLLM_USE_FLASHINFER_SAMPLER=0`. A supplied startup log identifies
vLLM `0.28.0`. The model snapshot revision, complete software environment,
GPU variant, CPU allocation, host memory, and effective server sampling
defaults remain to be confirmed from the running experiment.

The client temperature is `0.8`, and generation is not explicitly seeded.
The current client does not explicitly set `top_p` or a maximum completion
length. These effective values depend on the serving configuration and must
be reported rather than inferred from the model name. The runner records
the model alias, temperature, iteration limit, reducer budget, oracle
strictness, prompt hash, and harness version in its experiment configuration.
It records the requested build concurrency separately. The reported
experiment uses 64 build jobs. LLVM compilation and regression testing run
on the host CPUs; they are separate from H100 model inference.

Each issue-condition-trial cell starts a fresh conversation and permits at
most four model replies. Three trials are planned per cell, giving
24 x 9 x 3 = 648 runs and at most 2,592 replies in the main experiment.
The prompt identifies the bug type and primary component, supplies the issue
title, includes the condition-specific failure feedback, and asks for a
replacement C++ window. The issue body, reference patch, and reference C++
implementation are not supplied to the model. The dataset supplies
localization hints derived from the benchmark, so the task measures repair
given localization rather than autonomous fault localization.

The window is selected from the first hinted file at the base commit. It
covers the hinted line ranges with a 30-line margin on each side and is then
expanded by a bounded brace-balancing heuristic. The heuristic ignores some
literal and comment forms and is not a C++ parser; it does not guarantee a
complete syntactic unit. The model is asked to return the complete replacement
window, preserving specified prefixes and suffixes. The system instruction
asks for a small targeted repair, preservation of unrelated optimizations
and IR invariants, and use of concrete validation diagnostics.

Before every proposal, the LLVM source tree is reset to the issue's base
commit. Each reply therefore replaces the original window rather than being
applied as a delta to the preceding proposal. Earlier proposals and feedback
remain in the conversation. Code is extracted from a fenced reply when
present and substituted for the original window. A reply equal to the
original after the implemented whitespace normalization is classified as
unchanged, consumes one turn, and bypasses building. The guard detects
whitespace-normalized equality; it is not a semantic detector of comment-only
or behaviorally equivalent edits. A missing original window also consumes
a turn without validation. Other replies are built and tested, and the run
ends immediately on a full validation pass.

The client retries eligible transient request failures with backoff. Failure
before any completed reply leaves no run record. Failure after one or more
replies is marked by `notes.llm_error`; the resume predicate treats that
record as incomplete and retries the cell. A completed record is an early
success or a run that uses its reply budget without an endpoint-error flag.
The output manifest prevents changing the recorded model, prompt or budget
within an existing experiment directory. It does not by itself pin the
actual checkpoint, quantization, server version, or all source files, so
external provenance capture is also required.

## Counterexample reduction and presentation

For an Alive2 failure, the feedback layer selects the first failing test,
parses the verifier report, and selects its first reported violation. A
reduction candidate is accepted only if a live `alive-tv` invocation reports
a matching failure. The main experiment uses `error_class` strictness:
the error class and function name must match the original violation. This
criterion preserves a bounded refinement violation of the selected class;
it does not prove preservation of the same input, root cause, or semantic
relationship to the original program. Other available strictness settings
are not pooled into the main comparison.

The raw condition leaves the pair unchanged. The generic reducer applies
line-level delta debugging to the two files without representing LLVM IR
dependencies. Its candidate validity is established by the oracle. The
`llvmreduce` wrapper first invokes LLVM's reducer on the source with the
target held fixed, then on the target with the reduced source held fixed,
and checks the pair together. The interestingness script communicates its
decision through an exit status. Each side has an additional 600-second
subprocess limit.

The IR-aware reducer uses a line-oriented representation of functions,
blocks, instructions, and def-use dependencies. It matches source and target
values by SSA name. The report supplies protected seeds from divergences,
target poison/undef/UB, source-only trace values, and the source return.
It also supplies executed blocks and trace types. Candidates use tandem
edits and dependency closure. These constraints reduce dangling uses but do
not guarantee a valid LLVM module. Alive2 rejects candidates that fail its
checks.

The reducer applies eight passes in order: dead-value deletion, branch
folding, unreachable-block pruning, CFG simplification, slicing, operand
promotion, unused-parameter removal, and flag removal. It runs at most four
rounds, stopping earlier on no progress or budget exhaustion. It does not
claim a fixed point or global minimality. Operand promotion introduces fresh
parameters and may generalize the witness to executions unreachable in the
original reproducer; the oracle's error-class check does not eliminate this
threat.

The main reducer budget is 400 candidate verifier calls per reduction,
including the reducer's final acceptance check. The renderer may perform
an additional verification to refresh the trace; the 400-call setting is
therefore not a cap on all verifier activity in the repair run. Budget
exhaustion can cause the final acceptance check to fail and discard earlier
reduction progress. Parse failure, absent target function, failed final
validation, or failed refresh can also produce an unreduced fallback.
Fall-through and budget behavior must be reported as measured implementation
behavior, rather than treating every unchanged witness as evidence that the
original example was irreducible.

Plain presentation consists of the selected source and target IR and the
verifier's diagnostic report. It is a text rendering, not the upstream
benchmark's exact JSON serialization. Structured presentation includes the
same selected pair and mechanically generated sections for the violation,
SSA-based differences, critical values and their dependencies, executed
blocks, example inputs, divergences, memory state when present, and a
template-based interpretation. It omits the full per-value traces included
by the plain rendering. Consequently, the presentation intervention changes
both organization and content. No second language model generates this
feedback.

An active implementation limitation must be resolved in the final account:
the benchmark's original verification uses per-test `additional_args`, but
the feedback adapter at the inspected revision does not forward those
arguments into reduction and trace refresh. Five sampled issues carry such
arguments (`126012`, `114879`, `91369`, `180870`, and `91957`). If this path is
unchanged in the running snapshot, reduction checks may use different loop
bounds from the original verification. Any repair to this path requires a
separate versioned evaluation and an explicit protocol amendment; records
from different feedback semantics must not be silently pooled.

## Validation and interpretation of success

The benchmark builds LLVM with CMake and Ninja in `RelWithDebInfo` mode,
assertions enabled, and compiler caching. It validates the issue's listed
reproducers using the patched optimizer and Alive2. A passing Alive2 result
requires zero incorrect transformations, zero failed-to-prove
transformations, and zero errors. After reproducer success, the environment
checks out the benchmark's specified test-tree revision and runs `llvm-lit`
on the listed regression directories with a 300-second invocation limit and
early stopping on failure. This does not run the whole LLVM test suite.

A repair is counted as a harness pass only when all three stages succeed:
build, reproducer verification, and selected regression tests. A compiling
patch or reproducer-only success is not a full repair. These are plausible
patches under a bounded validation criterion, not proofs of whole-compiler
correctness. The final paper should distinguish this automated outcome from
any subsequent manual review of patch correctness and optimization
preservation.

Per-turn records identify patch application and the last validation stage.
Certificate-level `fast_check_pass` and `full_check_pass` fields are sticky
within a run: they indicate whether a stage passed at any time. A run can
pass its reproducer on an earlier proposal and end with a different failing
patch. Therefore a near-fix or regression-failure case study must recover
the corresponding assistant reply and validation turn, rather than assume
the final dumped patch is the proposal that passed the reproducer.

## Outcomes and paired analysis

The primary outcome is observed bug-level pass@3: an issue succeeds under a
condition if at least one of its three completed trials achieves a harness
pass. Pass@1 is reported alongside it as the fraction of individual runs
that pass. In this terminology a run allows four replies; pass@1 is not a
first-reply success rate. The independent unit is the issue. Repeated trials
reduce sampling noise in a cell but do not turn 24 issues into 648
independent observations.

Primary analyses require the same issue set and three completed trials per
condition being compared. Endpoint-incomplete, absent, malformed, and
duplicate records must be audited separately. Progress tables based on
unequal coverage are descriptive and must not be labelled complete pass@3
comparisons. Because bugs run in sample order, a partial sweep also lacks
the planned balance across size tiers. Infrastructure failures must be
reported and reconciled; they must not be silently recoded as unsuccessful
model repairs or selectively removed because of their outcomes.

The repository's dated analysis plan specifies four one-sided exact
McNemar comparisons: `iraware-plain` against `generic-plain`,
`iraware-plain` against `llvmreduce-plain`, `iraware-structured`
against `iraware-plain`, and `raw-plain` against `baseline`.
For each comparison, b is the number of issues repaired only by the
hypothesized better condition and c the number repaired only by the other.
The test conditions on b+c and uses a binomial success probability of one
half. The four p-values are adjusted together using Benjamini-Hochberg at
0.05. Counts for both-success and neither-success issues, b, c, and the
paired difference in repair proportions should be reported regardless of
statistical significance. When b+c is zero, the observations contain no
discordance to inform the directional comparison.

The other 32 pairs, including the complete method against `raw-plain`,
remain exploratory under that plan. They are not promoted to primary tests
after observing outcomes. An optional promotion-disabled ablation compares
the two IR-aware conditions against their corresponding promotion-enabled
conditions as a separate family. It requires its own completed records and
must be identified as incomplete if the deadline prevents it.

The analysis plan was recorded in repository history before the repair
records it describes. It was not registered with an external registry.
The final paper must report subsequent model, prompt, harness, and protocol
changes with their dates and reasons. Earlier Qwen2.5, earlier Qwen3 harness
records, the development pilot, and any later revised experiment are separate
configurations and are not pooled with the current sweep.

Secondary outcomes include compilation success, reproducer-only passes,
regression failures, unchanged replies, reduction in shown witness size,
fallback reasons, oracle calls, language-model tokens, and measured runtime.
Counts of accepted reducer passes are not by themselves a size-reduction
measure. Feedback token estimates use characters divided by four and are
not tokenizer-exact. Iterations among successful repairs are conditional on
success; an unconditional mean over successful and failed runs is not a
standalone efficiency estimate. Build counts and actual time should
accompany turn counts, since unchanged replies bypass building.

## Limits on conclusions

With 24 deterministic issues, the experiment is a pilot-scale paired study.
It may reveal a substantial difference without establishing significance
for every comparison. Failure to reject a hypothesis does not establish
equivalence or absence of an effect. Neither the need to meet a deadline nor
the desire for a positive result changes the success criterion or analysis
family.

The findings concern the selected single-function miscompilations, one model,
one serving configuration, a localized replacement window, and a fixed
reply budget. They do not establish performance on crashes, hangs,
multi-function fixes, autonomous localization, or all LLVM defects. The
model may have encountered public issue discussions or fixes during
training. The harness's model-cutoff variable is self-declared and does not
verify training-data provenance. Reference patches may be used for separate
validator checks and later patch review but are not model inputs. Any
external coding-agent evaluation needs a restricted workspace that also
prevents access to reference patches and fix-bearing repository history.

The final provenance record must include the exact experiment source commit,
sample checksum, prompt/configuration manifest, model snapshot, effective
serving parameters, tool versions, native build configuration, and the
coverage/exclusion ledger. The raw running-sweep snapshot and these host
details are still needed to finalize those values. This draft does not
substitute assumptions for missing provenance or incomplete outcomes.
