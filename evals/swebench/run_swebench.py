"""Generate SWE-bench Lite predictions with the codelet agent.

For each task instance:
  1. Shallow-fetch the repo at `base_commit` into a clean clone.
  2. Run the agent (AUTO mode, no streaming) with the issue as the prompt,
     sandboxed by chdir into the clone -- the tools resolve paths against cwd.
  3. Stage everything and capture `git diff --cached` as the model patch.
  4. Write one `{instance_id, model_name_or_path, model_patch}` line per instance.

The resulting predictions.jsonl is fed to the official Docker-based evaluator
(see README). Use `--gold` to skip the LLM and emit each instance's own gold
patch instead -- a zero-cost end-to-end sanity check of the whole pipeline.

Usage:
    python -m evals.swebench.run_swebench --limit 1
    python -m evals.swebench.run_swebench --instance marshmallow-code__marshmallow-1359
    python -m evals.swebench.run_swebench --gold --limit 5      # no LLM, gold patches
    python -m evals.swebench.run_swebench --limit 3 --profile deepseek
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from rich.console import Console

from codelet.agent_loop import AgentLoop
from codelet.cli import _build_config
from codelet.config import PermissionMode
from codelet.settings import load_env_files, load_settings
from codelet.tools.base import ToolRegistry

DATASET = "princeton-nlp/SWE-bench_Lite"
SPLIT = "test"
MODEL_NAME = "codelet"
OUT_DIR = Path(__file__).parent / "out"

PROMPT_TEMPLATE = """\
You are working at the root of the `{repo}` repository, checked out at the commit \
where the following issue was reported. Resolve the issue by editing the project's \
**source code only** -- do NOT edit, add, or delete any test files (the grader \
supplies its own tests). Keep the change minimal and focused on the root cause.

You do not need to run the test suite (its dependencies may not be installed); \
focus on making a correct code change. When you are confident the fix is complete, \
stop.

--- ISSUE ---
{problem_statement}
"""


# ---------- dataset ----------

def load_instances(
    *,
    limit: int | None,
    instance_ids: list[str] | None,
    dataset_name: str = DATASET,
    split: str = SPLIT,
) -> list[dict[str, Any]]:
    try:
        from datasets import load_dataset
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "The 'datasets' package is required to load SWE-bench Lite.\n"
            "Install it with:  pip install datasets"
        ) from exc

    ds = load_dataset(dataset_name, split=split)
    rows = [dict(r) for r in ds]
    if instance_ids:
        wanted = set(instance_ids)
        rows = [r for r in rows if r["instance_id"] in wanted]
    if limit is not None:
        rows = rows[:limit]
    return rows


# ---------- repo prep + diff ----------

def _git(args: list[str], cwd: str | Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd) if cwd else None,
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=check,
    )


FETCH_ATTEMPTS = 4


def prepare_repo(repo: str, base_commit: str, dest: Path) -> None:
    """Fetch just `base_commit` of `repo` into `dest` (GitHub allows fetch-by-SHA).

    The fetch is retried: over a long run, transient TLS/network failures
    ("schannel: failed to receive handshake") otherwise abort the instance before
    the agent ever starts, and a clone failure would be indistinguishable from the
    agent producing no patch — quietly scoring an infrastructure blip as a model
    failure.
    """
    url = f"https://github.com/{repo}.git"
    dest.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], cwd=dest)
    _git(["remote", "add", "origin", url], cwd=dest)
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        proc = _git(["fetch", "-q", "--depth", "1", "origin", base_commit],
                    cwd=dest, check=False)
        if proc.returncode == 0:
            break
        if attempt == FETCH_ATTEMPTS:
            raise subprocess.CalledProcessError(
                proc.returncode, proc.args, output=proc.stdout, stderr=proc.stderr
            )
        time.sleep(2 ** attempt)  # 2s, 4s, 8s
    _git(["checkout", "-q", base_commit], cwd=dest)


def extract_patch(repo_dir: Path) -> str:
    """Stage all changes (so new files are included) and return the unified diff."""
    _git(["add", "-A"], cwd=repo_dir)
    proc = _git(["diff", "--cached"], cwd=repo_dir)
    return proc.stdout


def apply_gold_patch(repo_dir: Path, patch: str) -> None:
    """Apply an instance's gold patch via `git apply` (for --gold sanity checks)."""
    patch_file = repo_dir / "__gold.patch"
    patch_file.write_text(patch, encoding="utf-8")
    try:
        _git(["apply", "--whitespace=nowarn", str(patch_file)], cwd=repo_dir)
    finally:
        patch_file.unlink(missing_ok=True)


# ---------- prompt ----------

def build_prompt(instance: dict[str, Any]) -> str:
    return PROMPT_TEMPLATE.format(
        repo=instance["repo"],
        problem_statement=instance["problem_statement"].strip(),
    )


# ---------- per-instance ----------

def generate_one(
    instance: dict[str, Any],
    config: Any,
    *,
    console: Console,
    gold: bool,
    verbose: bool,
    keep_clone: bool,
) -> dict[str, Any]:
    instance_id = instance["instance_id"]
    workdir = Path(tempfile.mkdtemp(prefix=f"swe-{instance_id}-"))
    prev_cwd = os.getcwd()
    started = time.monotonic()
    error: str | None = None
    patch = ""
    tokens_in = tokens_out = calls = 0

    try:
        prepare_repo(instance["repo"], instance["base_commit"], workdir)
        if gold:
            apply_gold_patch(workdir, instance["patch"])
        else:
            os.chdir(workdir)
            agent_console = console if verbose else Console(file=open(os.devnull, "w"))
            agent = AgentLoop(config=config, registry=ToolRegistry.default(), console=agent_console)
            try:
                agent.run(build_prompt(instance))
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            cum = agent.telemetry.cumulative
            tokens_in, tokens_out = cum.input_tokens or 0, cum.output_tokens or 0
            calls = len(agent.telemetry.turns)
            os.chdir(prev_cwd)
        patch = extract_patch(workdir)
    except subprocess.CalledProcessError as exc:
        error = f"git error: {exc.stderr.strip()[-300:] if exc.stderr else exc}"
    finally:
        os.chdir(prev_cwd)
        if not keep_clone:
            shutil.rmtree(workdir, ignore_errors=True)

    max_turns = getattr(config, "max_turns", None) if config is not None else None
    return {
        "instance_id": instance_id,
        "model_name_or_path": MODEL_NAME,
        "model_patch": patch,
        # diagnostics (ignored by the official harness, handy for us)
        "_empty_patch": not patch.strip(),
        "_error": error,
        "_input_tokens": tokens_in,
        "_output_tokens": tokens_out,
        "_llm_calls": calls,
        "_duration_s": round(time.monotonic() - started, 1),
        # did the agent stop on its own, or get cut off at the turn budget?
        # (lets scoring tell "wrong answer" apart from "ran out of turns")
        "_max_turns": max_turns,
        "_hit_turn_cap": bool(max_turns is not None and calls >= max_turns),
    }


# ---------- entry point ----------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="evals.swebench.run_swebench",
                                description="Generate SWE-bench Lite predictions")
    p.add_argument("--limit", type=int, default=1, help="How many instances to run (default 1).")
    p.add_argument("--instance", action="append", default=None,
                   help="Specific instance_id(s) to run (repeatable). Overrides --limit selection.")
    p.add_argument("--gold", action="store_true",
                   help="Emit each instance's gold patch instead of running the agent (no LLM).")
    p.add_argument("--output", default=str(OUT_DIR / "predictions.jsonl"),
                   help="Where to write predictions.jsonl.")
    p.add_argument("--profile", default=None, help="LLM profile (same as the CLI).")
    p.add_argument("--model", default=None, help="Override model.")
    p.add_argument("--max-turns", type=int, default=40, help="Per-instance agent turn cap.")
    p.add_argument("--verbose", action="store_true", help="Show live agent output.")
    p.add_argument("--keep-clones", action="store_true", help="Keep temp clones for debugging.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    console = Console()

    config = None
    if not args.gold:
        load_env_files()
        settings = load_settings()
        cfg_args = argparse.Namespace(
            profile=args.profile, provider=None, model=args.model, base_url=None,
            api_key=None, mode="auto", max_turns=args.max_turns, no_stream=True,
        )
        config = _build_config(cfg_args, settings)
        config.permission_mode = PermissionMode.AUTO
        config.stream = False

    console.print("[dim]Loading SWE-bench Lite…[/dim]")
    instances = load_instances(limit=args.limit, instance_ids=args.instance)
    if not instances:
        console.print("[yellow]No matching instances.[/yellow]")
        return 1

    mode = "gold patches" if args.gold else f"{config.provider.value}/{config.model}"
    console.print(f"[dim]Generating predictions for {len(instances)} instance(s) with {mode}[/dim]")

    predictions: list[dict[str, Any]] = []
    for inst in instances:
        console.print(f"\n[cyan]▶ {inst['instance_id']}[/cyan] [dim]{inst['repo']}[/dim]")
        pred = generate_one(inst, config, console=console, gold=args.gold,
                            verbose=args.verbose, keep_clone=args.keep_clones)
        status = "[red]empty patch[/red]" if pred["_empty_patch"] else "[green]patch produced[/green]"
        extra = f", {pred['_llm_calls']} calls" if not args.gold else ""
        if pred["_error"]:
            status += f"  [red]({pred['_error']})[/red]"
        console.print(f"  {status}  [dim]{pred['_duration_s']}s{extra}[/dim]")
        predictions.append(pred)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for pred in predictions:
            f.write(json.dumps(pred, ensure_ascii=False) + "\n")

    n_patches = sum(1 for p in predictions if not p["_empty_patch"])
    console.print(f"\n[bold]{n_patches}/{len(predictions)} produced a non-empty patch[/bold]")
    console.print(f"[dim]predictions: {out_path}[/dim]")
    console.print(
        "\n[dim]Next: score with the official Docker harness —\n"
        f"  python -m swebench.harness.run_evaluation --dataset_name {DATASET} \\\n"
        f"    --predictions_path {out_path} --max_workers 4 --run_id mcc-run[/dim]"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
