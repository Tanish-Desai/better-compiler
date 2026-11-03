# Revised Vertex pilot after inspecting the first failed pilot

> **Main-sweep recovery:** The v3b main schedule later halted before a model
> request on bug 91369 because its unpatched Alive2 check timed out. Do not
> restart that schedule unchanged. Use the same-tier reserve, no-model cohort
> preflight and safe carry-forward steps in
> [VERTEX_V3C_RECOVERY.md](VERTEX_V3C_RECOVERY.md).

Written 2026-11-03. The first four-cell MINIMAL-thinking pilot remains a failed
development pilot: zero full passes. Its supplied same-turn diagnostic JSON
was inspected independently. The derived audit is
[vertex_minimal_115575_pilot.json](../data/diagnostics/vertex_minimal_115575_pilot.json),
which records the input attachment's SHA256. No LLVM validation was rerun in
this development workspace and no API call was made here.

## What the records actually show

- All five build failures are candidate C++ errors: nonexistent member/static
  forms of `isSafeToSpeculativelyExecute`, an unknown class, or a conflicting
  `VecTy` declaration. The shown errors are not OOM or missing-verifier failures.
- Five turns across three conditions passed the reproducer and then failed
  `Transforms/VectorCombine/AArch64/scalarize-scalable.ll`. A fixed-vector cast
  asserted on scalable vectors; a blanket speculation guard suppressed valid
  scalarization expected by FileCheck. These are candidate errors caught by
  validation. Do not delete the failing test or accept reproducer-only passes.
- All three `invalid_format` replies were syntactically complete edit blocks.
  Each SEARCH matched the preceding proposed source exactly, but not the
  original source that the runner resets to. Thus zero code-fence replies was
  expected, and those three rejections reflect edit-base confusion.

The existing original-source instruction was present but did not prevent this
confusion. The revised runner supplies the original editable excerpt again in
each retry and explicitly requests a complete revised patch against it. It
keeps exact matching, checkout resets, code window and validation unchanged.
It does not silently apply an incremental patch to a previous candidate.

The separate `data/repair_v3b_vertex_protocol.json` also selects **LOW** thinking
for the same Gemini 3.1 Flash-Lite model. Google supports this level, but its
benefit for compiler repair is not established by the failed pilot.
[Thinking levels](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-1-flash-lite).
Prompt refresh and thinking change together in this feasibility follow-up;
do not attribute any improvement to one of them separately. Neither a reference
repair nor a target-specific bounds/scalable-vector hint is added to prompts.
All four feedback arms receive the same general task instructions and budget.

## Run only the revised pilot first

Use the H100 container. Its runtime, known-patch PASS and Google ADC login
already exist. Keep the original pilot directory/profile/ledger. Stop any
repair runner before pulling; do not update an active sweep's source.

From the existing clone:

```bash
cd "$HOME/24BCE5561/better-compiler"
mkdir -p archives run-provenance
tar -czf "archives/vertex-first-pilot-$(date -u +%Y%m%dT%H%M%SZ).tar.gz" \
    results-vertex-v3-pilot
git pull --ff-only https://github.com/Tanish-Desai/better-compiler.git feat/h100-sweep
python3 scripts/configure_vertex.py --project better-compiler --stage pilot \
    --protocol data/repair_v3b_vertex_protocol.json
```

The current clone path above is the operator's reported path; substitute it
if the clone was moved. Create a new tmux session:

```bash
tmux new -s bc-vertex-v3b
```

Inside that session:

```bash
cd "$HOME/24BCE5561/better-compiler"
source run-provenance/vertex-v3b-pilot.env
python3 scripts/check_llm_endpoint.py --repair-format
```

Require the format probe to pass before continuing. The new profile reuses
the dedicated ADC config directory, uses LOW thinking and writes reservations
to **vertex-v3b-pilot-budget.json**. Its new **$0.50 reservation limit** does not
reset the prior pilot's $1 ledger. Both ledgers remain part of account usage.
No additional gcloud installation or login is needed unless ADC reports a
credential problem.

```bash
python3 scripts/resource_plan.py --out run-provenance/vertex-v3b-build-resources.json
export BUILD_JOBS="$(python3 -c 'import json; print(json.load(open("run-provenance/vertex-v3b-build-resources.json"))["recommended_build_jobs"])')"
export LAB_LLVM_PARALLEL_LINK_JOBS=1
set -o pipefail
flock -n "$HOME/better-compiler-runtime/repair.lock" \
    python3 examples/repair_experiment.py 115575 \
    --repeat 1 --max-iterations 4 --oracle-budget 400 --require-semantic-baseline \
    --build-jobs "$BUILD_JOBS" --out results-vertex-v3b-pilot \
    --condition raw-plain raw-structured llvmreduce-structured iraware-structured \
    2>&1 | tee -a run-provenance/vertex-v3b-pilot.log

python3 scripts/triage_sweep.py results-vertex-v3b-pilot
python3 scripts/inspect_patches.py results-vertex-v3b-pilot
python3 scripts/diagnose_repairs.py results-vertex-v3b-pilot --bug 115575 --json \
    > run-provenance/vertex-v3b-pilot-diagnostics.json
```

Detach with Ctrl-b, then d; reconnect with `tmux attach -t bc-vertex-v3b`.
The regular plan remains four conditions, one trial, at most four turns, with
unchanged selected regression tests. A single new preflight plus all 16 replies
reserves about $0.3134 at configured ceilings; retries can exhaust the $0.50
guard. This is accounting at configured prices, not a provider-enforced bill
or a guarantee about taxes. Do not reset a ledger to bypass a stop.

## Decision and the separately frozen main schedule

Require four completed cells, at least one full validation pass and review of
the passing patch before any main study. This is a feasibility gate, not a
feedback-effect test on the pilot bug. If it again has zero full passes, do
not launch the 192-cell study or add selected repeat trials to make it pass.
Keep the diagnostics and reassess the model/task floor explicitly.

The revised main manifest retains all 16 bugs, four arms, three trials, four
turns, oracle budget, counterbalanced order and three primary contrasts. The
extra repeated source consumes context; the unchanged 24,576 input-token guard
still counts the actual full history and stops before an over-limit request.
No evaluation outcome from the first pilot can be pooled with the revision.
Changed source/prompt hashes make old output directories incompatible on
purpose; do not delete manifests or use `--overwrite` to get around that.

Only after pilot, passing-patch review and credit-balance gates pass, configure
the revised main profile. This profile is for planning now, not permission to
skip the pilot:

```bash
python3 scripts/configure_vertex.py --project better-compiler --stage main \
    --protocol data/repair_v3b_vertex_protocol.json
source run-provenance/vertex-v3b-main.env
python3 examples/repair_experiment.py --protocol data/repair_v3b_vertex_protocol.json \
    --build-jobs "$BUILD_JOBS" --out results-vertex-v3b --plan
```

The plan must show 192 cells. The main invocation is the same command without
`--plan`, under the shared `flock`, with logs/provenance/backups as in
[VERTEX_H100_RUNBOOK.md](VERTEX_H100_RUNBOOK.md). Substitute the revised
protocol and `results-vertex-v3b` directory in monitoring and final analysis
commands too. Its main ledger remains capped at $18, separate from both pilot
ledgers. Verify combined eligible credits, since only one ₹952.30 grant balance
has been confirmed. Preserve all stages and failure evidence in the paper.
