"""Every eval task is valid: its checks fail on the starting files and pass on
the reference solution (or, for clarify tasks, pass untouched). A task whose
checks pass before the agent does anything measures nothing; one whose
solution fails them measures the task author. No LLM involved."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evals.runner import TASKS_DIR, _write_files, load_tasks, run_checks  # noqa: E402

TASKS = load_tasks(TASKS_DIR)


def test_suite_shape():
    assert len(TASKS) >= 15
    assert len({t.category for t in TASKS}) >= 6
    assert all(t.checks for t in TASKS)


@pytest.mark.parametrize("task", TASKS, ids=[t.name for t in TASKS])
def test_task_is_valid(task, tmp_path):
    _write_files(tmp_path, task.files)
    _write_files(tmp_path, task.hidden_files)
    before = run_checks(tmp_path, task.checks)
    if not task.solution:
        assert all(c.passed for c in before), [c.description for c in before if not c.passed]
        return
    assert not all(c.passed for c in before), "checks already pass on the starting files"
    _write_files(tmp_path, task.solution)
    _write_files(tmp_path, task.hidden_files)      # a solution may not touch them
    after = run_checks(tmp_path, task.checks)
    assert all(c.passed for c in after), [(c.description, c.detail) for c in after if not c.passed]


def test_hidden_files_never_reach_the_agent_workspace():
    """They are graded with, not worked from: nothing in `files` may name them."""
    for t in TASKS:
        assert not set(t.hidden_files) & set(t.files), t.name
