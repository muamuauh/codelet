"""Launch the codelet web GUI:  python -m codelet.web  [--host H] [--port N]"""
from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="codelet.web", description="codelet web GUI")
    p.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1, local-only).")
    p.add_argument("--port", type=int, default=8000, help="Bind port (default 8000).")
    args = p.parse_args(argv)

    try:
        import uvicorn
    except ImportError:
        print("The web GUI needs extra deps. Install with:\n"
              "  pip install -e \".[web]\"", file=sys.stderr)
        return 1

    from .server import create_app

    print(f"codelet web GUI -> http://{args.host}:{args.port}  (Ctrl-C to stop)")
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
