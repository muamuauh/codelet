"""Frozen PyInstaller entry point for the Electron desktop sidecar backend."""
from __future__ import annotations

import os
import sys


def main() -> int:
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    # Ensure stdout/stderr exist under Windows windowed/noconsole launches.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

    from codelet.web.__main__ import main as web_main

    return web_main()


if __name__ == "__main__":
    raise SystemExit(main())
