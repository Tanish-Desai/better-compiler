# Vertex repair study on the existing H100 container

Written 2026-11-03. The operator's project `better-compiler` passed a native
Vertex smoke check: model `gemini-3.1-flash-lite`, 5 input tokens, 2 visible
output tokens, normal STOP, ON_DEMAND traffic. This establishes account/model
access from Cloud Shell. It is not a repair result, H100 connectivity test,
quota guarantee or proof that Cloud grants paid for it.

The operator also confirmed the H100 native runtime is installed and the
known-patch check for 115575 passed. **Keep that runtime. No LLVM reinstallation,
vLLM process or free GPU memory is required.** Use the container's CPUs for
builds/Alive2/regression tests and Vertex for generation.

One grant has ₹952.30 remaining out of ₹955.46. The other grants' remaining
balances have not been confirmed. The pilot reserves at most $1; the separate
main ledger reserves at most $18. These are local accounting limits at the
configured prices, not measured bills. Before main evaluation, verify the
combined eligible balance covers main plus any remaining pilot usage and
taxes/other usage. An already reduced grant is not proof that this particular
probe consumed it.

## 1. Update the existing clone and optional Python dependencies

Run these commands in **Bash on the H100 container**, not Cloud Shell. Replace
`$HOME/better-compiler` with the actual existing clone path if different.
Ensure no repair process currently owns the LLVM checkout before pulling.

Start a persistent session, then run the setup commands inside it:

```bash
tmux new -s bc-vertex
```

```bash
cd "$HOME/better-compiler"
git status --short
git pull --ff-only https://github.com/Tanish-Desai/better-compiler.git feat/h100-sweep
source "$HOME/better-compiler-runtime/env.sh"
python3 -m pip install -r requirements-hosted.txt
mkdir -p run-provenance
python3 scripts/configure_vertex.py --project better-compiler --stage pilot
source run-provenance/vertex-pilot.env
```

This writes a nonsecret, immutable profile and clears stale Qwen/API-key
settings. It selects a dedicated Google config directory at
`$HOME/.config/gcloud-better-compiler`, outside the repository. It does not
log in, spend funds or save credentials in the profile. Do not reuse a previous
experiment's output directory or reset its ledger.

## 2. Install gcloud if absent; authenticate on the H100

Check whether `gcloud` is installed:

```bash
command -v gcloud
```

If absent, as root in the Ubuntu container, install it using Google's apt
repository. These steps add the Cloud CLI, not a GPU runtime or new LLVM build:

```bash
apt-get update
apt-get install -y ca-certificates gnupg curl
curl --fail --silent --show-error --location \
    https://packages.cloud.google.com/apt/doc/apt-key.gpg \
    -o /tmp/better-compiler-cloud-apt-key.gpg &&
gpg --dearmor --yes -o /usr/share/keyrings/cloud.google.gpg \
    /tmp/better-compiler-cloud-apt-key.gpg
printf '%s\n' 'deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main' \
    > /etc/apt/sources.list.d/google-cloud-sdk.list
apt-get update
apt-get install -y google-cloud-cli
```

Stop if installation fails. Avoid duplicate Cloud SDK apt source entries if
this container already had a custom installation.
[Official installation](https://docs.cloud.google.com/sdk/docs/install-sdk#deb).

Now authenticate **Application Default Credentials (ADC)** in this same H100
session. A Cloud Shell login does not authenticate the university container.

```bash
source run-provenance/vertex-pilot.env
gcloud config set project better-compiler
gcloud auth application-default login --no-launch-browser
gcloud auth application-default set-quota-project better-compiler
```

Open the printed Google authorization URL on your laptop, sign in to the same
account that passed the Cloud Shell probe, then paste the returned authorization
code into the **H100 terminal** when prompted. Do not paste codes or credential
files in chat/Git. The runner obtains short-lived access tokens from ADC and
refreshes them before requests as needed. Copying one Cloud Shell access token
would expire during a long sweep and is not the setup used here.
[Headless ADC login](https://docs.cloud.google.com/sdk/gcloud/reference/auth/application-default/login).

## 3. Run the real edit-format preflight

```bash
python3 scripts/check_llm_endpoint.py --repair-format \
    2>&1 | tee run-provenance/vertex-format-probe.log
```

Require `endpoint and v3 repair format OK`. This checks a real SEARCH/REPLACE
edit using the runner's system prompt. It consumes one generation reservation
from the **pilot** ledger. Native Vertex does not expose an OpenAI-style model
listing here; use this access/format check instead of `--list-models`.

Check Cloud Billing → Reports/Credits for eligible grant application and the
selected project's service/SKU. Reports and credit offsets may lag; an immediate
zero cost for the tiny smoke probe cannot establish coverage. Inspect active
grant balance and service restrictions as well. The current code makes no
claim about verified credits, taxes or a zero cash bill.

## 4. Run four development cells in tmux

Continue inside the existing `bc-vertex` session. If detached, reconnect with
`tmux attach -t bc-vertex` before running:

```bash
cd "$HOME/better-compiler"
source run-provenance/vertex-pilot.env
python3 scripts/resource_plan.py --out run-provenance/vertex-build-resources.json
export BUILD_JOBS="$(python3 -c 'import json; print(json.load(open("run-provenance/vertex-build-resources.json"))["recommended_build_jobs"])')"
export LAB_LLVM_PARALLEL_LINK_JOBS=1
set -o pipefail
flock -n "$HOME/better-compiler-runtime/repair.lock" \
    python3 examples/repair_experiment.py 115575 \
    --repeat 1 --max-iterations 4 --oracle-budget 400 --require-semantic-baseline \
    --build-jobs "$BUILD_JOBS" --out results-vertex-v3-pilot \
    --condition raw-plain raw-structured llvmreduce-structured iraware-structured \
    2>&1 | tee -a run-provenance/vertex-pilot.log

python3 scripts/triage_sweep.py results-vertex-v3-pilot
python3 scripts/inspect_patches.py results-vertex-v3-pilot
python3 scripts/audit_sweep.py results-vertex-v3-pilot --repeat 1 \
    --condition raw-plain raw-structured llvmreduce-structured iraware-structured
python3 -m json.tool run-provenance/vertex-pilot-budget.json
```

Require four completed cells, usable edits, the already passed known-patch
check, and at least one full validation pass on this excluded development bug.
If the pilot fails, diagnose it and retain its failures before the main sample.
The smoke reply `OK.` alone does not establish a compiler-repair model floor.
Inspect build failures, malformed/truncated edits and verifier fallbacks.

For the exact patch and test diagnostics from each failed turn, with no new
builds or model requests:

```bash
python3 scripts/diagnose_repairs.py results-vertex-v3-pilot --bug 115575 \
    | tee run-provenance/vertex-pilot-diagnostics.txt
python3 scripts/diagnose_repairs.py results-vertex-v3-pilot --bug 115575 --json \
    > run-provenance/vertex-pilot-diagnostics.json
```

The console report is bounded; JSON preserves full saved replies, patches and
feedback. An invalid-format label can include a SEARCH anchor that does not
match the original excerpt. Missing code fences are expected for edit blocks.
The final saved patch is not necessarily the earlier one that passed the
reproducer, so use each turn's own diagnostics. Keep the original records.

Record pilot API/build timings; use those to judge whether the study fits the
remaining week. The planner initially caps compile jobs at eight and accounts
for container CPU/RAM limits. No GPU availability is needed.

Detach with **Ctrl-b, then d**. Reconnect using `tmux attach -t bc-vertex`.
Tmux survives SSH disconnections, not replacement of the container. Source the
same profile after restarting a shell. No API-key entry is needed.

## 5. Main comparison only after pilot and balance gates pass

The separately frozen `data/repair_v3_vertex_protocol.json` retains the same
16 bugs, four feedback arms, three trials, four-turn maximum and 400-call
oracle budget as the November four-arm design. There are **192 cells**, at most
768 successful generation replies before retries. Main and pilot have different
output directories, profiles and persistent ledgers. They use the same model,
temperature, thinking policy and per-request limits.

In the tmux session, after confirming enough combined eligible credits:

```bash
python3 scripts/configure_vertex.py --project better-compiler --stage main
source run-provenance/vertex-main.env
export BUILD_JOBS="$(python3 -c 'import json; print(json.load(open("run-provenance/vertex-build-resources.json"))["recommended_build_jobs"])')"
export LAB_LLVM_PARALLEL_LINK_JOBS=1
python3 -m pip freeze > run-provenance/vertex-packages.txt
git rev-parse HEAD > run-provenance/vertex-source-commit.txt
sha256sum "$LAB_LLVM_ALIVE_TV" "$LAB_LLVM_LLVM_REDUCE" \
    > run-provenance/vertex-verifier-binaries.sha256

python3 examples/repair_experiment.py \
    --protocol data/repair_v3_vertex_protocol.json \
    --build-jobs "$BUILD_JOBS" --out results-vertex-v3 --plan

set -o pipefail
flock -n "$HOME/better-compiler-runtime/repair.lock" \
    python3 examples/repair_experiment.py \
    --protocol data/repair_v3_vertex_protocol.json \
    --build-jobs "$BUILD_JOBS" --out results-vertex-v3 \
    2>&1 | tee -a run-provenance/vertex-sweep.log
```

Require the offline plan to show 192 cells. On resume, source the **main**
profile and rerun the identical command. Keep the same packages/code/profile
for the entire experiment. Compatible completed cells are skipped; interrupted
cells restart and new attempts consume further reservations. Archive partial
records before resuming an interrupted cell. Never use `--overwrite`, delete
manifests/ledgers, add selected trials, drop hard bugs, or change thinking/model
because interim results look weak. One repair/known-patch worker owns the LLVM
checkout via the shared lock.

## 6. Accounting, interpretation and backup

The native transport uses the same global model route that passed the smoke
check. It sends full text message history and the actual system instruction,
temperature 0.8, an 8,192-token maximum output, and **MINIMAL** thinking.
Thinking is not disabled. Returned thought-token counts, when available, are
included in completion usage and recorded separately. MAX_TOKENS maps to a
truncated reply that cannot be applied. The transport has no hidden generation
retries. Native token counting must succeed before generation and count plus
512 tokens must fit the fixed 24,576-token input allowance.

At checked global standard prices of **$0.25/M input, $1.50/M output**, each
8,192-token generation attempt reserves **$0.018432**, using the entire input
ceiling rather than actual prompt length. The 768 planned main requests reserve
**$14.16** at maximum allowances. Main is stopped before its ledger would
exceed $18; pilot/probes stop before their separate ledger exceeds $1. Retries
and ambiguous timeouts retain reservations. Both ledgers must be preserved.
These limits exclude changed prices, taxes and unexpected provider accounting;
they are not provider-enforced billing caps or confirmed credit coverage.
[Model pricing](https://cloud.google.com/model-garden/pricing),
[native token-count fields](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/reference/rest/v1/projects.locations.publishers.models/countTokens).

Scaling historical v2 token density to 192 cells gives an illustrative $3.74
generation-cost estimate at these rates. This is not a prediction: model
behavior, v3 prompts, minimal thinking and early stopping differ. Do not assume
the one confirmed ₹952.30 grant covers maximum allowances. Confirm the other
two active balances before main evaluation, set billing notifications, and
retain headroom for unrelated account usage. A Cloud budget alert is not a
hard spending limit.

Counter/context/budget stops preserve incomplete records and halt the worker.
These are not completed negative repairs. Final analysis requires full matched
coverage and the exact backend/project/endpoint/thinking/protocol metadata.
In another shell (analysis needs the Python runtime, not ADC):

```bash
cd "$HOME/better-compiler"
source "$HOME/better-compiler-runtime/env.sh"
python3 scripts/triage_sweep.py results-vertex-v3
python3 examples/summarize_results.py results-vertex-v3 \
    --protocol data/repair_v3_vertex_protocol.json
# Only after all planned cells complete:
python3 examples/analyze_significance.py results-vertex-v3 \
    --protocol data/repair_v3_vertex_protocol.json --final
```

The three primary comparisons are IR-aware structured versus raw structured,
IR-aware structured versus LLVM-reduce structured, and raw structured versus
raw plain. Compare bug-level pass@3 with exact paired tests and BH across all
three, using counterbalanced condition order. Keep Vertex, Qwen and Developer
API results separate. Hosted weights/parameter count/training exposure are
unknown; no SLM-specific or contamination-free claim follows. Review plausible
passes for overbroad optimization suppression and missed semantic corner cases.
The 16-bug sample includes prior diagnostic exposure of 115149. No positive
effect, significance or journal acceptance is guaranteed.

Also collect the no-LLM CPU witness comparison documented in section 3 of
[NEW_CONTAINER_SHARED_H100.md](NEW_CONTAINER_SHARED_H100.md). It supplies
separate development-corpus reduction/preservation/fallback evidence and an
operand-promotion ablation. It does not enlarge the independent repair sample.

After the pilot exists, archive records and both available ledgers regularly:

```bash
mkdir -p archives
paths=(results-vertex-v3-pilot run-provenance validation-checks)
for p in results-vertex-v3 witness-comparison-v2-cpu; do
    if [ -d "$p" ]; then paths+=("$p"); fi
done
tar --exclude='*.tmp' -czf "archives/vertex-v3-$(date -u +%Y%m%dT%H%M%SZ).tar.gz" "${paths[@]}"
```

Download archives off the container. Do not archive the private Google config
directory or credential files. On a replacement container, restore the exact
records/manifests/ledgers and compatible runtime before further requests, then
authenticate ADC again. Local credentials are not a substitute for data backups.
