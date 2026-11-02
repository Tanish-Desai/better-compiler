# Check the Cloud-credit route when AI Studio demands prepayment

**2026-11-03 update:** The operator passed this native Vertex smoke check.
The [Vertex H100 runbook](VERTEX_H100_RUNBOOK.md) now provides the repair adapter,
refreshable ADC setup, a $1-limited pilot and a separate 192-cell protocol.
The probe instructions below remain an account-access diagnostic.

Written 2026-11-02 after the operator confirmed AI Studio requires buying
credits. That is consistent with its Prepay billing gate. Existing eligible
Cloud grants do not remove AI Studio's requirement for purchased Prepay funds.
Google states the Gemini API Prepay transition does not change other Google
Cloud services' Postpay billing. An active Cloud billing account with eligible
GenAI & Cloud grants may therefore support Vertex AI/Agent Platform usage
without purchasing the AI Studio wallet balance.
[AI Studio billing](https://ai.google.dev/gemini-api/docs/billing),
[program benefits and eligible services](https://developers.google.com/profile/help/benefits).

This is a check of account access and one model, **not a repair experiment**.
The adapter/protocol in `HOSTED_GEMINI_WEEK.md` is for Gemini Developer API.
Use `VERTEX_H100_RUNBOOK.md` for the separate native Vertex adapter and protocol;
changing the older protocol's URL/model by hand is not equivalent. No
authenticated Vertex requests were made from this development workspace.

## 1. Select the credited Cloud project

Open [Google Cloud Console](https://console.cloud.google.com/). Use the project
selector at the top to choose the same research project linked to the billing
account holding the three grants. Confirm the link in Billing → Account
management and inspect Billing → Credits for the remaining balance and eligible
services. The displayed One-time credit type is a grant type, not an API plan.

Use [Vertex AI Studio](https://console.cloud.google.com/vertex-ai/studio)
(it may appear as Agent Studio/Agent Platform in the current console), not
AI Studio's Buy credits page. Enable the Vertex AI API when requested. Do not
deploy a VM, GPU, provisioned-throughput reservation or Model Garden endpoint.
This probe calls Google's existing shared model service.

## 2. Run a tiny probe in Cloud Shell

Click **Activate Cloud Shell** (`>_`) at the top of Cloud Console. This is a
short account check in Google's terminal, not the H100 container. Click
Authorize if Cloud Shell requests access to your selected Google account.
Replace `your-research-project-id` with the actual project ID visible in the
project selector. The ID is not an API key or a billing-account number.

```bash
export PROJECT_ID="your-research-project-id"
gcloud config set project "$PROJECT_ID"
gcloud services enable aiplatform.googleapis.com --project "$PROJECT_ID"
curl --fail --location --output "$HOME/bc-probe-vertex.py" \
    https://raw.githubusercontent.com/Tanish-Desai/better-compiler/04a8f198d3ab6d71c8d3d5eb0a135a6089fb1a1c/scripts/probe_vertex.py &&
python3 "$HOME/bc-probe-vertex.py" --project "$PROJECT_ID"
```

The download uses a fixed commit in the public Tanish-Desai mirror. The
Abhyuday-06 mirror is private, so unauthenticated raw-file downloads from it
return 404. Both mirrors contain the same probe. If the download fails, `&&`
prevents Python from attempting to run a missing file.

The default command only counts tokens for the toy prompt. It uses gcloud's
current OAuth session without printing or storing its access token. No API key
or extra Python installation is needed. A success here tests token-counter
access; it does not prove generation, quota sufficiency or credit application.

If that succeeds, make one explicitly requested generation:

```bash
python3 "$HOME/bc-probe-vertex.py" --project "$PROJECT_ID" --generate
```

This counts the prompt again and sends **one** generation, with a maximum of
128 output tokens and minimal thinking. It can incur a small Cloud usage
charge, consumed from eligible grants only if billing applies them. The output
should include visible text `OK`, the returned model version and token usage.
Check the Vertex/Agent Platform service entry and credit offsets in Cloud
Billing; reports can be delayed. Do not interpret an immediate zero charge as
proof that credits were applied. Do not purchase AI Studio prepaid credits
merely to perform this Vertex check.

The probe uses `gemini-3.1-flash-lite` on `global`, listed as GA with retirement
no earlier than May 7, 2027. Vertex's `gemini-2.5-flash` retired on October 20,
2026, even though the Developer API can have a different lifecycle. Thus the
existing Developer API study is not simply transferable to Vertex. Checked
Vertex standard global text rates are $0.25/M input and $1.50/M output; minimal
thinking is not a claim that thinking is disabled.
[Model lifecycle](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/model-versions),
[model capabilities](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-1-flash-lite),
[Vertex model pricing](https://cloud.google.com/model-garden/pricing),
[REST token counting](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/capabilities/get-token-count).

## 3. Interpret the output before configuring research runs

- **Success with visible text:** account/model access works. Verify grant
  coverage, then configure the separate Vertex adapter and pilot before the
  matched main study. This toy response is not repair evidence.
- **403 / SERVICE_DISABLED:** API is not enabled, activation is still
  propagating, or the selected account lacks permission. Resolve the exact
  reported cause; enabling the API requires Service Usage permissions, while
  inference commonly uses the Vertex AI User role (`roles/aiplatform.user`).
- **Billing disabled / account issue:** inspect the selected project's linked
  Cloud billing account. Grants alone do not prove that the account is active.
- **429:** retain the model and exact error; available quota/capacity must be
  assessed before committing to a week-long study. Do not rotate accounts.
- **404:** retain the model and endpoint error; do not silently substitute a
  different model in a research run.

Share only the script's printed results or error, with project identifiers
redacted if desired. Do not share authentication tokens, API keys or credential
files. The H100 remains useful for LLVM CPU validation and the existing
CPU-only witness comparison while account access is checked. Cloud Shell is
only used for this small probe.
