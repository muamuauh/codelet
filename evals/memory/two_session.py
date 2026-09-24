"""Two-session memory eval.

The question the memory plugin has to answer: when the user corrects the agent in
one session, does the NEXT session -- a fresh agent, empty context -- do it the
corrected way?

Each scenario is a small workspace plus three user turns:

  session 1   a task, then a correction of a project convention
              ("tests here are named check_*.py -- rename it")
  session 2   a new agent, a related task that the convention applies to
              ("add a test for subtract()")

A deterministic check then looks only at what session 2 produced. Every scenario
runs with the memory plugin enabled and with no plugins at all (the baseline: the
correction is gone with session 1's context).

Session 2 runs in two copies of the workspace, because the files session 1 left
behind can carry the convention on their own -- a model that finds check_calc.py
names the next test check_*.py without remembering anything:

  kept   session 1's files are still there (the realistic case: what memory adds
         on top of what the code already shows)
  clean  reset to the scenario's original files; only the memory directory
         survives (memory is the only thing that can carry the correction)

Reported per condition and variant: session-2 adherence, how many memories
session 1 wrote, and the tokens spent.

Usage:
    python -m evals.memory.two_session --model claude-haiku-4-5
    python -m evals.memory.two_session --model claude-haiku-4-5 --repeats 2 --filter naming
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from rich.console import Console
from rich.table import Table

from codelet.agent_loop import AgentLoop
from codelet.cli import _build_config
from codelet.config import Config, PermissionMode
from codelet.events import NullSink
from codelet.llm.base import LLMClient
from codelet.settings import load_env_files, load_settings
from codelet.skills.loader import SkillIndex
from codelet.tools.base import ToolRegistry

REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"
Snapshot = dict[str, str]           # relative path -> text, taken after session 1


@dataclass
class Scenario:
    name: str
    files: dict[str, str]
    task1: str
    correction: str
    task2: str
    check: Callable[[Path, Snapshot], tuple[bool, str]]


_SKIP = {".codelet", "__pycache__", ".pytest_cache"}


def _files(ws: Path) -> list[Path]:
    return [p for p in ws.rglob("*") if p.is_file() and not _SKIP & set(p.relative_to(ws).parts)]


def _new_or_changed(ws: Path, before: Snapshot) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in _files(ws):
        rel = p.relative_to(ws).as_posix()
        text = p.read_text(encoding="utf-8", errors="replace")
        if before.get(rel) != text:
            out[rel] = text
    return out


def _check_naming(ws: Path, before: Snapshot) -> tuple[bool, str]:
    changed = _new_or_changed(ws, before)
    wrong = [f for f in changed if Path(f).name.startswith("test_") and f not in before]
    right = [f for f, t in changed.items() if Path(f).name.startswith("check_") and "subtract" in t]
    return (bool(right) and not wrong), f"check_*: {right or '-'}; new test_*: {wrong or '-'}"


def _check_reports(ws: Path, before: Snapshot) -> tuple[bool, str]:
    new_md = [f for f in _new_or_changed(ws, before) if f.endswith(".md") and f not in before]
    inside = [f for f in new_md if f.startswith("out/reports/")]
    return (bool(inside) and len(inside) == len(new_md)), f"new .md files: {new_md or '-'}"


def _check_header(ws: Path, before: Snapshot) -> tuple[bool, str]:
    p = ws / "mathutil.py"
    if not p.is_file():
        return False, "mathutil.py missing"
    first = p.read_text(encoding="utf-8").splitlines()[:1]
    return first == ["# SPDX-License-Identifier: MIT"], f"first line: {first}"


def _check_logging(ws: Path, before: Snapshot) -> tuple[bool, str]:
    p = ws / "archive.py"
    if not p.is_file():
        return False, "archive.py missing"
    code = p.read_text(encoding="utf-8")
    prints = len(re.findall(r"\bprint\(", code))
    return (prints == 0 and "logging" in code), f"print() calls: {prints}; uses logging: {'logging' in code}"


def _check_stdlib(ws: Path, before: Snapshot) -> tuple[bool, str]:
    p = ws / "post.py"
    if not p.is_file():
        return False, "post.py missing"
    code = p.read_text(encoding="utf-8")
    third = re.findall(r"^\s*(?:import|from)\s+(requests|httpx|aiohttp|urllib3)\b", code, re.M)
    return (not third), f"third-party imports: {third or '-'}"


CALC = "def add(a, b):\n    return a + b\n\n\ndef subtract(a, b):\n    return a - b\n"
CSV = "name,city,age\nann,oslo,31\nbo,lima,25\ncy,oslo,40\ndi,kyiv,28\n"

SCENARIOS = [
    Scenario("naming", {"calc.py": CALC},
             "Add a unit test for add() in calc.py.",
             "In this repo test files are named check_<module>.py, never test_*.py. "
             "Rename the file you just created accordingly.",
             "Add a unit test for subtract() in calc.py.", _check_naming),
    Scenario("reports", {"data.csv": CSV},
             "Write a short markdown summary of data.csv (row count and column names) to a file.",
             "Reports never go in the repo root here. Move it to out/reports/ -- that is where "
             "every report in this project goes.",
             "Write a markdown report listing the distinct values of the city column in data.csv.",
             _check_reports),
    Scenario("header", {},
             "Create a module strutil.py with a function slugify(s) that lowercases s and "
             "replaces spaces with hyphens.",
             "Every Python file in this project must start with the exact line "
             "'# SPDX-License-Identifier: MIT'. Add it to strutil.py.",
             "Create a module mathutil.py with a function clamp(x, lo, hi).", _check_header),
    Scenario("logging", {},
             "Write a script cleanup.py that deletes *.tmp files in a directory given on the "
             "command line and reports what it deleted.",
             "We don't use print for diagnostics in this project -- use the logging module. "
             "Please fix cleanup.py.",
             "Write a script archive.py that zips a directory given on the command line and "
             "reports how many files it added.", _check_logging),
    Scenario("stdlib", {},
             "Write fetch.py that downloads a URL given on the command line and saves it to a file.",
             "This project must not have third-party dependencies -- standard library only. "
             "Rewrite fetch.py without them if you used any.",
             "Write post.py that sends a JSON POST to a URL given on the command line and "
             "reports the HTTP status code.", _check_stdlib),
]


def _snapshot(ws: Path) -> Snapshot:
    return {p.relative_to(ws).as_posix(): p.read_text(encoding="utf-8", errors="replace")
            for p in _files(ws)}


def _populate(ws: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (ws / rel).parent.mkdir(parents=True, exist_ok=True)
        (ws / rel).write_text(text, encoding="utf-8")


def _agent(base: Config, ws: Path, user_dir: Path, memory: bool,
           client: LLMClient | None = None) -> AgentLoop:
    cfg = copy.copy(base)
    cfg.permission_mode = PermissionMode.AUTO
    cfg.stream = False
    cfg.hooks = {}
    # enabled=[] rather than {}: an empty allowlist also keeps ~/.codelet/plugins out.
    cfg.plugins = ({"enabled": ["memory"], "config": {"memory": {
        "dir": str(ws / ".codelet" / "memory"), "user_dir": str(user_dir)}}}
        if memory else {"enabled": []})
    return AgentLoop(config=cfg, registry=ToolRegistry.default(), sink=NullSink(),
                     skill_index=SkillIndex(), client=client)


def run_scenario(sc: Scenario, base: Config, memory: bool,
                 client: LLMClient | None = None) -> list[dict]:
    """Session 1 once, then session 2 in a kept and a clean copy of the workspace."""
    root = Path(tempfile.mkdtemp(prefix=f"codelet-mem-{sc.name}-"))
    ws, user_dir = root / "ws", root / "user-memory"
    ws.mkdir()
    _populate(ws, sc.files)
    prev = os.getcwd()
    rows: list[dict] = []
    try:
        os.chdir(ws)
        first = _agent(base, ws, user_dir, memory, client)
        first.run(sc.task1)
        first.run(sc.correction)
        s1_tokens = first.telemetry.cumulative.input_tokens
        saved = sorted(p.name for d in (ws / ".codelet" / "memory", user_dir)
                       if d.is_dir() for p in d.glob("*.md"))
        for variant in ("kept", "clean"):
            ws2 = root / f"ws-{variant}"
            shutil.copytree(ws, ws2)
            if variant == "clean":
                for f in _files(ws2):
                    f.unlink()
                _populate(ws2, sc.files)
            before = _snapshot(ws2)
            error = ""
            try:
                os.chdir(ws2)
                second = _agent(base, ws2, user_dir, memory, client)
                second.run(sc.task2)
                ok, detail = sc.check(ws2, before)
                tokens = s1_tokens + second.telemetry.cumulative.input_tokens
            except Exception as exc:     # a crashed run is a failed run, and says why
                ok, detail, tokens, error = False, "", s1_tokens, f"{type(exc).__name__}: {exc}"
            rows.append({"scenario": sc.name, "memory": memory, "variant": variant, "ok": ok,
                         "detail": detail, "memories_saved": saved, "input_tokens": tokens,
                         "error": error})
    except Exception as exc:
        rows.append({"scenario": sc.name, "memory": memory, "variant": "session-1", "ok": False,
                     "detail": "", "memories_saved": [], "input_tokens": 0,
                     "error": f"{type(exc).__name__}: {exc}"})
    finally:
        os.chdir(prev)
        shutil.rmtree(root, ignore_errors=True)
    return rows


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--profile", default=None)
    p.add_argument("--model", default=None, help="Agent model (default: the profile's).")
    p.add_argument("--repeats", type=int, default=1)
    p.add_argument("--filter", default="", help="Only scenarios whose name contains this.")
    p.add_argument("--conditions", default="memory,none",
                   help="memory,none (default) or just one, to rerun a changed condition.")
    args = p.parse_args(argv)

    console = Console()
    load_env_files()
    base = _build_config(argparse.Namespace(profile=args.profile, provider=None, model=args.model,
                                            base_url=None, api_key=None, mode="auto",
                                            max_turns=12, no_stream=True), load_settings())
    conditions = [c == "memory" for c in args.conditions.split(",") if c in ("memory", "none")]
    rows = []
    for sc in [s for s in SCENARIOS if args.filter in s.name]:
        for rep in range(args.repeats):
            for memory in conditions:
                for r in run_scenario(sc, base, memory):
                    r["repeat"] = rep
                    rows.append(r)
                    console.print(f"{sc.name:8} {'memory' if memory else 'none':6} {r['variant']:5} "
                                  f"{'PASS' if r['ok'] else 'fail'}  saved={r['memories_saved'] or '-'}  "
                                  f"{r['detail']} {r['error']}")

    table = Table(title=f"Session-2 adherence after a session-1 correction ({base.model})")
    for col in ("condition", "session 2 in", "adherence", "saved a memory", "input tokens"):
        table.add_column(col, justify="left" if col in ("condition", "session 2 in") else "right")
    for memory in conditions:
        for variant in ("kept", "clean"):
            rs = [r for r in rows if r["memory"] is memory and r["variant"] == variant]
            table.add_row("memory plugin" if memory else "no memory",
                          "session-1 files kept" if variant == "kept" else "clean workspace",
                          f"{sum(r['ok'] for r in rs)}/{len(rs)}",
                          f"{sum(bool(r['memories_saved']) for r in rs)}/{len(rs)}",
                          f"{sum(r['input_tokens'] for r in rs):,}")
    console.print(table)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORTS_DIR / f"memory-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps({"model": base.model, "rows": rows}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    console.print(f"report: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
