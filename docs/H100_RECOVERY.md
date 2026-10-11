# H100 recovery after the mixed-model sweep

Checked against the results uploaded on `feat/h100-sweep`, 2026-10-11.

The original `results/` contains 213 historical Qwen2.5 14B records and 30
Qwen3 30B records. The previous resume check only looked at filenames, so the
Qwen3 invocation skipped completed Qwen2.5 cells. The 243-cell counter was
not progress through a Qwen3 sweep.

`results-qwen3-clean/` contains 30 Qwen3 runs: all 27 cells for `115149`, plus
two cells for `116249` and one for `165878`. Only one bug has a complete paired
row. Its 120 replies include 21 unchanged replies (17.5%), 99 build attempts
and 28 failed builds. Twelve runs reached regression validation, all on
`115149`, and all have real lit test failures in their conversations. There
were also reproducer-stage timeouts; they are not lit timeouts. The three old
pilot directories were all Qwen2.5 runs.

Typical failures on `115149` were GEP flag/output assertions in
`opaque-ptr.ll` and `gepphigep.ll`; one patch also failed
`new-inst-dbgloc-overwrite.ll`. A passing reproducer does not establish a full
fix, and the certificate's `fast_check_pass` means *some* turn passed, not
necessarily the saved final patch. Do not weaken these tests or report the
mixed-model repair rates.

## What changed

- The runner checks the served model before LLVM builds and refuses output
  directories containing another model, prompt/harness or repair budget.
  `--overwrite` cannot bypass the experiment boundary.
- New directories get `.experiment.json`; records carry the same configuration.
  The v2 prompt asks for a minimal repair that preserves other optimizations
  and IR invariants and explicitly requires regression validation.
- Each turn records its validation stage and distinguishes real regression
  failures from timeouts and infrastructure failures. An unpatched build
  failure stops the sweep rather than being presented as a compiler bug or
  retried under every feedback condition.
- Analysis tools reject mixed configurations. `--model` permits inspecting a
  single historical model without mixing model outcomes. Similarity numbers
  are descriptive and no longer declare the feedback design or model failed.
- `--plan` shows the number of cells to run without contacting the endpoint,
  importing the LLVM harness, writing files, or building anything.

The v2 prompt is a changed task setup, uniformly across all nine conditions.
Keep both uploaded directories as historical data. Start v2 in a new directory;
do not copy the 30 old Qwen3 cells into it. Their validation evidence remains
useful, but their repair rates must not be pooled with v2.

## Required H100 checks, in order

Stop the old repair process before pulling or running these commands. Keep the
existing vLLM server running. Run one LLVM-related command at a time: the gold
check and repair loop both reset the same checkout/build directory.

```bash
git checkout feat/h100-sweep
git pull --ff-only https://github.com/Abhyuday-06/better-compiler.git feat/h100-sweep
source ~/better-compiler-runtime/env.sh

# Override the native setup's 14B defaults AFTER sourcing env.sh.
export LAB_LLM_URL=http://127.0.0.1:8000/v1
export LAB_LLM_MODEL=qwen3-coder-30b
export LAB_LLM_TOKEN=local-sweep
export LAB_LLM_TEMP=0.8

python3 -m pytest tests -q
python3 scripts/check_llm_endpoint.py
```

Use the active repair-loop venv. The already-running Qwen3 vLLM command with
32,768 context, FP8 and `--enforce-eager` can stay as it is. If the runtime
root differs, use its `env.sh` path. Use 64 build jobs only if the container's
memory budget supports it; the existing native runbook explains that check.

First, validate the known human repair for the bug producing near-fixes:

```bash
python3 scripts/check_validation.py --bug-id 115149 --build-jobs 64
```

**Require PASS.** This must build the original commit, confirm an Alive2
incorrect transformation, apply the dataset's human patch, and pass both the
reproducer and lit. Logs go to `validation-checks/115149.json`, outside the
repair tables. It restores the unpatched checkout on completion. These are
oracle diagnostics, not model repairs. If it fails, stop and inspect that
JSON before any further LLM sweep.

Next, check the held-out pilot's known fix, then run the nine-condition v2
pilot on that same commit. `115575` is excluded from the 24-bug sample.

```bash
python3 scripts/check_validation.py --bug-id 115575 --build-jobs 64

conditions=(baseline raw-plain generic-plain llvmreduce-plain iraware-plain
            raw-structured generic-structured llvmreduce-structured iraware-structured)

python3 examples/repair_experiment.py 115575 \
    --repeat 1 --max-iterations 4 --build-jobs 64 \
    --out results-qwen3-v2-pilot --condition "${conditions[@]}" --plan

python3 examples/repair_experiment.py 115575 \
    --repeat 1 --max-iterations 4 --build-jobs 64 \
    --out results-qwen3-v2-pilot --condition "${conditions[@]}"

python3 scripts/triage_sweep.py results-qwen3-v2-pilot
python3 scripts/inspect_patches.py results-qwen3-v2-pilot
```

**Require the second gold check to PASS before running the pilot.** Run the
commands individually; a failed command must not be followed by the next one.
The pilot is nine run cells, at most 36 model turns. Do not launch another
648-cell sweep automatically. Review its validation stages, concrete patches,
unchanged rate and full fixes. One unsuccessful pilot bug alone does not prove
a model floor; if no full fixes appear, inspect the actual attempts before
committing days of compute. Do not raise repeats as a substitute for this check.

Share `validation-checks/` and `results-qwen3-v2-pilot/` after these runs.

## Full sweep only after the pilot is reviewed

Keep the same model, temperature, four-turn limit and reducer budget for every
condition. Use a separate sample directory, so the held-out pilot is not
included in analysis:

```bash
python3 examples/repair_experiment.py \
    --sample --repeat 3 --max-iterations 4 --build-jobs 64 \
    --out results-qwen3-v2 --condition "${conditions[@]}" --plan

python3 examples/repair_experiment.py \
    --sample --repeat 3 --max-iterations 4 --build-jobs 64 \
    --out results-qwen3-v2 --condition "${conditions[@]}"
```

This is a fresh 648-cell experiment; the archived 30 Qwen3 cells are not
v2 completions. Resume using the identical command and output directory.
After about ten bugs, inspect both repair validation and reducer no-ops using
`triage_sweep.py`. The preregistered reducer thresholds still apply.

The 27 historical Qwen3 cells on `115149` consumed about 6.51 hours in recorded
wall time (14.5 minutes per cell). That measures one commit's run costs, not a
reliable forecast across LLVM revisions. LLVM build/test work dominates;
the H100 does not accelerate it. Time the pilot and check ccache before
scheduling the larger experiment.

## Reading historical results safely

```bash
python3 scripts/triage_sweep.py results/ --model qwen3-coder-30b
python3 scripts/inspect_patches.py results-qwen3-clean/
python3 examples/summarize_results.py results-qwen3-clean/
```

These analyse historical results only. They do not promote those records to
the v2 configuration or make the one paired bug sufficient for inference.
