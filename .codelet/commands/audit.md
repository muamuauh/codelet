---
description: Audit the repo for TODO comments and unused imports
---
Please audit this repository:

1. Use `glob` to find every `.py` file (excluding tests/, build/, .venv/, __pycache__/).
2. Use `grep` to find every TODO/FIXME/XXX comment, grouping matches by file.
3. For the files in scope ({args} or all if empty), spawn parallel `task` calls
   that each list unused imports for a slice of the files.
4. Track progress with `todo_write` so I can see what's done.
5. Group findings by file in your final report and suggest a fix for each.

Stop and ask if more than 20 files match -- otherwise the report becomes a wall of text.
