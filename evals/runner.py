"""Mini eval runner.

Pipeline per task:
  1. Materialize the task's `files` into a fresh temp workspace.
  2. `os.chdir` into it so every tool (file_*, bash, glob, grep) is sandboxed
     to that directory -- the tools resolve relative paths against cwd.
  3. Run the real AgentLoop (AUTO mode, streaming off) against the prompt.
  4. Run the task's `checks` in the workspace (execution-based: a shell command
     that must exit 0, or a file-content assertion).
  5. Record pass/fail + tokens + cost + wall time.

Config (provider/model/key) is resolved exactly like the CLI: .env + settings.json
+ the same flags (`--profile`, `--model`). So `python -m evals.runner` uses
whatever LLM you've configured for the project.

Usage:
    python -m evals.runner                  # run every task in evals/tasks
    python -m evals.runner --filter slug    # only tasks whose name contains "slug"
    python -m evals.runner --task evals/tasks/fix_off_by_one.yaml
    python -m evals.runner --profile deepseek --max-turns 20 --verbose
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from rich.console import Console
from rich.table import Table

from codelet.agent_loop import AgentLoop
from codelet.cli import _build_config
from codelet.config import PermissionMode
from codelet.settings import load_env_files, load_settings
from codelet.tools.base import ToolRegistry

TASKS_DIR = Path(__file__).parent / "tasks"
REPORTS_DIR = Path(__file__).parent / "reports"


# ---------- data model ----------

@dataclass
class Task:
    name: str
    prompt: str
    files: dict[str, str] = field(default_factory=dict)
    checks: list[dict[str, Any]] = field(default_factory=list)
    description: str = ""


@dataclass
class CheckResult:
    description: str
    passed: bool
    detail: str = ""


@dataclass
class TaskResult:
    name: str
    passed: bool
    checks: list[CheckResult]
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    llm_calls: int = 0
    duration_s: float = 0.0
    error: str | None = None
    transcript_tail: str = ""


# ---------- task loading ----------

def load_task(path: Path) -> Task:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if "prompt" not in data:
        raise ValueError(f"{path}: task is missing a 'prompt'")
    return Task(
        name=str(data.get("name") or path.stem),
        prompt=str(data["prompt"]),
        files={str(k): str(v) for k, v in (data.get("files") or {}).items()},
        checks=list(data.get("checks") or []),
        description=str(data.get("description") or ""),
    )


def load_tasks(tasks_dir: Path, *, name_filter: str | None = None) -> list[Task]:
    tasks: list[Task] = []
    for path in sorted(tasks_dir.glob("*.y*ml")):
        task = load_task(path)
        if name_filter and name_filter.lower() not in task.name.lower():
            continue
        tasks.append(task)
    return tasks


# ---------- checks ----------

def run_checks(workspace: Path, checks: list[dict[str, Any]]) -> list[CheckResult]:
    return [_run_check(workspace, c) for c in checks]


def _check_env() -> dict[str, str]:
    """Make check commands resolve `python`/`pytest` to the SAME interpreter env
    that's running the runner, regardless of what's activated on PATH."""
    env = os.environ.copy()
    exe_dir = Path(sys.executable).parent
    extra = os.pathsep.join([str(exe_dir), str(exe_dir / "Scripts")])
    env["PATH"] = extra + os.pathsep + env.get("PATH", "")
    env["PYTHONUTF8"] = "1"  # keep subprocess decoding sane on Windows
    return env


def _run_check(workspace: Path, check: dict[str, Any]) -> CheckResult:
    if "cmd" in check:
        cmd = str(check["cmd"])
        timeout = int(check.get("timeout", 60))
        try:
            proc = subprocess.run(
                cmd, shell=True, cwd=str(workspace), capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=timeout,
                env=_check_env(),
            )
        except subprocess.TimeoutExpired:
            return CheckResult(f"cmd: {cmd}", False, f"timed out after {timeout}s")
        ok = proc.returncode == 0
        detail = "" if ok else (proc.stdout + proc.stderr).strip()[-600:]
        return CheckResult(f"cmd: {cmd}", ok, detail)

    if "file" in check:
        rel = str(check["file"])
        path = workspace / rel
        exists = path.is_file()
        if "exists" in check:
            want = bool(check["exists"])
            return CheckResult(f"file {rel} exists={want}", exists == want,
                               "" if exists == want else f"exists={exists}")
        if not exists:
            return CheckResult(f"file {rel}", False, "file missing")
        text = path.read_text(encoding="utf-8", errors="replace")
        if "contains" in check:
            sub = str(check["contains"])
            return CheckResult(f"{rel} contains {sub!r}", sub in text,
                               "" if sub in text else "substring not found")
        if "not_contains" in check:
            sub = str(check["not_contains"])
            return CheckResult(f"{rel} not_contains {sub!r}", sub not in text,
                               "" if sub not in text else "forbidden substring present")
        return CheckResult(f"file {rel} present", True, "")

    return CheckResult(f"unknown check: {check}", False, "unrecognized check spec")


# ---------- running one task ----------

def _write_files(workspace: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        dest = workspace / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")


def run_one(
    task: Task,
    config: Any,
    *,
    console: Console,
    verbose: bool = False,
    keep_workspace: bool = False,
) -> TaskResult:
    workspace = Path(tempfile.mkdtemp(prefix=f"eval-{task.name}-"))
    transcript = io.StringIO()
    agent_console = console if verbose else Console(file=transcript, width=100)
    prev_cwd = os.getcwd()
    error: str | None = None
    checks: list[CheckResult] = []
    tokens_in = tokens_out = calls = 0
    cost = 0.0
    started = time.monotonic()

    try:
        _write_files(workspace, task.files)
        os.chdir(workspace)
        agent = AgentLoop(config=config, registry=ToolRegistry.default(), console=agent_console)
        try:
            agent.run(task.prompt)
        except Exception as exc:  # agent crash shouldn't abort the whole suite
            error = f"{type(exc).__name__}: {exc}"
        cum = agent.telemetry.cumulative
        tokens_in, tokens_out = cum.input_tokens or 0, cum.output_tokens or 0
        cost = cum.cost_usd or 0.0  # None when the model has no pricing entry
        calls = len(agent.telemetry.turns)
        checks = run_checks(workspace, task.checks)
    finally:
        os.chdir(prev_cwd)
        if not keep_workspace:
            shutil.rmtree(workspace, ignore_errors=True)

    passed = error is None and len(checks) > 0 and all(c.passed for c in checks)
    return TaskResult(
        name=task.name,
        passed=passed,
        checks=checks,
        input_tokens=tokens_in,
        output_tokens=tokens_out,
        cost_usd=cost,
        llm_calls=calls,
        duration_s=time.monotonic() - started,
        error=error,
        transcript_tail=transcript.getvalue()[-2000:],
    )


# ---------- reporting ----------

def render_report(console: Console, results: list[TaskResult]) -> None:
    table = Table(title="Eval results", header_style="bold", show_lines=False)
    table.add_column("Task")
    table.add_column("Result", justify="center")
    table.add_column("Checks", justify="right")
    table.add_column("Calls", justify="right")
    table.add_column("In/Out tok", justify="right")
    table.add_column("Cost", justify="right")
    table.add_column("Time", justify="right")

    for r in results:
        mark = "[green]✓ PASS[/green]" if r.passed else "[red]✗ FAIL[/red]"
        n_ok = sum(1 for c in r.checks if c.passed)
        cost = f"${r.cost_usd:.4f}" if r.cost_usd else "n/a"
        table.add_row(
            r.name, mark, f"{n_ok}/{len(r.checks)}", str(r.llm_calls),
            f"{r.input_tokens}/{r.output_tokens}", cost, f"{r.duration_s:.1f}s",
        )
    console.print()
    console.print(table)

    # Failed-check details
    for r in results:
        if r.passed:
            continue
        console.print(f"\n[red]✗ {r.name}[/red]")
        if r.error:
            console.print(f"  [yellow]agent error:[/yellow] {r.error}")
        for c in r.checks:
            if not c.passed:
                console.print(f"  [red]- {c.description}[/red]")
                if c.detail:
                    console.print(f"    [dim]{c.detail}[/dim]")

    n_pass = sum(1 for r in results if r.passed)
    total_cost = sum(r.cost_usd for r in results)
    total_tok = sum(r.input_tokens + r.output_tokens for r in results)
    rate = (n_pass / len(results) * 100) if results else 0.0
    cost_str = f"  cost ${total_cost:.4f}" if total_cost else ""
    console.print(
        f"\n[bold]{n_pass}/{len(results)} passed[/bold] ({rate:.0f}%)  "
        f"tokens {total_tok}{cost_str}"
    )


def write_json_report(results: list[TaskResult]) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = REPORTS_DIR / f"{stamp}.json"
    payload = {
        "timestamp": stamp,
        "summary": {
            "total": len(results),
            "passed": sum(1 for r in results if r.passed),
            "total_cost_usd": sum(r.cost_usd for r in results),
            "total_tokens": sum(r.input_tokens + r.output_tokens for r in results),
        },
        "results": [
            {
                "name": r.name,
                "passed": r.passed,
                "error": r.error,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "cost_usd": r.cost_usd,
                "llm_calls": r.llm_calls,
                "duration_s": round(r.duration_s, 2),
                "checks": [
                    {"description": c.description, "passed": c.passed, "detail": c.detail}
                    for c in r.checks
                ],
            }
            for r in results
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# ---------- entry point ----------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="evals.runner", description="Run codelet evals")
    p.add_argument("--tasks-dir", default=str(TASKS_DIR), help="Directory of *.yaml task files.")
    p.add_argument("--task", default=None, help="Run a single task file instead of the whole dir.")
    p.add_argument("--filter", default=None, help="Only run tasks whose name contains this string.")
    p.add_argument("--profile", default=None, help="LLM profile (same as the CLI).")
    p.add_argument("--model", default=None, help="Override model.")
    p.add_argument("--max-turns", type=int, default=20, help="Per-task agent turn cap.")
    p.add_argument("--verbose", action="store_true", help="Show live agent output per task.")
    p.add_argument("--keep-workspace", action="store_true", help="Keep temp workspaces for debugging.")
    p.add_argument("--no-json", action="store_true", help="Skip writing the JSON report.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    console = Console()

    load_env_files()
    settings = load_settings()
    # Reuse the CLI's config resolution; force AUTO + no streaming for headless runs.
    cfg_args = argparse.Namespace(
        profile=args.profile, provider=None, model=args.model, base_url=None,
        api_key=None, mode="auto", max_turns=args.max_turns, no_stream=True,
    )
    config = _build_config(cfg_args, settings)
    config.permission_mode = PermissionMode.AUTO
    config.stream = False

    if args.task:
        tasks = [load_task(Path(args.task))]
    else:
        tasks = load_tasks(Path(args.tasks_dir), name_filter=args.filter)

    if not tasks:
        console.print("[yellow]No tasks found.[/yellow]")
        return 1

    console.print(f"[dim]Running {len(tasks)} task(s) with "
                  f"{config.provider.value}/{config.model}[/dim]")

    results: list[TaskResult] = []
    for task in tasks:
        console.print(f"\n[cyan]▶ {task.name}[/cyan] [dim]{task.description}[/dim]")
        result = run_one(task, config, console=console,
                         verbose=args.verbose, keep_workspace=args.keep_workspace)
        mark = "[green]✓[/green]" if result.passed else "[red]✗[/red]"
        console.print(f"  {mark} {sum(1 for c in result.checks if c.passed)}/"
                      f"{len(result.checks)} checks  "
                      f"[dim]{result.duration_s:.1f}s, {result.llm_calls} calls[/dim]")
        results.append(result)

    render_report(console, results)
    if not args.no_json:
        path = write_json_report(results)
        console.print(f"[dim]report: {path}[/dim]")

    n_pass = sum(1 for r in results if r.passed)
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
