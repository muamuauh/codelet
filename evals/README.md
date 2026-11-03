# Mini eval harness

A tiny, execution-based eval framework for the agent. Each **task** describes a
starting workspace + a prompt + checks; the runner sets up the workspace, runs
the real agent against it (sandboxed by `chdir`), then runs the checks and
reports pass-rate, tokens, and cost.

## Run

```powershell
conda activate codelet
$env:PYTHONUTF8="1"

python -m evals.runner                      # run every task in evals/tasks
python -m evals.runner --filter slug        # only tasks whose name contains "slug"
python -m evals.runner --task evals/tasks/fix_off_by_one.yaml
python -m evals.runner --profile deepseek --max-turns 20 --verbose
```

It uses the **same LLM config as the CLI** (`.env` + `settings.json` + `--profile`
/ `--model`). Runs in AUTO permission mode with streaming off. A JSON report is
written to `evals/reports/` (gitignored) unless `--no-json`.

Exit code is `0` only if every task passes (handy for CI).

## Task format (`evals/tasks/*.yaml`)

```yaml
name: fix_off_by_one              # optional; defaults to the filename
description: Fix an off-by-one bug
prompt: |                          # the instruction handed to the agent
  calc.py 里的 add 有 bug，修正它。
files:                             # written into the workspace before running
  calc.py: |
    def add(a, b):
        return a + b + 1
  test_calc.py: |
    from calc import add
    def test_add():
        assert add(2, 3) == 5
checks:                            # ALL must pass for the task to pass
  - cmd: python -m pytest -q       # passes if the command exits 0
  - file: calc.py                  # file-content assertions:
    not_contains: "a + b + 1"      #   contains / not_contains / exists
```

### Check types

| Check | Passes when |
|---|---|
| `{cmd: "<shell>", timeout?: 60}` | the command exits 0 (run in the workspace) |
| `{file: "p", contains: "s"}` | file `p` exists and contains substring `s` |
| `{file: "p", not_contains: "s"}` | file `p` exists and does NOT contain `s` |
| `{file: "p", exists: true}` | file `p` exists (or not, if `false`) |

`cmd` checks need their tools on PATH — run from the activated conda env so
`python` / `pytest` resolve to the project environment.

## Adding tasks

Drop a new `*.yaml` in `evals/tasks/`. Keep checks **execution-based** (a test
the patch must pass) rather than asserting on the agent's prose — that's what
makes the score objective. Put any test files the checks rely on under `files:`
so the agent can read them but the workspace stays self-contained.

## Trajectories, process metrics, pass@k

Every task run records a per-step trajectory (`codelet/trajectory.py`) to
`evals/reports/traj/<stamp>/<task>[#trial].jsonl`, and the JSON report carries
process metrics read off it (`evals/trajectory_metrics.py`): read-before-edit,
verified-after-edit, repeated identical calls, gate blocks by kind, parallel
share, steps after the last edit. `--repeat k` runs the suite k times
(trial-major) and reports pass@1 / pass@k / pass^k. See
[docs/evaluation.md](../docs/evaluation.md) for definitions and results.

Tasks may carry `category:` and a reference `solution:` (files overlaid on the
start state, never shown to the agent). `tests/test_eval_tasks.py` proves each
task valid: checks fail on the start files and pass with the solution;
`solution: {}` means "change nothing" (clarify tasks). `hidden_files:` are
written only after the agent stops, right before the checks (SWE-bench's
FAIL_TO_PASS / PASS_TO_PASS) -- the `hard` tasks use them with issue-style
prompts.

Model, prices and a spend cap can come from `.env`: `CODELET_EVAL_MODEL`,
`CODELET_EVAL_PRICE_INPUT` / `_OUTPUT` (USD per Mtok), `CODELET_EVAL_BUDGET_USD`.
With a budget and no price the runner refuses to start.

## What this is / isn't

This is a fast local feedback loop to catch regressions in *your* agent (edit
reliability, turn budget, permission gating). For cross-agent comparison or
contamination-free numbers, graduate to SWE-bench Verified or the Aider polyglot
benchmark — the adapter shape is the same: feed task → run agent → check.
