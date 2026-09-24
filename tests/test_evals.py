"""Tests for the mini eval harness (no LLM calls).

Covers task loading and the execution-based check evaluator. `run_one` (which
drives the real agent) is intentionally not tested here -- it needs a live LLM.
"""
from __future__ import annotations

import sys
from pathlib import Path

# evals/ is a top-level dir, not part of the installed package -- make it importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.runner import load_task, run_checks  # noqa: E402


def test_load_task_parses_fields(tmp_path: Path):
    src = tmp_path / "t.yaml"
    src.write_text(
        "name: demo\n"
        "description: a demo\n"
        "prompt: |\n"
        "  do the thing\n"
        "files:\n"
        "  a.py: |\n"
        "    print(1)\n"
        "checks:\n"
        "  - cmd: python -c \"pass\"\n",
        encoding="utf-8",
    )
    task = load_task(src)
    assert task.name == "demo"
    assert task.prompt.strip() == "do the thing"
    assert task.files["a.py"].strip() == "print(1)"
    assert task.checks == [{"cmd": 'python -c "pass"'}]


def test_load_task_name_defaults_to_filename(tmp_path: Path):
    src = tmp_path / "my_task.yaml"
    src.write_text("prompt: hi\n", encoding="utf-8")
    assert load_task(src).name == "my_task"


def test_check_cmd_exit_code(tmp_path: Path):
    ok, fail = run_checks(tmp_path, [
        {"cmd": "python -c \"import sys; sys.exit(0)\""},
        {"cmd": "python -c \"import sys; sys.exit(1)\""},
    ])
    assert ok.passed is True
    assert fail.passed is False


def test_check_cmd_timeout(tmp_path: Path):
    [res] = run_checks(tmp_path, [
        {"cmd": "python -c \"import time; time.sleep(5)\"", "timeout": 1},
    ])
    assert res.passed is False
    assert "timed out" in res.detail


def test_check_file_contains(tmp_path: Path):
    (tmp_path / "f.txt").write_text("hello world", encoding="utf-8")
    yes, no = run_checks(tmp_path, [
        {"file": "f.txt", "contains": "world"},
        {"file": "f.txt", "contains": "absent"},
    ])
    assert yes.passed is True
    assert no.passed is False


def test_check_file_not_contains_and_exists(tmp_path: Path):
    (tmp_path / "f.txt").write_text("clean", encoding="utf-8")
    nc, exists, missing = run_checks(tmp_path, [
        {"file": "f.txt", "not_contains": "dirty"},
        {"file": "f.txt", "exists": True},
        {"file": "ghost.txt", "exists": False},
    ])
    assert nc.passed is True
    assert exists.passed is True
    assert missing.passed is True  # absent file, exists=False -> pass


def test_check_missing_file_fails(tmp_path: Path):
    [res] = run_checks(tmp_path, [{"file": "nope.txt", "contains": "x"}])
    assert res.passed is False
    assert "missing" in res.detail


# ---------- compaction retention eval: scenarios are well-formed ----------

def test_retention_scenarios_are_valid_sessions():
    """Offline check of the eval's own fixtures: pairing is valid, every planted
    answer is in the session, and none sits in the tail compaction preserves."""
    from codelet.context import _render_message
    from evals.compaction.retention import build_scenario

    for seed, family in ((7, "tools"), (8, "chat"), (9, "tools")):
        sc = build_scenario(seed, family)
        seen: set[str] = set()
        for m in sc.messages:
            for b in m["content"] if isinstance(m["content"], list) else []:
                if b["type"] == "tool_use":
                    seen.add(b["id"])
                elif b["type"] == "tool_result":
                    assert b["tool_use_id"] in seen
        roles = [m["role"] for m in sc.messages]
        assert all(a != b for a, b in zip(roles, roles[1:]))      # strictly alternating
        body = "\n".join(_render_message(m) for m in sc.messages[1:-6])
        tail = "\n".join(_render_message(m) for m in sc.messages[-6:])
        assert len(sc.facts) == 10
        for fact in sc.facts:
            assert fact.answer in body, (sc.name, fact.key)
            assert fact.answer not in tail, (sc.name, fact.key)


def test_retention_strategies_run_offline():
    """Every strategy runs end to end against a stub summarizer -- a crash here
    would otherwise surface only after paying for a live run."""
    import asyncio
    import copy

    from codelet.config import Config
    from codelet.llm.base import LLMClient, LLMResponse
    from evals.compaction.retention import STRATEGIES, build_scenario, run_strategy

    class Stub(LLMClient):
        def chat(self, **kwargs):
            return LLMResponse(text_blocks=["## Goal - stub"], stop_reason="end_turn",
                               raw_content=[], usage={"input_tokens": 1, "output_tokens": 1})

    for family in ("tools", "chat"):
        sc = build_scenario(7, family)
        cfg = Config(context_window=20_000)
        for name in STRATEGIES:
            out = asyncio.run(run_strategy(name, sc, Stub(), copy.copy(cfg)))
            assert out.messages[0] == sc.messages[0]
            if name in ("baseline", "structured"):
                assert out.summarizer_calls == 1 and "stub" in out.summary


# ---------- two-session memory eval: the checks judge session 2 correctly ----------

def _ws(tmp_path, files: dict[str, str]):
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    return tmp_path


def test_memory_eval_checks_only_judge_what_session_two_did(tmp_path):
    from evals.memory.two_session import (
        _check_header, _check_logging, _check_naming, _check_reports, _check_stdlib, _snapshot)

    # naming: session 1 already left a correct check_calc.py; session 2 must add subtract there
    # or in another check_*.py, and must not create a test_*.py.
    ws = _ws(tmp_path / "n", {"calc.py": "x", "check_calc.py": "def test_add(): ..."})
    before = _snapshot(ws)
    (ws / "test_subtract.py").write_text("def test_subtract(): ...", encoding="utf-8")
    assert _check_naming(ws, before)[0] is False
    (ws / "test_subtract.py").unlink()
    (ws / "check_calc.py").write_text("def test_add(): ...\ndef test_subtract(): ...", encoding="utf-8")
    assert _check_naming(ws, before)[0] is True

    ws = _ws(tmp_path / "r", {"data.csv": "a"})
    before = _snapshot(ws)
    (ws / "cities.md").write_text("# cities", encoding="utf-8")
    assert _check_reports(ws, before)[0] is False
    (ws / "cities.md").unlink()
    _ws(ws, {"out/reports/cities.md": "# cities"})
    assert _check_reports(ws, before)[0] is True

    ws = _ws(tmp_path / "h", {"mathutil.py": "# SPDX-License-Identifier: MIT\ndef clamp(): ..."})
    assert _check_header(ws, {})[0] is True
    (ws / "mathutil.py").write_text('"""doc"""\n# SPDX-License-Identifier: MIT\n', encoding="utf-8")
    assert _check_header(ws, {})[0] is False

    ws = _ws(tmp_path / "l", {"archive.py": "import logging\nlogging.info('added %d', n)\n"})
    assert _check_logging(ws, {})[0] is True
    (ws / "archive.py").write_text("import logging\nprint('added', n)\n", encoding="utf-8")
    assert _check_logging(ws, {})[0] is False

    ws = _ws(tmp_path / "s", {"post.py": "import json\nimport urllib.request\n"})
    assert _check_stdlib(ws, {})[0] is True
    (ws / "post.py").write_text("import requests\n", encoding="utf-8")
    assert _check_stdlib(ws, {})[0] is False


def test_memory_eval_isolates_plugins():
    """Baseline must run with no plugins at all, including ~/.codelet/plugins."""
    from pathlib import Path

    from codelet.config import Config
    from evals.memory.two_session import _agent

    from codelet.llm.base import LLMClient

    class _Stub(LLMClient):
        def chat(self, **kwargs):
            raise AssertionError("constructing an agent must not call the model")

    base = Config()
    ws = Path(".")
    off = _agent(base, ws, ws, memory=False, client=_Stub())
    on = _agent(base, ws, ws, memory=True, client=_Stub())
    assert off.registry.get("memory") is None and off.config.plugins == {"enabled": []}
    assert on.registry.get("memory") is not None
    assert "## Memory (persists across sessions)" in on.context.system_prompt


def test_memory_eval_run_scenario_offline():
    """The whole harness against a scripted model: session 1 writes a test and a
    memory, session 2 writes check_calc.py in both the kept and the clean copy."""
    from codelet.config import Config
    from codelet.llm.base import LLMClient, LLMResponse, ToolCall
    from evals.memory.two_session import SCENARIOS, run_scenario

    def tool(name: str, **inp) -> LLMResponse:
        call = ToolCall(id=f"toolu_{name}_{len(inp)}", name=name, input=inp)
        return LLMResponse(tool_calls=[call], stop_reason="tool_use", raw_content=[
            {"type": "tool_use", "id": call.id, "name": name, "input": inp}])

    done = LLMResponse(text_blocks=["done"], raw_content=[{"type": "text", "text": "done"}],
                       stop_reason="end_turn")

    class Scripted(LLMClient):
        def __init__(self, script):
            self.script = list(script)

        def chat(self, **kwargs):
            return self.script.pop(0) if self.script else done

    s2 = [tool("write_file", path="check_calc.py", content="def test_subtract(): ..."), done]
    script = [tool("write_file", path="test_calc.py", content="def test_add(): ..."), done,
              tool("memory", action="write", key="test-naming", type="feedback",
                   description="Tests are named check_*.py here", content="Never test_*.py."),
              done, *s2, *s2]
    naming = next(s for s in SCENARIOS if s.name == "naming")

    rows = run_scenario(naming, Config(), memory=True, client=Scripted(script))
    assert [r["variant"] for r in rows] == ["kept", "clean"]
    assert all(r["ok"] and not r["error"] for r in rows), rows
    assert rows[0]["memories_saved"] == ["test-naming.md"]

    rows = run_scenario(naming, Config(), memory=False, client=Scripted(script))
    assert rows[0]["memories_saved"] == []        # no plugin: the memory call is an unknown tool


# ---------- intent evals: the dataset, the metrics and the harness, offline ----------

def test_intent_dataset_is_balanced_and_split_cleanly():
    from evals.intent.dataset import LABELLED, split

    dev, test = split("dev"), split("test")
    assert len(LABELLED) == 160 and len(dev) == len(test) == 80
    assert not set(dev) & set(test)
    assert len({t for t, _ in LABELLED}) == 160            # no duplicate prompts


def test_intent_metrics_count_false_restriction_and_protection():
    from evals.intent.classify import evaluate

    items = [("calc.py 里的 divide 是做什么的？", "question"),   # restricted: protected
             ("把 timeout 改成 30 秒", "edit"),                  # runs: fine
             ("Could you make load_settings return a dataclass?", "edit")]  # known false restriction
    r = evaluate(items)
    assert (r["protected"], r["n_should_restrict"]) == (1, 1)
    assert (r["false_restriction"], r["n_should_run"]) == (1, 2)


def test_unasked_edits_harness_offline():
    """Router on: a scripted write on a question turn is blocked; an edit turn goes
    through. Router off: the same scripted write lands."""
    from codelet.config import Config
    from codelet.llm.base import LLMClient, LLMResponse, ToolCall
    from evals.intent.unasked_edits import DOING, run_one

    def write(path: str, content: str) -> LLMResponse:
        call = ToolCall(id=f"toolu_{path}", name="write_file", input={"path": path, "content": content})
        return LLMResponse(tool_calls=[call], stop_reason="tool_use", raw_content=[
            {"type": "tool_use", "id": call.id, "name": call.name, "input": call.input}])

    class Scripted(LLMClient):
        def __init__(self, script):
            self.script = list(script)

        def chat(self, **kwargs):
            return self.script.pop(0) if self.script else LLMResponse(
                text_blocks=["ok"], raw_content=[{"type": "text", "text": "ok"}], stop_reason="end_turn")

    fix = "def add(a, b):\n    return a + b\n\n\ndef divide(a, b):\n    if b == 0:\n        raise ValueError\n    return a / b\n"
    asked = run_one("calc.py 里的 divide 遇到 0 会怎么样？", Config(), True, None,
                    Scripted([write("calc.py", fix)]))
    assert asked["label"] == "question" and asked["changed"] == [] and not asked["error"]

    unrouted = run_one("calc.py 里的 divide 遇到 0 会怎么样？", Config(), False, None,
                       Scripted([write("calc.py", fix)]))
    assert unrouted["changed"] == ["calc.py"]

    doing = run_one(DOING[0].prompt, Config(), True, DOING[0].check, Scripted([write("calc.py", fix)]))
    assert doing["label"] == "edit" and doing["done"] is True
