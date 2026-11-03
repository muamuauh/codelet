"""Process metrics read the right things off a real (scripted) trajectory."""
from __future__ import annotations

import sys
from pathlib import Path

from codelet.agent_loop import AgentLoop
from codelet.config import Config, PermissionMode
from codelet.llm.base import ToolCall
from codelet.tools.base import ToolRegistry
from codelet.trajectory import Trajectory

from tests.test_agent_loop import ScriptedClient, _text_response, _tool_response

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evals.runner import TaskResult, pass_stats  # noqa: E402
from evals.trajectory_metrics import aggregate, metrics  # noqa: E402


def _call(i, name, **inp):
    return ToolCall(id=f"t{i}", name=name, input=inp)


def _run(tmp_path, monkeypatch, script):
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b + 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    trace = Trajectory()
    agent = AgentLoop(config=Config(permission_mode=PermissionMode.AUTO),
                      registry=ToolRegistry.default(), client=ScriptedClient(script),
                      trajectory=trace)
    agent.run("fix calc.py")
    return metrics(trace.events)


def test_careful_run_reads_edits_then_verifies(tmp_path, monkeypatch):
    m = _run(tmp_path, monkeypatch, [
        _tool_response([_call(1, "read_file", path="calc.py")]),
        _tool_response([_call(2, "edit_file", path="calc.py",
                              old_string="a + b + 1", new_string="a + b")]),
        _tool_response([_call(3, "bash", command=f'"{sys.executable}" -c "print(1)"')]),
        _text_response("fixed"),
    ])
    assert m["llm_calls"] == 4 and m["tool_calls"] == 3
    assert m["read_before_edit"] == 1.0
    assert m["verified_after_edit"] is True
    assert m["steps_after_last_edit"] == 2           # the bash step + the final answer
    assert m["tool_error_rate"] == 0.0
    assert m["repeat_calls"] == 0


def test_blind_edit_and_repeats_are_flagged(tmp_path, monkeypatch):
    m = _run(tmp_path, monkeypatch, [
        _tool_response([_call(1, "edit_file", path="./calc.py",
                              old_string="a + b + 1", new_string="a + b")]),
        _tool_response([_call(2, "grep", pattern="add"), _call(3, "glob", pattern="*.py")]),
        _tool_response([_call(4, "grep", pattern="add")]),          # same call, no change between
        _tool_response([_call(5, "edit_file", path="calc.py",
                              old_string="missing", new_string="x")]),  # fails: not found
        _text_response("done"),
    ])
    assert m["read_before_edit"] == 0.5        # 2nd edit hit a path the 1st edit touched
    assert m["repeat_calls"] == 1
    assert m["verified_after_edit"] is False   # never ran anything after changing a file
    assert m["parallel_share"] == 0.4          # 2 of 5 calls were in a batch of 2
    assert m["tool_error_rate"] == 0.2


def test_aggregate_and_pass_stats():
    rows = [{"llm_calls": 3, "tool_calls": 2, "tool_error_rate": 0.0, "repeat_calls": 1,
             "verified_after_edit": True, "gate_blocks": {"permission_denied": 1}},
            {"llm_calls": 5, "tool_calls": 4, "tool_error_rate": 0.5, "repeat_calls": 0,
             "verified_after_edit": False, "gate_blocks": {}}]
    agg = aggregate(rows)
    assert agg["median_llm_calls"] == 4 and agg["verified_after_edit_rate"] == 0.5
    assert agg["gate_blocks"] == {"permission_denied": 1}

    def r(name, ok, trial):
        return TaskResult(name=name, passed=ok, checks=[], trial=trial)

    s = pass_stats([r("a", True, 1), r("a", False, 2), r("b", True, 1), r("b", True, 2)])
    assert s == {"tasks": 2, "trials_per_task": 2, "pass@1": 0.75, "pass@2": 1.0, "pass^2": 0.5}


def test_eval_env_sets_model_prices_and_budget(monkeypatch):
    import argparse

    from codelet.config import Config
    from evals.runner import _priced, apply_eval_env

    monkeypatch.setenv("CODELET_EVAL_MODEL", "cheap-model")
    monkeypatch.setenv("CODELET_EVAL_PRICE_INPUT", "0.5")
    monkeypatch.setenv("CODELET_EVAL_PRICE_OUTPUT", "2")
    monkeypatch.setenv("CODELET_EVAL_BUDGET_USD", "4")
    cfg = Config()
    budget = apply_eval_env(cfg, argparse.Namespace(model=None, budget=None))
    assert cfg.model == "cheap-model" and budget == 4.0 and _priced(cfg)

    # A flag wins over .env; --budget 0 means uncapped; no price -> not priced.
    monkeypatch.delenv("CODELET_EVAL_PRICE_INPUT")
    cfg = Config(model="flag-model")               # _build_config already applied --model
    assert apply_eval_env(cfg, argparse.Namespace(model="flag-model", budget=0.0)) is None
    assert cfg.model == "flag-model" and not _priced(cfg)


def test_bash_errors_are_split_by_cause():
    from evals.trajectory_metrics import bash_error_kind

    def ev(cmd, out):
        return {"input": {"command": cmd}, "output": out}

    assert bash_error_kind(ev("python -m pytest -q", "1 failed"), changed=False) == "repro"
    assert bash_error_kind(ev("python -m pytest -q", "1 failed"), changed=True) == "test_failed"
    assert bash_error_kind(ev("git status", "fatal: not a git repository"), changed=False) == "env"
    assert bash_error_kind(ev("ls -la", "找不到与参数名称 'la' 匹配的参数"), changed=False) == "env"
    assert bash_error_kind(ev("python x.py", "Traceback ... KeyError"), changed=False) == "other"
