"""Mini eval harness for codelet.

Each task = an initial workspace + a prompt + execution-based checks. The runner
sets up the workspace in a temp dir, runs the real agent against it (sandboxed by
chdir), then runs the checks and reports pass-rate + token/cost.

Run:  python -m evals.runner
"""
