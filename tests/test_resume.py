"""Tests for run-record durability -- can an interrupted sweep be resumed?

The sweep runs for days unattended (792 cells, `docs/ANALYSIS_PLAN.md` §5),
against a vLLM server on a GPU shared with someone else's job. It *will* be
interrupted: the endpoint restarts, the machine reboots, tmux dies, someone
hits Ctrl-C. Every one of those has to leave the results directory in a state
a plain re-run recovers from, because re-running from scratch means throwing
away days of LLVM rebuilds.

The rule the sweep resumes on is "a record file exists, so that cell is
done". These tests pin down the two cases where that rule is wrong and would
silently keep a broken result forever:

    an endpoint error mid-run   the loop breaks early, but iterations
                                recorded before the break mean a record still
                                gets written -- with `fixed: false` meaning
                                "vLLM died", not "the model failed to fix it".
                                Cost a whole nine-condition pilot once
                                (docs/IMPLEMENTATION.md Blocker 14).

    a truncated write           the process was killed during json.dump.

Both must be redone on resume. Everything else -- a finished run, a run whose
certificate dump failed -- must be left alone, or a resume would redo work
that was already valid.

No LLVM, no Alive2, no model endpoint needed: these build RunLog objects
directly.
"""

from __future__ import annotations

import json
import os

from ce.benchmark import Iteration, RunLog, record_is_complete


def _write(directory, bug_id, condition="iraware-plain", notes=None, iterations=1):
    """A run record on disk, as the sweep would leave one."""
    run = RunLog(bug_id=bug_id, condition=condition, notes=dict(notes or {}))
    for index in range(iterations):
        run.record(Iteration(index=index, condition=condition, fixed=False))
    return run.write(str(directory))


def test_finished_run_is_skipped_on_resume(tmp_path):
    assert record_is_complete(_write(tmp_path, "111"))


def test_endpoint_error_is_redone_on_resume(tmp_path):
    """The Blocker 14 case: iterations recorded, then the model call failed."""
    path = _write(tmp_path, "222", notes={"llm_error": "Error code: 400 ..."},
                  iterations=2)
    assert not record_is_complete(path)


def test_truncated_record_is_redone_on_resume(tmp_path):
    path = tmp_path / "333.iraware-plain.json"
    path.write_text('{"bug_id": "333", "iterat', encoding="utf-8")
    assert not record_is_complete(str(path))


def test_missing_record_is_redone_on_resume(tmp_path):
    assert not record_is_complete(str(tmp_path / "nothing-here.json"))


def test_non_object_record_is_redone_on_resume(tmp_path):
    path = tmp_path / "444.iraware-plain.json"
    path.write_text("[]", encoding="utf-8")
    assert not record_is_complete(str(path))


def test_dump_error_does_not_invalidate_a_finished_run(tmp_path):
    """`env.dump()` failing loses the certificate, not the run itself.

    Redoing these would throw away hours of valid rebuilds for a missing
    convenience artefact.
    """
    path = _write(tmp_path, "555", notes={"dump_error": "certificate failed"})
    assert record_is_complete(path)


def test_write_is_atomic(tmp_path):
    """No .tmp residue, and the record parses -- see RunLog.write."""
    path = _write(tmp_path, "666", iterations=2)
    assert not [p for p in os.listdir(tmp_path) if p.endswith(".tmp")]
    assert json.loads(open(path, encoding="utf-8").read())["bug_id"] == "666"


def test_rewriting_a_record_replaces_it_cleanly(tmp_path):
    """--overwrite re-runs a cell; the rename must not leave a merged file."""
    _write(tmp_path, "777", iterations=1)
    path = _write(tmp_path, "777", iterations=3)
    assert json.loads(open(path, encoding="utf-8").read())["totals"]["iterations"] == 3
