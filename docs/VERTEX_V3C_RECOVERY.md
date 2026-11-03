# Recover the Vertex v3b sweep after the 91369 verifier timeout

Written 2026-11-03. This is a narrow cohort amendment to the v3b Vertex
experiment. It does not change Gemini, prompts, feedback arms, iteration
limits, budgets, outcome validation, or statistical comparisons.

## What happened

The unpatched Alive2 baseline for bug 91369 timed out before producing an
incorrect-transformation witness. The saved output reported zero incorrect
transformations and one failed-to-prove transformation. The repair runner
therefore correctly withheld the model request, but stopped the whole sweep.
That cell is **verifier-unresolved**, not a failed repair and not a Gemini
timeout.

The amended cohort replaces 91369 with bug 78621, the next deterministic
component-diverse reserve in the same medium complexity tier. Before any model
request, `preflight_vertex_protocol.py` checks the unpatched build, reproducer
and clean Alive2 witness for every not-already-certified primary. If a primary
is ineligible, the predeclared reserve order supplies a replacement from the
same tier. It stops without generating if either tier cannot reach eight
eligible bugs. This preflight makes no LLM calls and does not use the API
reservation ledger.

Completed, treatment-identical cells from the first three v3b bugs may be
carried into a new results directory. `rebind_amended_results.py` copies only
complete cells with clean baseline certificates and matching source, code and
dataset identities. It preserves the originals and records the source config
inside each copied record. The v3b API ledger is reused; do not reset it.

## H100 recovery commands

Run these in Bash inside the existing H100 container. If the repo is not at
`~/24BCE5561/better-compiler`, substitute its actual clone path. Do not run a
preflight while a repair worker owns the shared LLVM checkout.

Start or reattach to a persistent tmux session:

```bash
tmux new-session -A -s bc-v3c
```

Then, inside tmux:

```bash
cd ~/24BCE5561/better-compiler
git status --short
git pull --ff-only origin feat/h100-sweep
source "$HOME/better-compiler-runtime/env.sh"
python3 -m pip install -r requirements-hosted.txt
python3 scripts/configure_vertex.py --project better-compiler --stage main \
    --protocol data/repair_v3c_vertex_protocol.json
source run-provenance/vertex-v3b-main.env
mkdir -p run-provenance
```

The v3c profile intentionally keeps the v3b profile name, project, model,
request settings, and main budget ledger. The configure command must leave the
existing `vertex-v3b-main.env` unchanged. Keep its existing
`vertex-v3b-main-budget.json`; the sweep cap includes earlier reservations.

Confirm that no old repair command is still running:

```bash
pgrep -af 'python3.*repair_experiment.py' || true
```

If a repair process is listed, do not start the next command until it has
exited. Then run the no-model preflight under the same exclusive checkout
lock. `python3 -u` makes each candidate result appear in the log immediately:

```bash
set -o pipefail
flock -n "$HOME/better-compiler-runtime/repair.lock" \
    python3 -u scripts/preflight_vertex_protocol.py \
    --protocol data/repair_v3c_vertex_protocol.json \
    --source-protocol data/repair_v3b_vertex_protocol.json \
    --reuse-completed results-vertex-v3b \
    --build-jobs 8 \
    --out-protocol run-provenance/repair_v3c_vertex_resolved.json \
    --report run-provenance/repair_v3c_preflight.json \
    2>&1 | tee run-provenance/repair-v3c-preflight.log
```

Require exit status zero and a report status of `passed`. The report should
show the previously completed bugs as reused baseline evidence and a
16-bug/two-tier eligible cohort. If it fails or exhausts reserves, stop there;
the script has made no model requests. Preserve its report/log and do not
manually remove a bug or weaken the verifier gate.

Carry the compatible finished cells to the new output directory, without
altering `results-vertex-v3b`:

```bash
python3 -u scripts/rebind_amended_results.py \
    --from results-vertex-v3b --to results-vertex-v3c \
    --source-protocol data/repair_v3b_vertex_protocol.json \
    --protocol run-provenance/repair_v3c_vertex_resolved.json \
    --report run-provenance/repair_v3c-amendment.json
```

Before spending another generation, check the runner's plan:

```bash
python3 examples/repair_experiment.py \
    --protocol run-provenance/repair_v3c_vertex_resolved.json \
    --build-jobs 8 --out results-vertex-v3c --plan
```

For the reported state of 36 complete cells, the plan must say **192 cells: 36
skipped, 156 to run**. If it does not, stop and inspect the manifests and
amendment report; do not use `--overwrite` or delete any manifest.

Start/resume the sweep with unbuffered, append-only logging:

```bash
set -o pipefail
flock -n "$HOME/better-compiler-runtime/repair.lock" \
    python3 -u examples/repair_experiment.py \
    --protocol run-provenance/repair_v3c_vertex_resolved.json \
    --build-jobs 8 --out results-vertex-v3c \
    2>&1 | tee -a run-provenance/repair-v3c-sweep.log
```

In a second shell, follow progress and inspect saved cells:

```bash
tail -n 30 -f run-provenance/repair-v3c-sweep.log
```

Every completed cell is written as a JSON checkpoint. If the SSH connection
drops, reattach with `tmux attach -t bc-v3c`. If the container itself stops,
restart it, source the same profile and rerun the **same** command. Completed
compatible cells are skipped; an interrupted cell is retried. Never reset the
main ledger or delete result files.

## Can it finish overnight?

No reliable overnight guarantee is possible from the data currently available.
The H100 is not doing model inference here: Vertex serves the model, while the
container's CPU builds historical LLVM revisions and runs Alive2/lit. Each of
the 156 remaining cells can request up to four generations and trigger
multiple builds. Tmux prevents an SSH disconnect from ending the process, but
it cannot prevent a container stop or shorten LLVM builds.

Use the actual 36-cell timings to estimate the remaining work after migration:

```bash
python3 - <<'PY'
import glob, json, statistics
rows = []
for path in glob.glob("results-vertex-v3c/*.json"):
    try:
        run = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        continue
    if run.get("notes", {}).get("amendment_provenance"):
        seconds = (run.get("totals") or {}).get("wall_seconds")
        if isinstance(seconds, (int, float)) and seconds > 0:
            rows.append(seconds)
if not rows:
    print("No migrated timing records found; estimate after several new cells.")
else:
    mean = statistics.mean(rows)
    print(f"{len(rows)} carried cells; mean {mean/60:.1f} min/cell; "
          f"rough estimate for 156 cells: {156*mean/3600:.1f} hours")
PY
```

This is only a rough estimate because remaining bugs and model turn counts
differ. If the estimate exceeds the available window, leave the tmux worker
running and resume after any container interruption rather than changing the
model, iteration cap, or cohort mid-study.

## Final audit

Use the amended protocol and output directory for final analysis. Keep v3b and
v3c protocol metadata and the preflight/amendment reports with the paper's
reproducibility materials:

```bash
python3 scripts/triage_sweep.py results-vertex-v3c
python3 scripts/inspect_patches.py results-vertex-v3c
python3 scripts/audit_sweep.py results-vertex-v3c --repeat 3 \
    --condition raw-plain raw-structured llvmreduce-structured iraware-structured
python3 examples/summarize_results.py results-vertex-v3c \
    --protocol run-provenance/repair_v3c_vertex_resolved.json
```

Do not claim a completed 192-cell study until the audit confirms all 192 cells
and matched trials. The 91369 timeout is reported separately as a pre-model
eligibility exclusion.
