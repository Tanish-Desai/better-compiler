"""Hosted options, token reservations and failures without real API requests."""
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from ce import api_budget as budget
from ce.llm_api import model_is_listed, request_options, response_metadata
from examples import repair_experiment as runner
from ce.protocol import load_protocol
from scripts import check_llm_endpoint as preflight

PROTOCOL = Path(__file__).resolve().parent.parent / "data/repair_v3_gemini_api_protocol.json"


@pytest.fixture
def funded(monkeypatch, tmp_path):
    protocol, _ = load_protocol(str(PROTOCOL))
    for key, value in {"LAB_LLM_MODEL": protocol["model"], "LAB_LLM_URL": protocol["endpoint_url"],
                       "LAB_LLM_TOKEN": "secret-not-logged", "LAB_LLM_TEMP": "0.8",
                       "LAB_LLM_MAX_TOKENS": "8192", "LAB_LLM_SERVING_ID": "authorized-project-a",
                       "LAB_LLM_REQUEST_OPTIONS": json.dumps(protocol["request_options"]),
                       "LAB_LLM_API_LIMITS": json.dumps(protocol["api_limits"]),
                       "LAB_LLM_API_LEDGER": str(tmp_path / "budget.json")}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("LAB_DATASET_DIR", raising=False)
    monkeypatch.setattr(budget, "count_gemini_prompt", lambda *args: 200)
    return protocol, tmp_path / "budget.json"


@pytest.mark.parametrize("raw", ['[]', '{"max_tokens":1}', '{"extra_body":{"model":"other"}}',
                                  '{"reasoning_effort":"unsupported"}',
                                  '{"extra_body":{"provider":{"api_key":"secret"}}}',
                                  '{"reasoning_effort":"none","extra_body":{"google":{"thinking_config":{}}}}'])
def test_options_cannot_override_controlled_settings_or_store_credentials(monkeypatch, raw):
    monkeypatch.setenv("LAB_LLM_REQUEST_OPTIONS", raw)
    with pytest.raises(ValueError):
        request_options()


def test_google_prefix_alias_is_not_applied_to_other_providers():
    assert model_is_listed("gemini-2.5-flash", ["models/gemini-2.5-flash"],
                           "https://generativelanguage.googleapis.com/v1beta/openai/")
    assert not model_is_listed("gemini-2.5-flash", ["models/gemini-2.5-flash"], "https://other.example/v1")


def test_reservations_accumulate_across_calls_and_contain_no_key(funded):
    _, path = funded
    one = budget.reserve_generation([], "gemini-2.5-flash", 8192)
    two = budget.reserve_generation([], "gemini-2.5-flash", 8192)
    assert one["reserved_micro_usd"] == 27853
    assert two["cumulative_reserved_micro_usd"] == 55706
    ledger = json.loads(path.read_text())
    assert ledger["generation_attempts"] == 2
    assert "secret-not-logged" not in path.read_text()
    assert not path.with_suffix(".json.lock").exists()


def test_cost_ceiling_blocks_before_the_next_generation(funded, monkeypatch):
    protocol, path = funded
    limits = protocol["api_limits"] | {"max_usd": 0.03}
    monkeypatch.setenv("LAB_LLM_API_LIMITS", json.dumps(limits))
    budget.reserve_generation([], "gemini-2.5-flash", 8192)
    original = path.read_bytes()
    with pytest.raises(budget.APIBudgetError, match="exceed"):
        budget.reserve_generation([], "gemini-2.5-flash", 8192)
    assert path.read_bytes() == original


def test_oversized_prompt_is_not_billed_or_recorded_as_a_generation(funded, monkeypatch):
    _, path = funded
    monkeypatch.setattr(budget, "count_gemini_prompt", lambda *args: 24500)
    with pytest.raises(budget.APIBudgetError, match="context exhausted"):
        budget.reserve_generation([], "gemini-2.5-flash", 8192)
    assert not path.exists()


def test_wrong_project_or_profile_cannot_reset_the_ledger(funded, monkeypatch):
    _, path = funded
    budget.reserve_generation([], "gemini-2.5-flash", 8192)
    before = path.read_bytes()
    monkeypatch.setenv("LAB_LLM_SERVING_ID", "different-project")
    with pytest.raises(budget.APIBudgetError, match="different configuration"):
        budget.reserve_generation([], "gemini-2.5-flash", 8192)
    assert path.read_bytes() == before


def test_native_count_payload_and_key_header(monkeypatch):
    seen = []
    monkeypatch.setenv("LAB_LLM_TOKEN", "hidden-key")

    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return b'{"totalTokens": 123}'

    def opening(request, **kwargs):
        seen.append(request)
        return Response()
    monkeypatch.setattr(budget, "urlopen", opening)
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "before"},
                {"role": "assistant", "content": "patch"}, {"role": "user", "content": "feedback"}]
    assert budget.count_gemini_prompt(messages, "gemini-2.5-flash") == 123
    request = seen[0]
    assert "hidden-key" not in request.full_url
    payload = json.loads(request.data)["generateContentRequest"]
    assert payload["systemInstruction"]["parts"] == [{"text": "system"}]
    assert [part["role"] for part in payload["contents"]] == ["user", "model", "user"]


def test_model_and_preflight_send_same_options_and_reserve_each_attempt(funded, monkeypatch):
    _, ledger = funded
    calls = []
    response = SimpleNamespace(
        id="request-id", model="gemini-2.5-flash", system_fingerprint=None,
        choices=[SimpleNamespace(message=SimpleNamespace(content="hello"), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=200, completion_tokens=10,
                              completion_tokens_details=SimpleNamespace(reasoning_tokens=0)))
    def create(**kwargs):
        calls.append(kwargs)
        return response
    init = []
    client = SimpleNamespace(models=SimpleNamespace(list=lambda **kw: SimpleNamespace(
        data=[SimpleNamespace(id="models/gemini-2.5-flash")])),
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    def sdk(**kwargs):
        init.append(kwargs)
        return client
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=sdk))
    model = runner.Model()
    model.check_endpoint()
    reply, stats = model.chat([{"role": "user", "content": "test"}])
    assert reply == "hello" and stats["reasoning_tokens"] == 0
    assert init[0]["max_retries"] == 0
    assert preflight.main([]) == 0
    assert len(calls) == json.loads(ledger.read_text())["generation_attempts"] == 2
    assert all(call["reasoning_effort"] == "none" and call["max_tokens"] == 8192 for call in calls)


def test_api_plan_needs_no_key_and_enforces_thinking_policy(funded, monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("LAB_LLM_TOKEN")
    monkeypatch.setattr(runner, "_import_benchmark", lambda: pytest.fail("LLVM import"))
    assert runner.main(["--protocol", str(PROTOCOL), "--out", str(tmp_path / "runs"), "--plan"]) == 0
    assert "192 cells" in capsys.readouterr().out
    monkeypatch.setenv("LAB_LLM_REQUEST_OPTIONS", '{}')
    with pytest.raises(SystemExit) as error:
        runner.main(["--protocol", str(PROTOCOL), "--plan"])
    assert error.value.code == 2


def test_metadata_uses_null_for_unreported_billing_not_zero():
    assert response_metadata(SimpleNamespace())["provider_reported_cost"] is None


def test_ledger_lock_prevents_concurrent_updates(funded):
    _, path = funded
    path.with_suffix(".json.lock").write_text("other updater")
    with pytest.raises(budget.APIBudgetError, match="locked"):
        budget.reserve_generation([], "gemini-2.5-flash", 8192)
    assert not path.exists()


@pytest.mark.parametrize("saved", ["[]", "not JSON"])
def test_corrupted_ledger_stops_without_replacing_it(funded, saved):
    _, path = funded
    path.write_text(saved)
    with pytest.raises(budget.APIBudgetError, match="ledger"):
        budget.reserve_generation([], "gemini-2.5-flash", 8192)
    assert path.read_text() == saved


def test_counter_outage_withholds_generation_and_stops(funded, monkeypatch):
    _, path = funded
    def unavailable(*args):
        raise TimeoutError("counter unavailable")
    monkeypatch.setattr(budget, "count_gemini_prompt", unavailable)
    with pytest.raises(budget.APIBudgetError, match="counter failed"):
        budget.reserve_generation([], "gemini-2.5-flash", 8192)
    assert not path.exists()


def test_profile_never_stores_inherited_credentials_and_refuses_project_change(monkeypatch, tmp_path):
    from scripts import configure_gemini_api as config
    runtime = tmp_path / "env.sh"
    runtime.write_text("# native runtime")
    profile = tmp_path / "api.env"
    monkeypatch.setenv("LAB_LLM_TOKEN", "private-inherited-key")
    argv = ["--project", "research-project-a", "--runtime-env", str(runtime), "--out", str(profile)]
    assert config.main(argv) == config.main(argv) == 0
    content = profile.read_text()
    assert "private-inherited-key" not in content
    assert "unset LAB_LLM_TOKEN" in content
    assert "reasoning_effort" in content
    with pytest.raises(SystemExit):
        config.main(["--project", "research-project-b"] + argv[2:])
    assert profile.read_text() == content


def test_paid_retry_is_reserved_and_exhaustion_never_calls_provider(funded, monkeypatch):
    protocol, path = funded
    monkeypatch.setenv("LAB_LLM_API_LIMITS", json.dumps(protocol["api_limits"] | {"max_usd": 0.06}))
    calls = []
    def unavailable(**kwargs):
        calls.append(kwargs)
        raise TimeoutError("ambiguous generation timeout")
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=unavailable)))
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=lambda **kw: client))
    monkeypatch.setattr(runner.time, "sleep", lambda *args: None)
    with pytest.raises(budget.APIBudgetError, match="exceed"):
        runner.Model().chat([{"role": "user", "content": "fix"}])
    assert len(calls) == json.loads(path.read_text())["generation_attempts"] == 2


def test_budget_stop_preserves_partial_repair_without_completing_cell(funded, monkeypatch):
    from test_repair_pipeline import FakeEnvironment, args, model
    from ce.benchmark import record_is_complete
    _, path = funded
    monkeypatch.setattr(runner, "Env", FakeEnvironment)
    monkeypatch.setattr(runner, "bug_hunk", lambda env: ("file.cpp", "return 0;"))
    monkeypatch.setattr(runner, "apply_patch", lambda *args: True)
    llm = model()
    original = llm.chat
    def chat(messages):
        if any(message["role"] == "assistant" for message in messages):
            raise budget.APIBudgetError("exhausted")
        return original(messages)
    llm.chat = chat
    run = runner.repair("111", "baseline", args(), llm)
    assert len(run.iterations) == 1 and run.notes["api_budget_error"] == "exhausted"
    record = run.write(str(path.parent / "runs"))
    assert not record_is_complete(record)


@pytest.mark.parametrize("invalid", ["missing", "thinking"])
def test_final_hosted_analysis_requires_all_cells_and_exact_thinking_policy(funded, monkeypatch, capsys, invalid):
    from examples import analyze_significance as analysis
    protocol, _ = funded
    _, digest = load_protocol(str(PROTOCOL))
    fields = ("model", "harness_version", "max_iterations", "oracle_budget", "oracle_strictness",
              "temperature", "max_completion_tokens", "require_semantic_baseline", "condition_order",
              "endpoint_url", "request_options", "api_limits")
    config = {key: protocol[key] for key in fields}
    config.update(protocol_id=protocol["id"], protocol_sha256=digest)
    rows = [{"bug_id": bug, "condition": condition, "trial": trial, "max_iterations": 4,
             "iterations": [{}] * 4, "totals": {"fixed": False}, "notes": {"experiment_config": config}}
            for bug in protocol["bugs"] for condition in protocol["conditions"] for trial in range(1, 4)]
    monkeypatch.setattr(analysis, "load_experiment_runs", lambda *args: rows)
    argv = ["unused", "--protocol", str(PROTOCOL), "--final", "--json"]
    assert analysis.main(argv) == 0
    result = json.loads(capsys.readouterr().out)
    assert len(result["primary"]) == 3
    assert len(result["coverage"]["complete_bugs"]) == 16
    if invalid == "missing":
        rows.pop()
    else:
        config["request_options"] = {"reasoning_effort": "high"}
    with pytest.raises(SystemExit) as error:
        analysis.main(argv)
    assert error.value.code == 2
