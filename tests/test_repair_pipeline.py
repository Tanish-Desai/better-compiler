"""Exercise repair()'s stage records and baseline guard without an LLVM build."""

from types import SimpleNamespace

import pytest

import examples.repair_experiment as rx


class FakeEnvironment:
    knowledge_cutoff = "2024-01-01"
    build_failure_count = 0

    def __init__(self, *args, **kwargs):
        self.turn = 0
        self.last_validation = {}

    def is_single_func_fix(self):
        return True

    def get_bug_type(self):
        return "miscompilation"

    def get_hint_components(self):
        return ["InstCombine"]

    def use_knowledge(self, *args):
        pass

    def reset(self):
        pass

    def check_fast(self):
        return False, "baseline reproducer fails"

    def get_hint_issue(self):
        return {"title": "test issue"}

    def check_full(self):
        self.turn += 1
        passed = self.turn == 2
        self.last_validation = {"stage": "regression" if passed else "reproducer",
                                "passed": passed, "reproducer_pass": passed,
                                "failure_kind": None if passed else "error"}
        return passed, "validation feedback"

    def dump(self, log):
        return {"log": log}


def args():
    return SimpleNamespace(build_jobs=1, max_iterations=4, oracle_budget=400,
                           strictness="error_class", no_promotion=False)


def model():
    replies = iter(["```cpp\nreturn 1;\n```", "```cpp\nreturn 2;\n```"])
    return SimpleNamespace(name="test", cutoff="2000-01-01", temperature=0.8,
                           chat=lambda messages: (next(replies), {"completion_tokens": 5}))


def test_each_turn_serializes_its_own_validation(monkeypatch):
    monkeypatch.setenv("LAB_LLM_MODEL", "test")
    monkeypatch.setattr(rx, "Env", FakeEnvironment)
    monkeypatch.setattr(rx, "bug_hunk", lambda env: ("file.cpp", "return 0;"))
    monkeypatch.setattr(rx, "apply_patch", lambda *args: True)
    run = rx.repair("111", "baseline", args(), model())
    assert run.fixed
    assert [it.validation["stage"] for it in run.iterations] == ["reproducer", "regression"]
    assert run.notes["experiment_config"]["model"] == run.model
    assert run.iterations[0].as_dict()["validation"]["passed"] is False


def test_unpatched_build_failure_never_queries_model(monkeypatch):
    class BrokenBuild(FakeEnvironment):
        build_failure_count = 1

    monkeypatch.setattr(rx, "Env", BrokenBuild)
    llm = model()
    llm.chat = lambda messages: pytest.fail("must not query LLM for infrastructure failure")
    with pytest.raises(RuntimeError, match="unpatched LLVM build failed"):
        rx.repair("111", "baseline", args(), llm)
