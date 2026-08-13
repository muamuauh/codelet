# SWE-bench Lite adapter

Run codelet against [SWE-bench Lite](https://www.swebench.com/) — 300 real
GitHub issues from popular Python projects. The agent must produce a code patch
that makes the hidden tests pass.

The workflow is **two stages** (the standard SWE-bench split):

```
   stage 1: generate predictions          stage 2: score them
   (this adapter, runs natively)          (official harness, runs in Docker)
   ┌───────────────────────────┐          ┌──────────────────────────────┐
   │ clone repo @ base_commit  │          │ for each prediction:         │
   │ run agent on the issue    │  ──────► │  apply model patch           │
   │ git diff = model_patch    │ predictions  apply gold test patch      │
   │ write predictions.jsonl   │  .jsonl  │  run FAIL_TO_PASS / PASS_..  │
   └───────────────────────────┘          │  resolved = all pass         │
                                          └──────────────────────────────┘
```

Why split? Each repo needs a specific Python version + pinned dependencies to run
its tests. The official harness handles that hell with per-instance Docker images.
Our agent only needs to *read and edit* the code, so it runs natively — no env
setup required to produce a patch.

## Prerequisites

```powershell
conda activate codelet
pip install datasets          # to load the dataset (stage 1)
pip install swebench          # to score (stage 2) — also needs Docker running
```

`git` and Docker Desktop must be installed. Stage 1 fetches repos by commit SHA
from GitHub, so it needs network access.

## Stage 1 — generate predictions

```powershell
$env:PYTHONUTF8="1"

# Sanity-check the whole pipeline with NO LLM (emits each instance's gold patch).
# These should later score as RESOLVED — proves clone/diff/format/Docker all work.
python -m evals.swebench.run_swebench --gold --limit 5

# Real run: agent solves N instances (costs tokens + time; thinking models are slow).
python -m evals.swebench.run_swebench --limit 1
python -m evals.swebench.run_swebench --instance marshmallow-code__marshmallow-1359 --verbose
python -m evals.swebench.run_swebench --limit 10 --profile deepseek --max-turns 40
```

Output: `evals/swebench/out/predictions.jsonl`, one line per instance:

```json
{"instance_id": "...", "model_name_or_path": "codelet", "model_patch": "diff --git ..."}
```

(Extra `_`-prefixed fields — tokens, calls, errors — are diagnostics the official
harness ignores.)

### Useful flags

| Flag | Meaning |
|---|---|
| `--limit N` | run the first N instances (default 1) |
| `--instance ID` | run a specific instance (repeatable) |
| `--gold` | emit gold patches instead of running the agent (no LLM) |
| `--profile` / `--model` | pick the LLM (same as the CLI) |
| `--max-turns N` | per-instance agent turn cap (default 40) |
| `--keep-clones` | keep temp clones for debugging |
| `--verbose` | stream live agent output |

## Stage 2 — score the predictions

The grader computes the **resolved rate** (the headline SWE-bench metric): it
applies each model patch + the gold test patch in a per-repo Docker image and
runs the target tests.

> ⚠️ **Windows note.** The official `swebench` harness does **not run natively on
> Windows** — it does `import resource` (a Unix-only stdlib module) at import
> time, so `python -m swebench.harness.run_evaluation` fails with
> `ModuleNotFoundError: No module named 'resource'` before Docker is ever
> touched. Use one of the two paths below.

### Option A — WSL2 (local Docker scoring) ✅ verified path

This is the path we actually got working (sb-cli, Option B below, returned 0% even
for gold reference patches — treat it as unreliable). Docker Desktop shares its
daemon with WSL2, so run the official harness from inside an Ubuntu WSL2 shell.

**One-time setup inside WSL2 (Ubuntu):**

```bash
# Ubuntu 24.04 ships no pip/ensurepip and marks system python externally-managed;
# sudo may need a password. Bootstrap an isolated venv without any of that:
python3 -m venv --without-pip ~/sweb-venv
curl -fsSL https://bootstrap.pypa.io/get-pip.py | ~/sweb-venv/bin/python
~/sweb-venv/bin/python -m pip install swebench datasets
```

**Score (run from `$HOME`, not `/mnt/...`, so logs land on fast ext4):**

```bash
# If behind a localhost proxy (e.g. Clash), point the harness at it — the harness
# fetches each repo's requirements from raw.githubusercontent.com, which the GFW
# resets. With WSL mirrored networking the host proxy is reachable at 127.0.0.1.
export HTTPS_PROXY=http://127.0.0.1:7897 HTTP_PROXY=http://127.0.0.1:7897

cd ~
~/sweb-venv/bin/python -m swebench.harness.run_evaluation \
  --dataset_name princeton-nlp/SWE-bench_Lite \
  --predictions_path /mnt/e/codes/codelet/evals/swebench/out/predictions.jsonl \
  --max_workers 4 --run_id mcc-run --cache_level instance
```

First run builds a Docker image per repo (large, slow; ~14 min for astropy). It
writes per-instance `report.json` files under
`~/logs/run_evaluation/<run_id>/<model>/<instance_id>/` and, if the summary step
succeeds, a top-level report with the resolved rate.

**Gotchas we hit (Windows + China network):**

- *Docker CLI must be on PATH:* invoke scripts with a **login shell**
  (`wsl -d Ubuntu-24.04 -- bash -l script.sh`); a plain `bash script.sh` gives
  "docker: command not found".
- *Docker Desktop WSL integration is flaky here:* a localhost proxy in NAT mode
  breaks the integration bootstrap (0-byte `docker-desktop-user-distro` proxy →
  "Permission denied"/"Exec format error" dialogs). Fix by putting
  `[wsl2]\nnetworkingMode=mirrored\nautoProxy=true` in `%USERPROFILE%\.wslconfig`.
  It still resets on distro re-init / Docker Desktop updates — recover with
  `wsl --shutdown`, kill+relaunch Docker Desktop, then run the harness immediately
  in one continuous session.
- *Use `--cache_level instance`, not `env`.* swebench 4.x doesn't build images
  locally — it **pulls a prebuilt per-instance image** (`swebench/sweb.eval.x86_64.
  <instance>`) from Docker Hub. `--cache_level env` only keeps base/env images, which
  in that flow never exist, so every pulled image is deleted right after use and each
  run re-downloads gigabytes. `instance` keeps them (~1-2GB each, so budget ~50-100GB
  for 50 instances) and makes re-runs and model comparisons far faster.
- *The summary step can crash* (`make_run_report` fetches every repo's
  requirements from raw.githubusercontent.com). The eval itself still finishes and
  the per-instance `report.json` files are valid — summarize them directly:

  ```bash
  # resolved rate only:
  python3 evals/swebench/summarize_reports.py --run-id mcc-run
  # + per-instance tokens / calls / time / turn-cap, split resolved vs unresolved:
  python3 evals/swebench/summarize_reports.py --run-id mcc-run \
      --predictions evals/swebench/out/predictions.jsonl --max-turns 40
  # add --price-in / --price-out (USD per 1M tokens) for cost and cost-per-resolved
  ```

### Option B — sb-cli (cloud scoring, no local Docker) ⚠️ unreliable here

> **In our testing (2026-07) sb-cli scored 0% even for verbatim gold reference
> patches**, across multiple runs — i.e. its results were meaningless, and
> `get-report` intermittently 500s. Gold patches must score ~100% if scoring
> works, so we treat sb-cli as broken for this setup and use Option A. Try it if
> you like, but **gold-gate it first** and don't trust any number until gold comes
> back ~100%.

The SWE-bench team hosts a cloud evaluator — easiest on Windows:

```powershell
pip install sb-cli
# get a free API key: https://www.swebench.com/sb-cli/
sb-cli submit swe-bench_lite test --predictions_path evals/swebench/out/predictions.jsonl
```

It runs the same evaluation server-side and returns the resolved rate.

### Sanity-gate with --gold first

Before spending tokens on real runs, score the `--gold` predictions — they
should come back ~100% resolved. If they don't, your scoring setup (not the
agent) is the problem.

## Cost & scope notes

- Real runs cost tokens and are **slow** (cloning big repos + many agent turns;
  thinking models like qwen are slower still). Start with `--limit 1`.
- This adapter generates the **model patch only**; it never touches the gold test
  patch — the harness applies that itself, so the agent can't game the tests.
- The prompt tells the agent not to edit test files; if it does anyway, those
  edits are still captured in the diff but the harness resets tests to gold.
- For a quick offline pipeline check (no network/LLM), see
  `tests/test_swebench_adapter.py`.

## Results so far

Scored locally with the official harness via Option A (WSL2). Small samples — not
statistically significant, but a real, reproducible signal:

| run | agent / model | resolved | avg in-tok/inst | notes |
|---|---|---|---|---|
| gold-gate | gold reference | 1/1 | — | proves the local scoring chain works |
| agent-3 | codelet · qwen3.7-max | 2/3 | — | astropy-12907 ✅, 14182 ✅, 14365 ❌ |
| agent-10 | codelet · qwen3.7-max | 5/10 (50%) | 169k | first 10 of the dataset (6 astropy + 4 django) |
| kimi-10 | codelet · kimi-k3 | 9/10 (90%) | 331k | same 10 instances; only missed astropy-7746 |
| **kimi-50** | codelet · kimi-k3 | **42/50 (84%)** | 321k | first 50 (6 astropy + 44 django); 5 empty patches count as unresolved (93.3% of the 45 scored) |

**At n=50, kimi-k3 holds up: 84%** (42/50) — close to its 90% on the 10-instance
sample, so that number wasn't a fluke. Counting convention matters: 5 instances where
the agent produced *no diff at all* never reach the harness (nothing to apply) and are
**unresolved** by the SWE-bench metric; ignoring them would inflate the score to 93.3%.
`summarize_reports.py` reports both. Caveat: qwen is still only at n=10, so the
head-to-head below is not an equal-n comparison.

### Failure attribution (`analyze_failures.py`)

`resolved: false` is one bit; joining predictions + report.json + the gold patch says
*how* it failed. On kimi-50 the 8 failures split:

| category | n | meaning |
|---|---|---|
| `agent_error` | 4 | 3× git clone `schannel: failed to receive handshake` (**0 LLM calls — the agent never ran**), 1× APIConnectionError mid-run |
| `wrong_logic` | 2 | edited exactly the file the gold patch edits, but the fix was wrong |
| `regression` | 1 | fixed the bug but broke a previously-passing test |
| `empty_patch` | 1 | ran 1 turn, produced no diff |

**Three of the eight "failures" were network flakes, not model failures** — over a
5-hour run, transient TLS errors aborted the clone before the agent started. Excluding
them: **89.4%** (42/47), back in line with the n=10 result. Two fixes came out of this:
`prepare_repo` now retries the fetch with backoff, and the summarizer counts
"agent never ran" instances separately instead of silently scoring them as model
failures. Note what is *not* in the table: no `wrong_file` — kimi localized the right
file every time it got to run; its genuine misses are reasoning, not search.

```bash
python evals/swebench/analyze_failures.py \
    --predictions evals/swebench/out/predictions_kimi50.jsonl --run-id kimi-50
```

**Model comparison (same agent, same 10 instances).** kimi-k3 resolved 9/10 vs
qwen3.7-max's 5/10 — the scaffold is identical, so this is a model-quality gap, not
an agent gap. kimi costs it: ~331k vs ~169k input tokens/instance (~2× the tokens for
~2× the resolve rate). The two even fail differently: qwen's *unresolved* runs burn
the most tokens (thrash → give up), whereas kimi's *resolved* runs are its priciest
(it grinds — two even hit the 40-turn cap and still resolved). Takeaway: on this
sample the bottleneck is the model, not the harness/prompt.

agent-10 breakdown — resolved: astropy-12907, astropy-14995, astropy-6938,
django-10914, django-11001; unresolved: astropy-14182, astropy-14365, astropy-7746,
django-10924, django-11019. **All 10 patches applied cleanly**
(`patch_successfully_applied: true`), so misses are logic, not malformed diffs.

Predictions are non-deterministic (astropy-14182 resolved in agent-3 but not in the
agent-10 regeneration). The same patches scored 0% via sb-cli (Option B) — a
broken-cloud artifact, not the agent.

**Efficiency signal** (from joining generation tokens via `summarize_reports.py
--predictions`): on agent-10 the resolved instances averaged **~55k input tokens /
10.4 calls**, the unresolved ones **~283k / 18.6 calls** — a ~5× gap. When the agent
starts thrashing (turns and tokens climbing) it's usually already lost; the wins are
cheap. None hit the turn cap (40), so misses are wrong answers, not truncation.
