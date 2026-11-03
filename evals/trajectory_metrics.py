"""Process metrics over a trajectory -- how the agent got its result.

Pass/fail says whether a task was solved. Two runs that both pass can still
differ by 3x in steps, or one can have edited a file it never looked at and got
lucky. These metrics read that off the trajectory (codelet/trajectory.py) with
plain code, no model:

  llm_calls / tool_calls       volume
  tool_error_rate              tool calls that ran and failed / tool calls ran,
                               NOT counting failing test runs: a red test run
                               before any edit is reproducing the bug (good), and
                               one after an edit is the code being wrong, not the
                               tool -- both are kept in bash_errors instead
  bash_errors                  failed bash calls by kind: repro (test command,
                               no file changed yet) / test_failed (test command
                               after a change) / env (shell dialect, missing
                               command, git outside a repo) / other
  env_errors                   bash_errors["env"] -- what the Environment section
                               of the system prompt is there to drive down
  gate_blocks                  calls stopped before running, by gate (outcome)
  repeat_calls                 identical (tool, input) issued again with no file
                               change in between -- re-running the same failing
                               test, re-reading the same file: the loop signature
  read_before_edit             edit_file calls on a path the agent had read (or
                               written) earlier / edit_file calls
  verified_after_edit          a bash call came after the last file change: the
                               agent checked its own work before stopping
  steps_after_last_edit        LLM calls after the last file change (wrap-up cost)
  parallel_share               tool calls issued in a batch of >= 2 / tool calls
  compactions, max_turns_hit, subagent_calls, tokens, cost, latency

    python -m evals.trajectory_metrics evals/reports/traj/20261102-210000
    python -m evals.trajectory_metrics one_task.jsonl --json
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from codelet.trajectory import load

MUTATING_TOOLS = {"write_file", "edit_file", "create_tool"}
_TEST_CMD = re.compile(r"\b(pytest|unittest|nose2|tox|npm (run )?test|yarn test|go test|"
                       r"cargo test|jest|mvn test|gradle test)\b")
# What the shell or git says when the command, not the code, is wrong:
# cmd.exe, PowerShell 7 (English and Chinese UI), POSIX shells, git.
_ENV_ERR = re.compile(r"not recognized as an internal or external command|不是内部或外部命令|"
                      r"此时不应有|was unexpected at this time|"
                      r"is not recognized as a name of a cmdlet|识别为 cmdlet|"
                      r"cannot be found that matches parameter name|找不到与参数名称|ParserError|"
                      r"not a git repository|command not found|unknown option", re.I)


def bash_error_kind(event: dict[str, Any], changed: bool) -> str:
    """Why a failed bash call failed: env / repro / test_failed / other."""
    output = str(event.get("output") or "")
    command = str((event.get("input") or {}).get("command") or "")
    if _ENV_ERR.search(output):
        return "env"
    if _TEST_CMD.search(command):
        return "test_failed" if changed else "repro"
    return "other"
_READS_PATH = {"read_file", "write_file", "edit_file"}


def _norm(path: Any) -> str:
    p = str(path or "").replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p.lower()


def _key(event: dict[str, Any]) -> str:
    return event["name"] + "|" + json.dumps(event.get("input"), sort_keys=True, ensure_ascii=False)


def metrics(events: list[dict[str, Any]], *, agent: str | None = "main") -> dict[str, Any]:
    """Metrics for one trajectory. `agent=None` pools the main agent and its subagents."""
    scoped = [e for e in events if agent is None or e.get("agent") == agent]
    llm = [e for e in scoped if e["kind"] == "llm_call"]
    tools = [e for e in scoped if e["kind"] == "tool_call"]

    ran = [t for t in tools if t["outcome"] in ("ok", "error")]
    errors = sum(1 for t in ran if t["outcome"] == "error")
    gate_blocks = Counter(t["outcome"] for t in tools if t["outcome"] not in ("ok", "error"))

    seen: set[str] = set()
    touched: set[str] = set()
    repeats = edits = edits_after_read = 0
    last_change_step: int | None = None
    bash_after_change = False
    bash_errors: Counter[str] = Counter()
    for t in tools:
        name = t["name"]
        if name == "bash" and t["outcome"] == "error":
            bash_errors[bash_error_kind(t, changed=last_change_step is not None)] += 1
        key = _key(t)
        if key in seen:
            repeats += 1
        seen.add(key)
        path = _norm((t.get("input") or {}).get("path"))
        if name == "edit_file":
            edits += 1
            edits_after_read += path in touched
        if name in _READS_PATH and t["outcome"] == "ok" and path:
            touched.add(path)
        if name in MUTATING_TOOLS and t["outcome"] == "ok":
            last_change_step = t["step"]
            bash_after_change = False
            seen.clear()                    # after a change, re-running is legitimate
        elif name == "bash" and t["outcome"] in ("ok", "error") and last_change_step is not None:
            bash_after_change = True

    usage_in = sum(int((e.get("usage") or {}).get("input_tokens", 0) or 0) for e in llm)
    usage_out = sum(int((e.get("usage") or {}).get("output_tokens", 0) or 0) for e in llm)
    costs = [e.get("cost_usd") for e in llm]
    end = next((e for e in reversed(scoped) if e["kind"] == "turn_end"), {})

    return {
        "llm_calls": len(llm),
        "tool_calls": len(tools),
        "by_tool": dict(Counter(t["name"] for t in tools)),
        "tool_error_rate": (round((errors - bash_errors["repro"] - bash_errors["test_failed"])
                                  / len(ran), 3) if ran else None),
        "bash_errors": dict(bash_errors),
        "env_errors": bash_errors["env"],
        "gate_blocks": dict(gate_blocks),
        "repeat_calls": repeats,
        "read_before_edit": round(edits_after_read / edits, 3) if edits else None,
        "changed_files": last_change_step is not None,
        "verified_after_edit": bash_after_change if last_change_step is not None else None,
        "steps_after_last_edit": (max((e["step"] for e in llm), default=0) - last_change_step
                                  if last_change_step is not None else None),
        "parallel_share": (round(sum(1 for t in tools if t.get("batch", 1) > 1) / len(tools), 3)
                           if tools else None),
        "compactions": sum(1 for e in scoped if e["kind"] == "compaction"),
        "max_turns_hit": bool(end.get("max_turns_hit")),
        "subagent_calls": sum(1 for e in events
                              if e["kind"] == "llm_call" and e.get("agent") != "main"),
        "input_tokens": usage_in,
        "output_tokens": usage_out,
        "cost_usd": (round(sum(c for c in costs if c is not None), 6)
                     if any(c is not None for c in costs) else None),
        "llm_latency_s": round(sum(e.get("latency_ms", 0) for e in llm) / 1000, 2),
        "tool_latency_s": round(sum(t.get("latency_ms", 0) for t in tools) / 1000, 2),
    }


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Suite-level view: medians for counts, means for rates, totals for gates."""
    def med(key: str) -> float | None:
        vals = [r[key] for r in rows if r.get(key) is not None]
        return statistics.median(vals) if vals else None

    def mean(key: str) -> float | None:
        vals = [float(r[key]) for r in rows if r.get(key) is not None]
        return round(statistics.fmean(vals), 3) if vals else None

    gates: Counter[str] = Counter()
    bash: Counter[str] = Counter()
    for r in rows:
        gates.update(r.get("gate_blocks") or {})
        bash.update(r.get("bash_errors") or {})
    return {
        "n": len(rows),
        "median_llm_calls": med("llm_calls"),
        "median_tool_calls": med("tool_calls"),
        "mean_tool_error_rate": mean("tool_error_rate"),
        "total_repeat_calls": sum(r.get("repeat_calls", 0) for r in rows),
        "mean_read_before_edit": mean("read_before_edit"),
        "verified_after_edit_rate": mean("verified_after_edit"),
        "median_steps_after_last_edit": med("steps_after_last_edit"),
        "mean_parallel_share": mean("parallel_share"),
        "gate_blocks": dict(gates),
        "bash_errors": dict(bash),
        "total_env_errors": bash["env"],
        "max_turns_hit": sum(1 for r in rows if r.get("max_turns_hit")),
    }


def _files(target: Path) -> list[Path]:
    return sorted(target.glob("*.jsonl")) if target.is_dir() else [target]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="evals.trajectory_metrics")
    p.add_argument("target", type=Path, help="a trajectory .jsonl or a directory of them")
    p.add_argument("--json", action="store_true", help="print raw JSON")
    args = p.parse_args(argv)

    per = {f.stem: metrics(load(f)) for f in _files(args.target)}
    summary = aggregate(list(per.values()))
    if args.json:
        print(json.dumps({"tasks": per, "summary": summary}, ensure_ascii=False, indent=2))
        return 0
    cols = ["llm_calls", "tool_calls", "tool_error_rate", "repeat_calls",
            "read_before_edit", "verified_after_edit", "steps_after_last_edit", "parallel_share"]
    print("task".ljust(28) + "".join(c[:12].rjust(13) for c in cols))
    for name, m in per.items():
        print(name[:27].ljust(28) + "".join(str(m.get(c)).rjust(13) for c in cols))
    print("\n" + json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
