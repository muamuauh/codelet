"""SWE-bench Lite adapter for codelet.

Two-stage, following the standard SWE-bench split:

  1. Prediction generation (this module, runs natively): shallow-clone each repo
     at its base_commit, run the agent against the issue text, and capture
     `git diff` as the model patch -> predictions.jsonl.

  2. Evaluation (official `swebench` harness, runs in Docker): applies each patch
     + the gold test patch inside a per-repo image and runs the target tests.

See evals/swebench/README.md for the full workflow.
"""
