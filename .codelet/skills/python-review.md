---
name: python-review
description: Audit Python files for unused imports, TODO comments, and obvious lint issues
triggers: [review, lint, python]
allowed_tools: [glob, grep, read_file]
---

# Python Review Procedure

When asked to audit Python code, follow these steps:

1. **Discover scope** — use `glob` with pattern `**/*.py` (skip `tests/`, `build/`,
   `.venv/`, `__pycache__/` when the user hasn't asked about them).
2. **Find TODOs** — `grep` for `r"TODO|FIXME|XXX"` across the file list. Cluster
   matches by file in your final report.
3. **Spot unused imports** — for each file, read the import block and grep the
   rest of the file for each imported name. Star-imports (`from x import *`)
   should be flagged but not deeply analyzed.
4. **Report shape** — group findings by file, not by category. For each file:
   - one line per TODO with the line number
   - one line per likely-unused import
   - one line of "fix suggestion" only when the fix is unambiguous

5. **Stop conditions** — if more than 20 files match, ask the user to narrow
   scope rather than producing a wall of text.
