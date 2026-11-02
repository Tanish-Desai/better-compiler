# Hosted Gemini inference with CPU LLVM validation

Written 2026-11-02. Use this route when the H100 has less than 10 GiB free.
It needs no vLLM process or GPU allocation. LLVM builds, Alive2, reproducers
and regression tests still use the university container's CPUs and storage.
This update has **not** made authenticated Gemini requests, verified the
operator's billing account, or executed this pilot. Local tests use mocks.

## 1. Confirm usable credits before generation

**Confirmed operator update:** AI Studio asked to buy credits after billing
setup. Check [VERTEX_CREDIT_CHECK.md](VERTEX_CREDIT_CHECK.md) before paying.
It prepares a separate Cloud Shell access probe; the Developer API adapter
below does not become a Vertex adapter by changing its base URL.

The operator reports three monthly **Gen AI & Cloud** grants of ₹955.46,
expiring August 18, 2027. If all three are unused, their combined face value
is **₹2,866.38**. Check the remaining balance, eligibility and billing-account
assignment; an original grant amount is not proof of remaining funds.

The operator has confirmed all three grants use the same billing account.
The credit type **One-time** describes the grant, not the API project's
billing plan. To find that plan, open AI Studio → **Projects**, find/import
the research project, and read **Billing Tier**. It can show Free, a paid
tier with Prepay/Postpay, or an action such as Set up billing/Set up Prepay.
If it shows Free, use Set up billing to inspect the linked account and
available plans; stop if it requires an upfront payment beyond the cash
budget. Do not assume an API key or an applied grant has enabled paid access.

Google Developer Program credits are advertised for AI Studio and Google
Cloud. In the [benefits dashboard](https://developers.google.com/profile/benefits),
apply eligible unused grants to the same research billing account. Check
Cloud Console → Billing → Credits. In [AI Studio](https://aistudio.google.com/),
import/create one research project linked to that account and create its API
key. Verify the project's actual model limits in AI Studio.
[Program benefits](https://developers.google.com/program/plans-and-pricing),
[applying credits](https://developers.google.com/profile/help/benefits),
[API rate limits](https://ai.google.dev/gemini-api/docs/rate-limits).

**Billing gate:** Google says Prepay users need a positive purchased Prepay
balance before eligible Cloud credits can be consumed. Its current minimum
purchase is $5, already beyond a ₹200–300 cash limit. If AI Studio asks for
that payment, do not assume the grants remove the upfront requirement. An
existing eligible Postpay account could avoid that gate. Google Cloud's
Vertex/Agent Platform is another credit-eligible service, but **this runbook's
adapter is Gemini Developer API, not Vertex**; Vertex requires a separately
configured endpoint/authentication/protocol. Generic $300 Welcome/free-trial
credits are excluded from Gemini Developer API usage under current rules.
[Google's billing rules](https://ai.google.dev/gemini-api/docs/billing).

Use one authorized research project. Gemini quotas are per project; extra
keys for that project do not raise them. Do not rotate friends' accounts or
keys to bypass limits. This code does not rotate accounts or fall back to
another model/provider. Never send an API key in chat, Git, screenshots or
archives.

## 2. Update code and prepare the CPU runtime

Use Bash inside the container. For an existing clone at `$HOME/better-compiler`:

```bash
cd "$HOME/better-compiler"
git status --short
git pull --ff-only https://github.com/Abhyuday-06/better-compiler.git feat/h100-sweep
mkdir -p run-provenance
```

Pull only after any running repair process has stopped. Preserve existing
results. If your clone is elsewhere, substitute its actual path in each `cd`.

For a completely fresh container, follow **sections 1–2** of
[NEW_CONTAINER_SHARED_H100.md](NEW_CONTAINER_SHARED_H100.md), including
`tmux new -s bc-setup`, native setup, resource planning and the known-patch
check on bug 115575. Skip the local vLLM/offload setup. Require the known-patch
check to report `PASS`. The initial LLVM build can take hours and needs no GPU.
Do not rerun the long setup if the existing runtime already passes these checks.

Use one LLVM repair/known-patch process at a time. The resource planner reads
container CPU/RAM limits and initially caps build jobs at eight. Keep assertions,
backends and validation enabled; do not copy the old `--build-jobs 64` command.

## 3. Configure the hosted profile without storing a key

Replace `your-research-project-id` with the actual nonsecret project ID linked
to the credits. This helper writes settings only; it cannot verify credits or
select the billing project on Google's behalf. The API key must belong to
that project. A profile with different existing settings is preserved/refused.

```bash
cd "$HOME/better-compiler"
source "$HOME/better-compiler-runtime/env.sh"
python3 scripts/configure_gemini_api.py --project your-research-project-id
source run-provenance/gemini-api.env
read -rsp 'Gemini API key: ' LAB_LLM_TOKEN; export LAB_LLM_TOKEN; printf '\n'
python3 scripts/check_llm_endpoint.py --list-models
```

Model listing makes no generation request. Require `gemini-2.5-flash` in the
listing. If unavailable, stop before generating; select and freeze a separate
model protocol rather than silently substituting one. The profile explicitly
uses temperature 0.8, 8,192 output tokens, `reasoning_effort=none` (thinking
disabled), standard service, and no tools/search. Google's
[compatibility documentation](https://ai.google.dev/gemini-api/docs/openai)
documents that thinking setting for Gemini 2.5 Flash.

Now make **one** small, budgeted format probe:

```bash
python3 scripts/check_llm_endpoint.py --repair-format \
    2>&1 | tee run-provenance/gemini-format-probe.log
```

Before the pilot, verify in Cloud Billing/AI Studio that the request is charged
to the intended service/account and that the eligible grant is applied. Billing
reports can lag beyond a day; do not interpret an immediate zero as proof of
credit coverage. Check credits/service restrictions and account setup as well.
Set an AI Studio project spend cap if available and billing notifications;
neither delayed cap enforcement nor a Cloud budget alert guarantees zero
overage. These are separate from the local reservation guard.
[Billing and reporting delays](https://ai.google.dev/gemini-api/docs/billing).

## 4. Run the excluded four-cell pilot in tmux

After billing and native validation pass:

```bash
tmux new -s bc-gemini
```

Inside the new session, source the profile and enter the key again. The profile
clears any inherited local/Qwen token; it never persists the entered API key.

```bash
cd "$HOME/better-compiler"
source run-provenance/gemini-api.env
read -rsp 'Gemini API key: ' LAB_LLM_TOKEN; export LAB_LLM_TOKEN; printf '\n'
python3 scripts/resource_plan.py --out run-provenance/gemini-build-resources.json
export BUILD_JOBS="$(python3 -c 'import json; print(json.load(open("run-provenance/gemini-build-resources.json"))["recommended_build_jobs"])')"
export LAB_LLVM_PARALLEL_LINK_JOBS=1
set -o pipefail
flock -n "$HOME/better-compiler-runtime/repair.lock" \
    python3 examples/repair_experiment.py 115575 \
    --repeat 1 --max-iterations 4 --oracle-budget 400 --require-semantic-baseline \
    --build-jobs "$BUILD_JOBS" --out results-gemini-v3-pilot \
    --condition raw-plain raw-structured llvmreduce-structured iraware-structured \
    2>&1 | tee -a run-provenance/gemini-pilot.log

python3 scripts/triage_sweep.py results-gemini-v3-pilot
python3 scripts/inspect_patches.py results-gemini-v3-pilot
python3 scripts/audit_sweep.py results-gemini-v3-pilot --repeat 1 \
    --condition raw-plain raw-structured llvmreduce-structured iraware-structured
```

Require four completed cells, usable edits and at least one full validation
pass on this previously repairable **development** bug. Inspect malformed or
truncated replies and build/verifier failures. If this gate fails, diagnose
before the main sample; retain all pilot failures. This tests feasibility and
the model floor, not main-sample performance. Record elapsed build and API
time here before claiming the full evaluation will fit in a week.

## 5. Freeze the separate 192-cell Gemini comparison

After the pilot gates pass, in the same tmux session:

```bash
python3 -m pip freeze > run-provenance/gemini-packages.txt
git rev-parse HEAD > run-provenance/gemini-source-commit.txt
sha256sum "$LAB_LLVM_ALIVE_TV" "$LAB_LLVM_LLVM_REDUCE" \
    > run-provenance/gemini-verifier-binaries.sha256

python3 examples/repair_experiment.py \
    --protocol data/repair_v3_gemini_api_protocol.json \
    --build-jobs "$BUILD_JOBS" --out results-gemini-v3 --plan

flock -n "$HOME/better-compiler-runtime/repair.lock" \
    python3 examples/repair_experiment.py \
    --protocol data/repair_v3_gemini_api_protocol.json \
    --build-jobs "$BUILD_JOBS" --out results-gemini-v3 \
    2>&1 | tee -a run-provenance/gemini-sweep.log
```

Require the plan to show **192 cells**: 16 fixed bugs × four conditions × three
trials, with at most four model turns per cell. Conditions and the three
primary contrasts match the November 1 four-arm plan. Order rotates by bug
and trial independently of outcomes. Bug-level pass@3 is the comparison unit;
BH covers all three primary paired comparisons. The dated API amendment is
in [ANALYSIS_PLAN.md](ANALYSIS_PLAN.md). Neither more trials nor dropping
difficult bugs is justified by an interim p-value.

Detach with **Ctrl-b, then d**. Reconnect with `tmux attach -t bc-gemini`;
list sessions with `tmux ls`. Tmux survives SSH disconnections, not destruction
of the container. If the process ended, source the **same profile**, enter its
key privately, restore `BUILD_JOBS`, and rerun the identical main command.
Compatible completed cells are skipped; interrupted cells are retried and
their new API attempts also consume reservations.

Do not pull code into an active sweep, switch models/thinking settings,
delete manifests/ledgers, use `--overwrite`, or start a second LLVM repair
worker. A different configuration uses a separately reported experiment.
Gemini and Qwen runs cannot be pooled as one experiment. Hosted model weights
cannot be pinned here; actual returned model IDs and response metadata are
recorded, with unavailable fields left null.

## 6. Cost, quota and context guards

At Google's checked standard text rates of **$0.30/M input and $2.50/M output**,
scaling historical v2 usage (6,936,568 input + 1,506,874 output tokens over 205
cells) to 192 cells gives approximately **$5.48**. This is an illustrative
estimate: v3 prompts, repairs and provider accounting differ. At the configured
per-call ceilings, 768 generation attempts reserve approximately **$21.39**;
pilot, probes, retries and tax are additional. Check current prices and enough
eligible remaining balance before main evaluation. The three unused grants
look sufficient for this scale, conditional on billing eligibility and actual
usage; this is not a guaranteed bill or a promise that ₹300 buys a full study.
[Current model prices](https://ai.google.dev/gemini-api/docs/pricing).

The shared `run-provenance/gemini-api-budget.json` ledger covers probes, pilot,
main and retries. Before **each** generation, the native token counter must
succeed and its prompt count plus 512 tokens of serialization margin must fit
24,576 input tokens. The ledger then reserves that entire input ceiling plus
the requested maximum output at configured rates, rounded up to microdollars.
It stops before a reservation would exceed **$25**. Hidden SDK generation
retries are disabled; ambiguous timeouts are not refunded. Failed requests can
consume quota even when not billed. The ledger is conservative accounting,
**not a measured charge, verified credit balance or provider-enforced cap**;
taxes, price changes and unexpected serialization differences are excluded.

Counter failure, context exhaustion, corrupt/locked ledger or budget exhaustion
stops the worker and preserves partial records. Such cells are **incomplete**,
not negative repair results; final analysis rejects incomplete coverage.
Do not reset the ledger to make a stopped run continue. Diagnose the cause and
retain the original records. A context or protocol change is a new study.
To read reservations:

```bash
python3 -m json.tool run-provenance/gemini-api-budget.json
```

If only free access works, first check the project's actual request/day and
input-token limits against up to 768 main generations plus pilot/retries.
Across seven days that is about 110 main requests/day; historical scaled
usage is roughly 1.1M total input/output tokens/day, while quota categories
differ by provider. Extra same-project keys do not help. Mistral has a Free
mode but its account/model allowances must be checked; no free service is
assumed to guarantee this entire workload in a week. Preserve the protocol
until a feasible separate alternative is frozen.
[Gemini quotas](https://ai.google.dev/gemini-api/docs/rate-limits),
[Mistral usage and limits](https://docs.mistral.ai/admin/billing-usage/usage-limits).

## 7. Monitor, analyze and back up

In another shell (no API key needed for analysis):

```bash
cd "$HOME/better-compiler"
source "$HOME/better-compiler-runtime/env.sh"
python3 scripts/triage_sweep.py results-gemini-v3
python3 examples/summarize_results.py results-gemini-v3 \
    --protocol data/repair_v3_gemini_api_protocol.json
# Only after all planned cells complete:
python3 examples/analyze_significance.py results-gemini-v3 \
    --protocol data/repair_v3_gemini_api_protocol.json --final
```

Also run the CPU-only witness experiment in section 3 of the shared-H100
runbook. It needs no API calls and supplies separate development-corpus evidence
on actual reduction, preservation, fallback and oracle costs, including the
promotion ablation. It does not enlarge the independent repair sample.

Archive completed outputs and the ledger regularly, and download them off the
container. After the pilot exists:

```bash
mkdir -p archives
paths=(results-gemini-v3-pilot run-provenance validation-checks)
for p in results-gemini-v3 witness-comparison-v2-cpu; do
    if [ -d "$p" ]; then paths+=("$p"); fi
done
tar --exclude='*.tmp' -czf "archives/gemini-v3-$(date -u +%Y%m%dT%H%M%SZ).tar.gz" "${paths[@]}"
```

The generated Gemini profile is nonsecret, but check other files in
`run-provenance` for credentials before archiving. On a replacement container,
restore records, manifest and the **existing ledger** before further requests;
moving its path is allowed, resetting its reserved amount is not.

Aim for setup/billing/pilot and CPU diagnostics on day 1, matched evaluation
on days 2–5, passing-patch review and corner cases on day 6, and final tables
and paper edits on day 7. This is a schedule target, subject to measured CPU
throughput. The study can test our contribution; it cannot guarantee a positive
effect, statistical significance or Q1 acceptance. Gemini's parameter count is
not established here, so these results would not demonstrate an SLM-specific
advantage. Review plausible passes for overbroad optimization suppression;
passing selected tests is not proof of semantic correctness.
