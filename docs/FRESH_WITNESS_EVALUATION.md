# Fresh witness evaluation at 32 Alive2 calls

This experiment measures compression on real LLVM miscompilations under a fixed verifier budget. It follows the development results in [Vertex v3c repair and reducer results](VERTEX_V3C_RESULTS.md). The completed v3c study remains the repair evaluation. Its primary tests did not establish a repair advantage. This follow-up makes no model requests.

## Method and primary comparison

The paired reducer proposes edits on both sides of an LLVM transformation using the Alive2 diagnostic and IR dependencies. The hybrid spends up to eight calls on that search. It starts sequential `llvm-reduce` from the paired result only if the pair has fewer instructions. Otherwise the sequential phase starts from the original pair. Any unused calls from the first phase roll into the second. Character count breaks ties when selecting the output after sequential reduction. The instruction gate is a change from the development hybrid. On case `118467`, a seed with the same number of instructions changed the search trajectory and the hybrid returned a larger pair than sequential reduction.

The primary comparison is this hybrid versus source-first sequential `llvm-reduce`. Both have a total allowance of 32 reducer calls, including any final pair check charged to the reducer. The one shared establishment call, any diagnostic refresh, and the independent output check are recorded separately. The secondary comparison uses the same hybrid and baseline with the balanced allocation rule. A positive result would support the combined pipeline under this budget and on this corpus. It would take a separate seed-blind ablation to attribute the gain specifically to the diagnostic, and a separate repair trial to estimate any downstream effect.

## Cohort and analysis

The fixed [protocol](../data/fresh_witness_b32_protocol.json) lists 16 candidates in each of three reproducer size tiers. It excludes previously evaluated bugs and conservatively excludes IDs listed as reserves in the earlier repair plans. Candidates come from the repository's single-function LLVM miscompilation benchmark cases with an Alive2 check. Within each tier, deterministic hash order and round-robin component selection fix the order. The run selects the first eight eligible cases per tier, for 24 total. Eligibility depends on the first failure of the unpatched reproducer yielding one clean Alive2 violation with a named function and error class. Exact duplicate fresh pairs are skipped. Build or checkout failures interrupt the run so they can be diagnosed without silently changing the sample.

For each method and bug, compute the fraction of original source and target instructions removed. The reported pair must pass a fresh Alive2 check for the same function and error class with one incorrect transformation and no verifier errors or unresolved proofs. A method without a verified output gets zero credit for removal and its failed check is counted. The primary test is the two-sided exact paired sign test at 0.05. Report wins, ties, losses, and the mean and median paired effect. The operational improvement criterion also requires a mean gain of at least five percentage points of original instructions removed. The balanced comparison is descriptive. A partial run reports coverage and is insufficient for the planned inference.

## Run and resume

The runner uses the existing native LLVM checkout and builds only `opt` at each selected bug's recorded base commit. It captures the unmodified reproducer pair and then compares the reducers. These historical builds can still take substantial time. Each eligibility decision, captured witness, and completed replay is saved before advancing. Use the native workspace's repair lock while the command is active.

```bash
tmux new -s bc-fresh-witnesses
```

Inside tmux, using the existing project virtual environment:

```bash
set -o pipefail
cd ~/24BCE5561/better-compiler &&
git pull --ff-only https://github.com/Abhyuday-06/better-compiler.git feat/h100-sweep &&
source "$HOME/better-compiler-runtime/env.sh" &&
python3 scripts/run_fresh_witnesses.py --plan &&
flock -n "$HOME/better-compiler-runtime/repair.lock" \
  python3 -u scripts/run_fresh_witnesses.py \
    --build-jobs 8 \
    --llvm-reduce /usr/lib/llvm-21/bin/llvm-reduce \
    --out witness-comparison-fresh-b32 \
  2>&1 | tee -a run-provenance/witnesses-fresh-b32.log
```

Detach with Ctrl-b then d. If the container stops, use the same code, verifier binaries, protocol and output directory when restarting. The following check only reads saved data:

```bash
cd ~/24BCE5561/better-compiler
python3 scripts/run_fresh_witnesses.py --status --out witness-comparison-fresh-b32
```

Retain `witness-comparison-fresh-b32/report.json`. It stores the fixed cohort, data and binary identities, screen outcomes, pairs, call counts, and final analysis when all 24 witnesses have been compared.

## Trace guidance follow-up

The archived 24-witness evaluation shows that standalone IR-aware reduction
used fewer verifier calls on every bug and produced equal or smaller output
on 19. The paired size counts were seven smaller, twelve tied, and five larger.
This is a cost and output-size comparison, with no claim of equivalence or of a
gain in model repair rate.

The follow-up reuses the 24 saved pairs. At the same 32-call search ceiling,
compare the original paired reducer with a version that omits trace-derived
seeds and disables executed-path branch folding. Both versions retain SSA
dependency closure, returned-value protection, and the trace-derived type
information needed by operand promotion. This ablation measures the joint
contribution of seed and path guidance while holding the promotion type lookup
fixed. A new variant tries guided reduction for at most eight calls. If it
removes no instructions, it retries from the original pair with the remaining
allowance and without seed or path guidance.

The two sequential controls use default `llvm-reduce` and the same wrapper
restricted to instruction, basic block, CFG, argument, operand-to-argument,
and flag passes. All arms share the original verifier arguments and require a
fresh output check. Report fraction of instructions removed, paired counts,
and verifier calls. The archived cohort is reused after inspecting the original
results, so this is a mechanism study and a development comparison for the new
variant. Any favorable effect would need confirmation on a new cohort.

```bash
cd ~/24BCE5561/better-compiler
git pull --ff-only https://github.com/Abhyuday-06/better-compiler.git feat/h100-sweep
source "$HOME/better-compiler-runtime/env.sh"
set -o pipefail
python3 -u scripts/replay_reducer_guidance.py \
  --jobs 4 \
  --alive-tv "$LAB_LLVM_ALIVE_TV" \
  --llvm-reduce /usr/lib/llvm-21/bin/llvm-reduce \
  2>&1 | tee -a run-provenance/witnesses-guidance-b32.log
```

This uses the saved pairs and installed verifier binaries. It needs no LLVM
builds, GPU, or model credits. Each completed pair is saved to
`witness-comparison-guidance-b32/report.json`. Reuse the command to resume.
The parallel run supports comparisons by call count and compression. Elapsed
times can include contention from other replay workers.
