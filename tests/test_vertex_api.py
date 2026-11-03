"""Native Vertex messages, refreshable auth, reservations and protocol isolation."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from urllib.error import HTTPError
import io

import pytest

from ce import vertex_api as vertex
from ce import api_budget as budget
from ce.protocol import load_protocol
from examples import repair_experiment as runner
from scripts import check_llm_endpoint as preflight
from scripts import configure_vertex

PROTOCOL = Path(__file__).resolve().parent.parent / "data/repair_v3_vertex_protocol.json"


@pytest.fixture
def configured(monkeypatch, tmp_path):
    protocol, _ = load_protocol(str(PROTOCOL))
    values = {"LAB_LLM_BACKEND": "vertex", "LAB_VERTEX_PROJECT": "better-compiler", "LAB_VERTEX_LOCATION": "global",
              "LAB_LLM_URL": protocol["endpoint_url_template"].format(project="better-compiler"),
              "LAB_LLM_MODEL": protocol["model"], "LAB_LLM_TEMP": "0.8", "LAB_LLM_MAX_TOKENS": "8192",
              "LAB_LLM_REQUEST_OPTIONS": json.dumps(protocol["request_options"]),
              "LAB_LLM_API_LIMITS": json.dumps(protocol["api_limits"]),
              "LAB_LLM_API_LEDGER": str(tmp_path / "budget.json"), "LAB_LLM_SERVING_ID": "vertex:test:global"}
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    for key in ("LAB_LLM_TOKEN", "LAB_DATASET_DIR", "LAB_LLM_MODEL_REPOSITORY", "LAB_LLM_MODEL_REVISION"):
        monkeypatch.delenv(key, raising=False)
    return protocol, tmp_path


def response(text="patch", finish="STOP"):
    return {"responseId": "native-request", "modelVersion": vertex.MODEL,
            "candidates": [{"content": {"parts": [{"text": "private thought", "thought": True}, {"text": text}]},
                            "finishReason": finish}],
            "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 10,
                              "thoughtsTokenCount": 20, "cachedContentTokenCount": 5, "totalTokenCount": 130}}


@pytest.mark.parametrize("thinking", ["MINIMAL", "LOW"])
def test_native_multi_turn_messages_and_thought_usage(configured, monkeypatch, thinking):
    monkeypatch.setenv("LAB_LLM_REQUEST_OPTIONS", json.dumps({"thinking_level": thinking}))
    calls = []
    def post(self, action, payload, timeout):
        calls.append((action, payload, timeout))
        return response(finish="MAX_TOKENS")
    monkeypatch.setattr(vertex.VertexClient, "_post", post)
    messages = [{"role": role, "content": content} for role, content in
                (("system", "rules"), ("user", "bug"), ("assistant", "first patch"), ("user", "feedback"))]
    reply = vertex.VertexClient().generate(model=vertex.MODEL, messages=messages, temperature=0.8,
                                         max_tokens=8192, thinking_level=thinking)
    payload = calls[0][1]
    assert payload["systemInstruction"] == {"parts": [{"text": "rules"}]}
    assert [part["role"] for part in payload["contents"]] == ["user", "model", "user"]
    assert payload["generationConfig"]["maxOutputTokens"] == 8192
    assert payload["generationConfig"]["thinkingConfig"] == {"thinkingLevel": thinking}
    assert reply.choices[0].message.content == "patch"
    assert reply.choices[0].finish_reason == "length"
    assert reply.usage.completion_tokens == 30
    assert reply.usage.completion_tokens_details.reasoning_tokens == 20


def test_credentials_refresh_after_expiry_without_static_cloudshell_token(monkeypatch):
    class Credentials:
        valid = False
        token = None
        refreshes = 0
        def refresh(self, request):
            self.refreshes += 1
            self.valid = True
            self.token = f"secret-{self.refreshes}"
    credentials = Credentials()
    monkeypatch.setattr(vertex, "_credentials", lambda project: credentials)
    monkeypatch.setitem(sys.modules, "google.auth.transport.requests", SimpleNamespace(Request=lambda: object()))
    assert vertex.authorization("better-compiler")["Authorization"] == "Bearer secret-1"
    vertex.authorization("better-compiler")
    assert credentials.refreshes == 1
    credentials.valid = False
    assert vertex.authorization("better-compiler")["Authorization"] == "Bearer secret-2"
    assert credentials.refreshes == 2


def test_native_http_failure_is_not_silently_retried(configured, monkeypatch):
    calls = []
    monkeypatch.setattr(vertex, "authorization", lambda project: {"Authorization": "Bearer secret"})
    def denied(request, **kwargs):
        calls.append(request)
        assert request.get_header("Authorization") == "Bearer secret"
        raise HTTPError(request.full_url, 403, "Forbidden", {}, io.BytesIO(
            b'{"error":{"message":"Permission denied"}}'))
    monkeypatch.setattr(vertex, "urlopen", denied)
    with pytest.raises(vertex.VertexHTTPError) as error:
        vertex.VertexClient().count_prompt([{"role": "user", "content": "probe"}])
    assert error.value.status_code == 403 and len(calls) == 1
    assert "secret" not in str(error.value)


def test_runner_and_preflight_use_same_vertex_profile_without_api_key(configured, monkeypatch):
    _, root = configured
    generations = []
    edit = "<<<<<<< SEARCH\n  return value - 1;\n=======\n  return value + 1;\n>>>>>>> REPLACE"
    def post(self, action, payload, timeout):
        if action == "countTokens":
            return {"totalTokens": 100}
        generations.append(payload)
        return response(text=edit)
    monkeypatch.setattr(vertex.VertexClient, "_post", post)
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=lambda **kw: pytest.fail("OpenAI SDK used")))
    model = runner.Model()
    model.check_endpoint()
    reply, stats = model.chat([{"role": "user", "content": "repair"}])
    assert reply == edit and stats["reserved_micro_usd"] == 18432
    assert stats["completion_tokens"] == 30 and stats["reasoning_tokens"] == 20
    assert stats["provider"] == "google-vertex-ai"
    assert preflight.main(["--repair-format"]) == 0
    assert len(generations) == 2
    ledger = json.loads((root / "budget.json").read_text())
    assert ledger["generation_attempts"] == 2
    assert all(payload["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "MINIMAL"}
               for payload in generations)


def test_vertex_plan_is_offline_and_refuses_wrong_location(configured, monkeypatch, capsys):
    _, root = configured
    monkeypatch.setattr(vertex, "_credentials", lambda *args: pytest.fail("credentials loaded in offline plan"))
    assert runner.main(["--protocol", str(PROTOCOL), "--out", str(root / "runs"), "--plan"]) == 0
    assert "192 cells" in capsys.readouterr().out and not (root / "runs").exists()
    monkeypatch.setenv("LAB_VERTEX_LOCATION", "us-central1")
    with pytest.raises(SystemExit) as error:
        runner.main(["--protocol", str(PROTOCOL), "--plan"])
    assert error.value.code == 2


def test_native_budget_refuses_an_overlong_prompt_before_generation(configured, monkeypatch):
    _, root = configured
    monkeypatch.setattr(vertex.VertexClient, "count_prompt", lambda *args: 24500)
    with pytest.raises(budget.APIBudgetError, match="context exhausted"):
        budget.reserve_generation([], vertex.MODEL, 8192)
    assert not (root / "budget.json").exists()


def test_vertex_cannot_silently_disable_paid_generation_accounting(configured, monkeypatch):
    monkeypatch.setenv("LAB_LLM_API_LIMITS", "{}")
    with pytest.raises(budget.APIBudgetError, match="requires the configured reservation"):
        budget.reserve_generation([], vertex.MODEL, 8192)


def test_stage_profiles_preserve_independent_pilot_and_main_ledgers(configured, monkeypatch):
    protocol, root = configured
    runtime = root / "runtime.env"
    runtime.write_text("# existing runtime")
    monkeypatch.setenv("LAB_LLM_TOKEN", "must-not-save-this")
    paths = []
    for stage in ("pilot", "main"):
        path = root / f"{stage}.env"
        argv = ["--project", "better-compiler", "--stage", stage, "--out", str(path), "--runtime-env", str(runtime)]
        assert configure_vertex.main(argv) == configure_vertex.main(argv) == 0
        content = path.read_text()
        assert "must-not-save-this" not in content
        assert f"vertex-{stage}-budget.json" in content
        assert "CLOUDSDK_CONFIG" in content
        assert f'"max_usd":{1.0 if stage == "pilot" else protocol["api_limits"]["max_usd"]}' in content
        paths.append(path)
    with pytest.raises(SystemExit):
        configure_vertex.main(["--project", "different-project", "--out", str(paths[0]), "--runtime-env", str(runtime)])


@pytest.mark.parametrize("filename", ["repair_v3_vertex_protocol.json", "repair_v3b_vertex_protocol.json"])
def test_final_vertex_analysis_uses_three_comparisons_and_rejects_incomplete_cells(configured, monkeypatch, capsys, filename):
    from examples import analyze_significance as analysis
    path = PROTOCOL.with_name(filename)
    protocol, digest = load_protocol(str(path))
    keys = ("model", "harness_version", "max_iterations", "oracle_budget", "oracle_strictness", "temperature",
            "max_completion_tokens", "require_semantic_baseline", "condition_order", "request_options",
            "api_limits", "llm_backend", "vertex_location")
    config = {key: protocol[key] for key in keys}
    for key in ("revision_context", "prompt_hash"):
        if key in protocol:
            config[key] = protocol[key]
    config.update(protocol_sha256=digest, protocol_id=protocol["id"], vertex_project="better-compiler",
                  endpoint_url=protocol["endpoint_url_template"].format(project="better-compiler"))
    rows = [{"bug_id": bug, "condition": condition, "trial": trial, "max_iterations": 4,
             "iterations": [{}] * 4, "totals": {"fixed": False}, "notes": {"experiment_config": config}}
            for bug in protocol["bugs"] for condition in protocol["conditions"] for trial in range(1, 4)]
    monkeypatch.setattr(analysis, "load_experiment_runs", lambda *args: rows)
    argv = ["unused", "--protocol", str(path), "--final", "--json"]
    assert analysis.main(argv) == 0
    assert len(json.loads(capsys.readouterr().out)["primary"]) == 3
    rows[-1]["notes"] = {"experiment_config": config, "llm_error": "budget stopped"}
    with pytest.raises(SystemExit):
        analysis.main(argv)


def test_revised_profile_has_its_own_capped_ledger_and_offline_plan(configured, monkeypatch, capsys):
    _, root = configured
    path = PROTOCOL.with_name("repair_v3b_vertex_protocol.json")
    protocol, _ = load_protocol(str(path))
    runtime = root / "runtime.env"
    runtime.write_text("# native runtime")
    profile = root / "revised.env"
    assert configure_vertex.main(["--project", "better-compiler", "--protocol", str(path),
                                  "--runtime-env", str(runtime), "--out", str(profile)]) == 0
    text = profile.read_text()
    assert '"thinking_level":"LOW"' in text
    assert '"max_usd":0.5' in text
    assert "vertex-v3b-pilot-budget.json" in text
    monkeypatch.setenv("LAB_LLM_REQUEST_OPTIONS", json.dumps(protocol["request_options"]))
    monkeypatch.setenv("LAB_LLM_API_LIMITS", json.dumps(protocol["api_limits"]))
    monkeypatch.setattr(vertex, "_credentials", lambda *args: pytest.fail("ADC accessed in plan"))
    assert runner.main(["--protocol", str(path), "--out", str(root / "main"), "--plan"]) == 0
    assert "192 cells" in capsys.readouterr().out
    assert not (root / "main").exists()
    monkeypatch.setattr(runner, "SYSTEM_PROMPT", runner.SYSTEM_PROMPT + " changed without a new protocol")
    with pytest.raises(SystemExit) as error:
        runner.main(["--protocol", str(path), "--plan"])
    assert error.value.code == 2
