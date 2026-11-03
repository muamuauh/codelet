"""Mini eval runner.

Pipeline per task:
  1. Materialize the task's `files` into a fresh temp workspace.
  2. `os.chdir` into it so every tool (file_*, bash, glob, grep) is sandboxed
     to that directory -- the tools resolve relative paths against cwd.
  3. Run the real AgentLoop (AUTO mode, streaming off) against the prompt.
  4. Run the task's `checks` in the workspace (execution-based: a shell command
     that must exit 0, or a file-content assertion).
  5. Record pass/fail + tokens + cost + wall time.
  6. Keep the per-step trajectory (evals/reports/traj/<stamp>/<task>.jsonl) and
     the process metrics read off it (evals/trajectory_metrics.py).

`--repeat k` runs every task k times: pass@1 is the mean over all trials,
pass@k counts a task solved if any trial passed, pass^k only if all did. The
gap between the last two is the run-to-run variance a single pass rate hides.

Config (provider/model/key) is resolved exactly like the CLI: .env + settings.json
+ the same flags (`--profile`, `--model`). So `python -m evals.runner` uses
whatever LLM you've configured for the project.

Usage:
    python -m evals.runner                  # run every task in evals/tasks
    python -m evals.runner --filter slug    # only tasks whose name contains "slug"
    python -m evals.runner --task evals/tasks/fix_off_by_one.yaml
    python -m evals.runner --profile deepseek --max-turns 20 --verbose
    python -m evals.runner --repeat 3            # pass@1 / pass@3 / pass^3

Model, prices and a spend cap can come from .env (flags win):
    CODELET_EVAL_MODEL          blank = LLM_MODEL
    CODELET_EVAL_PRICE_INPUT    USD per million input tokens
    CODELET_EVAL_PRICE_OUTPUT   USD per million output tokens
    CODELET_EVAL_BUDGET_USD     no new task starts once the suite has spent this
With a budget set, the runner refuses to start unless the model has a price:
a cost of "n/a" would let the cap pass silently.
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
from codelet.trajectory import Trajectory

from evals.trajectory_metrics import aggregate, metrics

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
    category: str = "general"
    # Reference fix: files to overwrite after `files`. Never shown to the agent;
    # tests/test_eval_tasks.py uses it to prove the task is valid (checks fail
    # on the starting files, pass on the solution). `{}` = the right answer is
    # to change nothing (clarify tasks), so the starting files must pass.
    solution: dict[str, str] = field(default_factory=dict)
    # Written into the workspace only after the agent stops, right before the
    # checks: SWE-bench's FAIL_TO_PASS / PASS_TO_PASS. The agent works from the
    # issue text and has to anticipate the edge cases, instead of reading them
    # off a test file that is sitting next to the code.
    hidden_files: dict[str, str] = field(default_factory=dict)


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
    category: str = "general"
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    llm_calls: int = 0
    duration_s: float = 0.0
    error: str | None = None
    transcript_tail: str = ""
    trial: int = 1
    trajectory: str | None = None
    process: dict[str, Any] = field(default_factory=dict)


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
        category=str(data.get("category") or "general"),
        solution={str(k): str(v) for k, v in (data.get("solution") or {}).items()},
        hidden_files={str(k): str(v) for k, v in (data.get("hidden_files") or {}).items()},
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
    # No .pyc: a .pyc is trusted when the source's mtime (whole seconds) and size
    # match, so an equal-length edit made within the second of the last test run
    # (`cfg["Port"]` -> `cfg["port"]`) was graded against the old bytecode.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
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
    trace_dir: Path | None = None,
    trial: int = 1,
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
    trace: Trajectory | None = None
    if trace_dir is not None:
        stem = task.name if trial == 1 else f"{task.name}#{trial}"
        trace = Trajectory(trace_dir / f"{stem}.jsonl", meta={
            "task": task.name, "trial": trial, "provider": config.provider.value,
            "model": config.model, "max_turns": config.max_turns})

    try:
        _write_files(workspace, task.files)
        os.chdir(workspace)
        agent = AgentLoop(config=config, registry=ToolRegistry.default(), console=agent_console,
                          trajectory=trace)
        try:
            agent.run(task.prompt)
        except Exception as exc:  # agent crash shouldn't abort the whole suite
            error = f"{type(exc).__name__}: {exc}"
        cum = agent.telemetry.cumulative
        tokens_in, tokens_out = cum.input_tokens or 0, cum.output_tokens or 0
        cost = cum.cost_usd or 0.0  # None when the model has no pricing entry
        calls = len(agent.telemetry.turns)
        _write_files(workspace, task.hidden_files)   # after the agent, before grading
        checks = run_checks(workspace, task.checks)
    finally:
        os.chdir(prev_cwd)
        if not keep_workspace:
            shutil.rmtree(workspace, ignore_errors=True)

    passed = error is None and len(checks) > 0 and all(c.passed for c in checks)
    if trace is not None:
        trace.record("eval_result", passed=passed, error=error,
                     checks=[{"description": c.description, "passed": c.passed} for c in checks])
    return TaskResult(
        name=task.name,
        category=task.category,
        passed=passed,
        checks=checks,
        input_tokens=tokens_in,
        output_tokens=tokens_out,
        cost_usd=cost,
        llm_calls=calls,
        duration_s=time.monotonic() - started,
        error=error,
        transcript_tail=transcript.getvalue()[-2000:],
        trial=trial,
        trajectory=str(trace.path) if trace is not None else None,
        process=metrics(trace.events) if trace is not None else {},
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


def pass_stats(results: list[TaskResult]) -> dict[str, Any]:
    """pass@1 over all trials; pass@k / pass^k over tasks (any / every trial passed)."""
    by_task: dict[str, list[bool]] = {}
    for r in results:
        by_task.setdefault(r.name, []).append(r.passed)
    k = max((len(v) for v in by_task.values()), default=0)
    n = len(by_task)
    return {
        "tasks": n,
        "trials_per_task": k,
        "pass@1": round(sum(r.passed for r in results) / len(results), 3) if results else None,
        f"pass@{k}": round(sum(any(v) for v in by_task.values()) / n, 3) if n else None,
        f"pass^{k}": round(sum(all(v) for v in by_task.values()) / n, 3) if n else None,
    }


def by_category(results: list[TaskResult]) -> dict[str, list[int]]:
    """category -> [passed trials, trials]."""
    out: dict[str, list[int]] = {}
    for r in results:
        row = out.setdefault(r.category, [0, 0])
        row[0] += r.passed
        row[1] += 1
    return out


def write_json_report(results: list[TaskResult], stamp: str | None = None) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    path = REPORTS_DIR / f"{stamp}.json"
    traced = [r.process for r in results if r.process]
    payload = {
        "timestamp": stamp,
        "summary": {
            "total": len(results),
            "passed": sum(1 for r in results if r.passed),
            "total_cost_usd": sum(r.cost_usd for r in results),
            "total_tokens": sum(r.input_tokens + r.output_tokens for r in results),
            **pass_stats(results),
            "by_category": by_category(results),
            "process": aggregate(traced) if traced else None,
        },
        "results": [
            {
                "name": r.name,
                "category": r.category,
                "trial": r.trial,
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
                "trajectory": r.trajectory,
                "process": r.process,
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
    p.add_argument("--repeat", type=int, default=1,
                   help="Run each task N times (pass@1 / pass@N / pass^N).")
    p.add_argument("--no-trace", action="store_true",
                   help="Do not record per-step trajectories.")
    p.add_argument("--budget", type=float, default=None,
                   help="Spend cap in USD (default: CODELET_EVAL_BUDGET_USD; 0 = none).")
    return p


def _env_float(name: str) -> float | None:
    raw = os.environ.get(name, "").strip()
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def apply_eval_env(config: Any, args: argparse.Namespace) -> float | None:
    """Fold the CODELET_EVAL_* settings into `config`; return the budget (None = no cap)."""
    if not args.model and os.environ.get("CODELET_EVAL_MODEL", "").strip():
        config.model = os.environ["CODELET_EVAL_MODEL"].strip()
    p_in, p_out = _env_float("CODELET_EVAL_PRICE_INPUT"), _env_float("CODELET_EVAL_PRICE_OUTPUT")
    if p_in is not None and p_out is not None:
        config.pricing_overrides = {**(config.pricing_overrides or {}),
                                    config.model: {"input": p_in, "output": p_out}}
    budget = args.budget if args.budget is not None else _env_float("CODELET_EVAL_BUDGET_USD")
    return budget if budget else None


def _priced(config: Any) -> bool:
    from codelet.telemetry import Telemetry

    t = Telemetry()
    t.update_pricing(config.pricing_overrides)
    return config.model in t.pricing


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    console = Console()

    load_env_files()
    settings = load_settings()
    # The agent's own `python` / `pytest` must be the interpreter the checks use
    # (see _check_env); otherwise a task can pass under one Python and be graded
    # under another.
    os.environ["PATH"] = _check_env()["PATH"]
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"      # same stale-.pyc trap as the checks
    # Reuse the CLI's config resolution; force AUTO + no streaming for headless runs.
    cfg_args = argparse.Namespace(
        profile=args.profile, provider=None, model=args.model, base_url=None,
        api_key=None, mode="auto", max_turns=args.max_turns, no_stream=True,
    )
    config = _build_config(cfg_args, settings)
    config.permission_mode = PermissionMode.AUTO
    config.stream = False
    budget = apply_eval_env(config, args)
    if budget is not None and not _priced(config):
        console.print(f"[red]No price for model {config.model!r}:[/red] set "
                      "CODELET_EVAL_PRICE_INPUT / CODELET_EVAL_PRICE_OUTPUT in .env (or "
                      "`pricing` in settings.json), or pass --budget 0 to run uncapped.")
        return 2

    if args.task:
        tasks = [load_task(Path(args.task))]
    else:
        tasks = load_tasks(Path(args.tasks_dir), name_filter=args.filter)

    if not tasks:
        console.print("[yellow]No tasks found.[/yellow]")
        return 1

    console.print(f"[dim]Running {len(tasks)} task(s) x {max(1, args.repeat)} with "
                  f"{config.model} via {config.provider.value}"
                  + (f", budget ${budget:.2f}" if budget else "") + "[/dim]")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    trace_dir = None if args.no_trace else REPORTS_DIR / "traj" / stamp
    results: list[TaskResult] = []
    spent = 0.0
    stopped = False
    # Trial-major: every task once, then every task again. If the budget stops
    # the run early, pass@1 still covers the whole suite instead of the first
    # few tasks three times over.
    for trial in range(1, max(1, args.repeat) + 1):
        if stopped:
            break
        for task in tasks:
            # Stop before a task that would likely cross the cap, not after:
            # estimate the next one from the mean so far.
            nxt = spent / len(results) if results else 0.0
            if budget is not None and spent + nxt > budget:
                console.print(f"[yellow]budget ${budget:.2f}: spent ${spent:.4f} + "
                              f"~${nxt:.4f} next would cross it; stopping[/yellow]")
                stopped = True
                break
            label = task.name if args.repeat <= 1 else f"{task.name} #{trial}"
            console.print(f"\n[cyan]▶ {label}[/cyan] [dim]{task.description}[/dim]")
            result = run_one(task, config, console=console,
                             verbose=args.verbose, keep_workspace=args.keep_workspace,
                             trace_dir=trace_dir, trial=trial)
            mark = "[green]✓[/green]" if result.passed else "[red]✗[/red]"
            console.print(f"  {mark} {sum(1 for c in result.checks if c.passed)}/"
                          f"{len(result.checks)} checks  "
                          f"[dim]{result.duration_s:.1f}s, {result.llm_calls} calls[/dim]")
            results.append(result)
            spent += result.cost_usd or 0.0

    render_report(console, results)
    stats = pass_stats(results)
    if stats["trials_per_task"] > 1:
        k = stats["trials_per_task"]
        console.print(f"pass@1 {stats['pass@1']}  pass@{k} {stats[f'pass@{k}']}  "
                      f"pass^{k} {stats[f'pass^{k}']}")
    if not args.no_json:
        path = write_json_report(results, stamp)
        console.print(f"[dim]report: {path}[/dim]")
        if trace_dir is not None:
            console.print(f"[dim]trajectories: {trace_dir}[/dim]")

    n_pass = sum(1 for r in results if r.passed)
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
