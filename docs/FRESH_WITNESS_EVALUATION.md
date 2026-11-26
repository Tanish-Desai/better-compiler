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
it compares the original paired reducer with a version that omits trace-derived
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
variant. The [local replay summary](../run-provenance/witness-guidance-local-summary.json)
records results from different binary builds than the original H100 evaluation.

### H100 replay outcome

The [archived H100 report](../witness-comparison-guidance-h100-b32/report.json)
at `ecd51cc` records all 24 cases complete and all 120 outputs verified. The
input and both verifier hashes match the archived fresh evaluation. The
report SHA-256 is `c0030d16ac0be976c02bfa2ca3af64f705cab7a4d1858ccb73f0c959c664806c`.

| Method | Mean instructions removed | Median search and postcheck calls |
|---|---:|---:|
| Trace-guided paired | 8.6% | 3.5 |
| Paired without trace seeds or path folding | 14.4% | 4 |
| Relax if stalled | 12.2% | 4 |
| Default sequential `llvm-reduce` | 8.9% | 33 |
| Focused sequential `llvm-reduce` | 42.8% | 33 |

The paired reducer without trace seeds or path folding left fewer instructions
on seven cases, tied on sixteen, and left more on one compared with trace
guidance. Its mean gain was 5.78 percentage points. The focused LLVM schedule
left fewer instructions than the default LLVM schedule on 20 cases and tied
on four. It left fewer instructions than the paired reducer without trace
seeds or path folding on 22 cases and left more on two. These results
reproduce the main development observations on the original H100 toolchain.
Both replays used the same witnesses. The next prospective study should
compare against the focused schedule on unused cases.

### Reproduce the comparison on the H100 container

Use the existing native runtime. Replace the `cd` path if the clone is
elsewhere. Update the branch before starting a new replay.

```bash
cd ~/24BCE5561/better-compiler &&
git pull --ff-only https://github.com/Abhyuday-06/better-compiler.git feat/h100-sweep &&
tmux new -s bc-guidance
```

Run the following inside tmux. The same block works in a foreground Bash shell.
The plan reads the saved witnesses and lists the case IDs before the replay.
The replay uses installed verifier binaries and needs no LLVM builds, GPU
allocation, or model credits.

```bash
set -e -o pipefail
cd ~/24BCE5561/better-compiler
source "$HOME/better-compiler-runtime/env.sh"
mkdir -p run-provenance

LLVM_REDUCE=/usr/lib/llvm-21/bin/llvm-reduce
GUIDANCE_INPUT=witness-comparison-fresh-b32/report.json
GUIDANCE_OUT=witness-comparison-guidance-h100-b32/report.json

python3 scripts/replay_reducer_guidance.py --input "$GUIDANCE_INPUT" --plan
git rev-parse HEAD > run-provenance/witnesses-guidance-h100-b32.commit
sha256sum "$LAB_LLVM_ALIVE_TV" "$LLVM_REDUCE" "$GUIDANCE_INPUT" \
  | tee run-provenance/witnesses-guidance-h100-b32-inputs.sha256
python3 -u scripts/replay_reducer_guidance.py \
  --input "$GUIDANCE_INPUT" \
  --out "$GUIDANCE_OUT" \
  --jobs 4 \
  --alive-tv "$LAB_LLVM_ALIVE_TV" \
  --llvm-reduce "$LLVM_REDUCE" \
  2>&1 | tee -a run-provenance/witnesses-guidance-h100-b32.log
sha256sum "$GUIDANCE_OUT" > run-provenance/witnesses-guidance-h100-b32.sha256
```

Detach with Ctrl-b then d and reattach with `tmux attach -t bc-guidance`.
The report checkpoints each completed witness. Restart from the recorded
commit with the same inputs, binaries, worker count, and output path. A
different setup needs a fresh output path. Use the following read-only check
from the repository root.

```bash
python3 scripts/replay_reducer_guidance.py --status \
  --out witness-comparison-guidance-h100-b32/report.json
```

Retain the report, log, commit, and checksums. The report also records the
input, source, and verifier digests. On a local Linux or WSL installation
with the verifier binaries already available, set `LAB_LLVM_ALIVE_TV` and
`LLVM_REDUCE` to their absolute paths and use a separate `GUIDANCE_OUT`.
Report compression and search-call counts from the parallel run. For a timing
follow-up, use one worker and a separate output path, repeat measurements,
and record the CPU allocation.

### Confirm performance on unused witnesses

The [frozen protocol](../data/fresh_witness_frontier_protocol.json) targets 18
unused bugs, six per reproducer-size tier, from 27 candidates. The runner
builds unpatched `opt` and uses the first six eligible witnesses per tier.
It compares the paired reducer with trace seeds and path folding disabled
against focused source-first `llvm-reduce` at 4, 8, 16, and 32 reducer calls.
Each result receives an independent Alive2 postcheck, one call outside the
ceiling. Failed outputs receive zero removal credit. Eight calls is the
primary comparison, with a two-sided paired sign test and a prespecified
five-percentage-point minimum mean gain. The other ceilings are descriptive.

Run from the repository root in Bash, optionally inside `tmux new -s bc-frontier`.
The plan needs no LLVM binaries. The full run needs the native runtime and
can spend substantial CPU time on historical builds. It uses no GPU or model credits.

```bash
set -e -o pipefail
git pull --ff-only https://github.com/Abhyuday-06/better-compiler.git feat/h100-sweep
source "$HOME/better-compiler-runtime/env.sh"
mkdir -p run-provenance

python3 scripts/run_witness_frontier.py --plan
flock -n "$HOME/better-compiler-runtime/repair.lock" \
  python3 -u scripts/run_witness_frontier.py \
    --build-jobs 8 \
    --llvm-reduce /usr/lib/llvm-21/bin/llvm-reduce \
    --out witness-comparison-frontier/report.json \
  2>&1 | tee -a run-provenance/witnesses-frontier.log
```

Detach with Ctrl-b then d. Use the same commit, binaries, build concurrency,
and output path if resuming. The report checkpoints each eligible capture and
completed comparison. A build failure stops the runner for diagnosis. A tier
that exhausts its fixed reserves stays incomplete. To inspect saved progress:

```bash
python3 scripts/run_witness_frontier.py --status --out witness-comparison-frontier/report.json
```

Retain the JSON report and log. A later mechanism study can toggle trace seeds
and executed-path folding independently with promotion held fixed. Separately
test whether reduced sources still produce a violation after the original
`opt` pipeline runs, and repeat with the reference fix where available.

### Review the archived repair patches

Use the saved turn-level diffs to review the benchmark-passing candidates and
diagnose reproducer-only fixes. This review uses neither LLVM builds nor model
credits. Run from the repository root.

```bash
mkdir -p run-provenance
REVIEW_DIR=$(mktemp -d)
tar -xzf vertex-v3c-records.tar.gz -C "$REVIEW_DIR"
python3 scripts/diagnose_repairs.py "$REVIEW_DIR/results-vertex-v3c" \
  --bug 116553
python3 scripts/diagnose_repairs.py "$REVIEW_DIR/results-vertex-v3c" \
  --json > run-provenance/repair-v3c-turn-review.json
```

Use the edit and validation from the same turn. For each passing patch, review
the bug fix, the scope of the changed optimization, and tests covering nearby
cases where the optimization should still fire.
