"""Validation-stage accounting and the human-patch diagnostic without LLVM."""

import importlib.util
from pathlib import Path
import sys
import types

import pytest

from scripts.check_validation import check_known_fix
from scripts.triage_sweep import classify, patch_statuses

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def lab(monkeypatch):
    helper = types.ModuleType("llvm_helper")
    monkeypatch.setitem(sys.modules, "llvm_helper", helper)
    monkeypatch.setitem(sys.modules, "dateparser", types.ModuleType("dateparser"))
    spec = importlib.util.spec_from_file_location("validation_lab", ROOT / "llvm-apr-benchmark/scripts/lab_env.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, helper


def environment(module):
    env = module.Environment.__new__(module.Environment)
    env.data = {"tests": [], "lit_test_dir": []}
    env.test_commit = "test"
    env.bug_type = "miscompilation"
    env.max_build_jobs = 1
    env.fast_check_count = env.full_check_count = 0
    env.interaction_time_compensation_enter = 0
    env.interaction_time_compensation = 0
    env.fast_check_pass = env.full_check_pass = False
    env.last_validation = {}
    env.build = lambda: (True, "built")
    return env


def test_later_build_failure_does_not_inherit_earlier_reproducer_success(lab):
    module, helper = lab
    env = environment(module)
    helper.verify_test_group = lambda **kwargs: (True, "reproducer passed")
    helper.verify_lit = lambda **kwargs: (False, "FAIL: LLVM :: some-test.ll")
    assert not env.check_full()[0]
    assert env.last_validation["reproducer_pass"]
    assert env.last_validation["failure_kind"] == "test_failure"
    env.build = lambda: (False, "compiler error")
    assert not env.check_full()[0]
    assert env.fast_check_pass  # Historical aggregate is deliberately sticky.
    assert not env.last_validation["reproducer_pass"]
    assert env.last_validation["stage"] == "build"


@pytest.mark.parametrize("log,kind", [
    ("Command llvm-lit timed out after 300 seconds", "timeout"),
    ("UNRESOLVED: missing FileCheck", "infrastructure"),
    ("FAIL: LLVM :: foo.ll", "test_failure"),
])
def test_lit_failure_kind_is_not_always_a_regression(lab, log, kind):
    module, helper = lab
    env = environment(module)
    helper.verify_test_group = lambda **kwargs: (True, [])
    helper.verify_lit = lambda **kwargs: (False, log)
    assert not env.check_full()[0]
    assert env.last_validation["failure_kind"] == kind


def test_triage_uses_any_turn_reaching_lit_even_if_final_turn_failed_build():
    run = {"iterations": [
        {"patch": "applied", "validation": {"stage": "regression", "failure_kind": "test_failure"}},
        {"patch": "applied", "validation": {"stage": "build", "failure_kind": "error"}},
    ]}
    assert classify(run) == "broke regression tests"


def test_endpoint_error_is_not_hidden_by_unchanged_turns():
    assert classify({"notes": {"llm_error": "server unavailable"},
                     "iterations": [{"patch": "unchanged"}]}) == "llm_error"


def test_noop_turn_is_not_reported_as_patch_application_failure():
    record = {"iterations": [
        {"patch": "unchanged", "llm": {"completion_tokens": 10}},
        {"patch": "applied", "llm": {"completion_tokens": 10}},
    ], "certificate": {"build_count": 2, "build_failure_count": 0}}
    assert classify(record) == "no fix found (some unchanged turns)"


def test_historical_noops_are_recovered_from_conversation():
    run = {"iterations": [{}], "certificate": {"log": {"messages": [
        {"role": "user", "content": "Please modify the following code in file.cpp to fix the bug:\n```cpp\nreturn 0;\n```"},
        {"role": "assistant", "content": "```cpp\nreturn 0;\n```"},
    ]}}}
    assert patch_statuses(run) == ["unchanged"]


class KnownFixEnv:
    def __init__(self, baseline=None):
        self.data = {"patch": "human patch"}
        self.build_failure_count = 0
        self.resets = 0
        self.baseline = baseline or {"src": "src", "tgt": "tgt", "log": "1 incorrect transformations"}
        self.last_validation = {"stage": "reproducer", "passed": False}

    def reset(self):
        self.resets += 1

    def check_fast(self):
        return False, [{"result": False, "log": self.baseline}]

    def check_full(self):
        self.last_validation = {"stage": "regression", "passed": True}
        return True, "all tests passed"


def test_known_fix_uses_full_validation_and_restores_checkout():
    env = KnownFixEnv()
    patches = []
    helper = types.SimpleNamespace(apply=lambda patch: (patches.append(patch) is None, "applied"))
    assert check_known_fix(env, helper)["passed"]
    assert patches == ["human patch"]
    assert env.resets == 2


def test_known_fix_does_not_misread_zero_incorrect_as_reproduced():
    env = KnownFixEnv({"src": "src", "tgt": "tgt", "log": "0 incorrect transformations\n1 failed-to-prove transformations"})
    helper = types.SimpleNamespace(apply=lambda patch: pytest.fail("must not apply patch"))
    assert not check_known_fix(env, helper)["passed"]
    assert env.resets == 2
