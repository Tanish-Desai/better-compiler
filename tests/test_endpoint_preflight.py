"""Preflight uses the actual v3 protocol and fails before a costly pilot."""

import sys
from types import SimpleNamespace

import pytest

from examples.repair_experiment import SYSTEM_PROMPT
from scripts import check_llm_endpoint as preflight
from scripts.triage_sweep import classify, patch_statuses, turn_level
from scripts import inspect_patches
import json


VALID_EDIT = "<<<<<<< SEARCH\n  return value - 1;\n=======\n  return value + 1;\n>>>>>>> REPLACE"
UNCHANGED_EDIT = "<<<<<<< SEARCH\n  return value - 1;\n=======\n  return value - 1;\n>>>>>>> REPLACE"


@pytest.fixture
def endpoint(monkeypatch):
    monkeypatch.setenv("LAB_LLM_URL", "http://test.local/v1")
    monkeypatch.setenv("LAB_LLM_TOKEN", "test-secret")
    monkeypatch.setenv("LAB_LLM_MODEL", "test-model")
    monkeypatch.setenv("LAB_LLM_TEMP", "0.8")
    monkeypatch.delenv("LAB_LLM_MAX_TOKENS", raising=False)
    calls = []
    model_calls = []

    def install(replies, *, served="test-model"):
        iterator = iter(replies)

        def create(**kwargs):
            calls.append(kwargs)
            text, reason = next(iterator)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=text), finish_reason=reason)],
                usage=SimpleNamespace(prompt_tokens=100, completion_tokens=30),
            )

        def models(**kwargs):
            model_calls.append(kwargs)
            return SimpleNamespace(data=[SimpleNamespace(id=served)])

        client = SimpleNamespace(models=SimpleNamespace(list=models),
                                 chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=lambda **kwargs: client))
        return calls, model_calls

    return install


def test_repair_preflight_sends_actual_prompt_and_explicit_default_budget(endpoint, capsys):
    calls, models = endpoint([(VALID_EDIT, "stop")])
    assert preflight.main(["--repair-format"]) == 0
    assert models == [{"timeout": 30}]
    assert calls[0]["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert preflight.REPAIR_SOURCE in calls[0]["messages"][1]["content"]
    assert calls[0]["max_tokens"] == 8192
    assert calls[0]["temperature"] == 0.8
    assert "test-secret" not in capsys.readouterr().out


def test_explicit_output_budget_matches_runner_environment(endpoint, monkeypatch):
    calls, _ = endpoint([(VALID_EDIT, "stop")])
    monkeypatch.setenv("LAB_LLM_MAX_TOKENS", "4096")
    assert preflight.main(["--repair-format"]) == 0
    assert calls[0]["max_tokens"] == 4096


@pytest.mark.parametrize("value", ["0", "-1", "not-an-integer"])
def test_invalid_output_budget_stops_before_any_api_calls(endpoint, monkeypatch, value):
    calls, models = endpoint([])
    monkeypatch.setenv("LAB_LLM_MAX_TOKENS", value)
    with pytest.raises(SystemExit) as error:
        preflight.main(["--repair-format"])
    assert error.value.code == 2
    assert calls == []
    assert models == []


@pytest.mark.parametrize("text,reason", [
    (VALID_EDIT, "length"),  # Even a complete first block cannot make a truncated reply safe.
    (None, "stop"),
    ("", "stop"),
    ("```cpp\nreturn value + 1;\n```", "stop"),  # Legacy whole-window response.
    (VALID_EDIT.removesuffix(">>>>>>> REPLACE"), "stop"),
    (UNCHANGED_EDIT, "stop"),
    (VALID_EDIT.replace("value + 1", "value + 2"), "stop"),
    (VALID_EDIT.replace("value - 1", "value - 9"), "stop"),
])
def test_unusable_or_incorrect_repair_reply_rejects_preflight(endpoint, capsys, text, reason):
    endpoint([(text, reason)])
    assert preflight.main(["--repair-format"]) == 1
    assert "FAIL:" in capsys.readouterr().err


def test_each_reply_must_be_usable_not_just_one_of_the_repeats(endpoint):
    calls, _ = endpoint([(VALID_EDIT, "stop"), ("", "stop")])
    assert preflight.main(["--repair-format", "--repeat", "2"]) == 1
    assert len(calls) == 2


def test_identical_constrained_answers_are_not_called_broken_sampling(endpoint, capsys):
    endpoint([(VALID_EDIT, "stop")] * 3)
    assert preflight.main(["--repair-format", "--repeat", "3"]) == 0
    output = capsys.readouterr()
    assert "1 distinct reply/replies out of 3" in output.out
    assert "do not establish that sampling is broken" in output.out
    assert "WARNING" not in output.err


def test_unavailable_model_fails_before_generation(endpoint, capsys):
    calls, _ = endpoint([], served="different-model")
    assert preflight.main(["--repair-format"]) == 1
    assert calls == []
    assert "ERROR: LAB_LLM_MODEL" in capsys.readouterr().err


def test_invalid_format_and_truncation_are_unapplied_with_separate_counters():
    run = {"iterations": [
        {"patch": status, "llm": {"completion_tokens": 30}}
        for status in ("invalid_format", "truncated", "unchanged", "applied")
    ], "certificate": {"build_count": 2, "build_failure_count": 0}}
    counts = turn_level([run])
    assert counts["turns"] == 4
    assert counts["turns that reached a build"] == 1
    assert counts["turns that returned the code unchanged"] == 1
    assert counts["turns the patch did not apply"] == 2
    assert counts["turns with invalid patch format"] == 1
    assert counts["turns with truncated replies"] == 1
    assert counts["turns with unmatched source"] == 0
    assert classify(run) == "no fix found (some turns unapplied)"


@pytest.mark.parametrize("status,bucket", [
    ("truncated", "all replies hit output limit"),
    ("invalid_format", "invalid patch format"),
    ("unchanged", "returned the code unchanged"),
    ("mismatch", "patch never applied"),
])
def test_entire_run_without_source_edit_has_specific_reason(status, bucket):
    run = {"iterations": [{"patch": status, "llm": {"completion_tokens": 30}}] * 4,
           "certificate": {"build_count": 1, "build_failure_count": 0}}
    assert classify(run) == bucket


def test_v2_counts_are_preserved_when_new_statuses_are_absent():
    run = {"iterations": [{"patch": "applied", "llm": {"completion_tokens": 30}},
                          {"patch": "unchanged", "llm": {"completion_tokens": 30}}],
           "certificate": {"build_count": 2, "build_failure_count": 1}}
    counts = turn_level([run])
    assert counts["turns that reached a build"] == 1
    assert counts["build failures"] == 1
    assert counts["turns the patch did not apply"] == 0
    assert counts["turns that returned the code unchanged"] == 1
    assert classify(run) == "build failed"


def inspection_run(replies, statuses, *, explicit=True):
    notes = {"experiment_config": {"patch_format": "search-replace-v1"}} if explicit else {}
    return {"bug_id": "111", "condition": "raw-plain", "trial": 1, "notes": notes,
            "iterations": [{"patch": status} for status in statuses],
            "certificate": {"log": {"messages": [
                {"role": "user", "content": "Please modify the following code in file.cpp to fix the bug:\n"
                 f"```cpp\n{preflight.REPAIR_SOURCE.rstrip()}\n```"},
                *[{"role": "assistant", "content": reply} for reply in replies],
            ]}}}


def test_v3_inspection_diffs_resulting_source_not_search_replace_text(monkeypatch, capsys):
    run = inspection_run([VALID_EDIT, UNCHANGED_EDIT], ["applied", "unchanged"])
    monkeypatch.setattr(inspect_patches, "load_experiment_runs", lambda *a: [run])
    assert inspect_patches.main(["unused", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["median_lines_changed"] == 1  # Two changed diff lines, then zero.
    assert report["returned_the_hunk_unchanged"] == 1
    assert report["replies_unapplied"] == 0
    assert inspect_patches.proposed_source(run, preflight.REPAIR_SOURCE, 0, VALID_EDIT)[0] == preflight.REPAIR_EXPECTED


def test_malformed_and_truncated_v3_outputs_do_not_inflate_edit_sizes_or_similarity(monkeypatch, capsys):
    valid = inspection_run([VALID_EDIT], ["applied"])
    broken = inspection_run(["<<<< broken", VALID_EDIT], ["invalid_format", "truncated"])
    broken["condition"] = "iraware-plain"
    monkeypatch.setattr(inspect_patches, "load_experiment_runs", lambda *a: [valid, broken])
    assert inspect_patches.main(["unused", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["median_lines_changed"] == 2
    assert report["replies_unapplied"] == 2
    assert report["replies_with_invalid_patch_format"] == 1
    assert report["replies_truncated"] == 1
    assert report["pairs_across_conditions"] == 0


def test_v3_missing_format_is_distinct_while_historical_full_hunk_still_works():
    full_hunk = f"```cpp\n{preflight.REPAIR_EXPECTED}```"
    v3 = inspection_run([full_hunk], [None])
    assert inspect_patches.proposed_source(v3, preflight.REPAIR_SOURCE, 0, full_hunk) == (None, "invalid_format")
    historical = inspection_run([full_hunk], [None], explicit=False)
    code, error = inspect_patches.proposed_source(historical, preflight.REPAIR_SOURCE, 0, full_hunk)
    assert error is None
    assert code == preflight.REPAIR_EXPECTED.rstrip()


def test_historical_malformed_reply_does_not_crash_noop_recovery():
    reply = "```cpp\nreturn 0;\n```\nprose\n```cpp\nreturn 1;\n```"
    run = inspection_run([reply], [None], explicit=False)
    assert patch_statuses(run) == [None]
