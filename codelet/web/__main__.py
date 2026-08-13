"""Launch the codelet web GUI:

    python -m codelet.web            # starts the server and opens your browser
    python -m codelet.web --no-open  # don't open a browser
    python -m codelet.web --port 9000
"""
from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import webbrowser


def _open_when_ready(url: str, host: str, port: int) -> None:
    """Wait (up to ~5s) for the server to accept connections, then open a browser."""
    probe = "127.0.0.1" if host in ("0.0.0.0", "") else host
    for _ in range(50):
        try:
            with socket.create_connection((probe, port), timeout=0.3):
                break
        except OSError:
            time.sleep(0.1)
    webbrowser.open(url)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="codelet.web", description="codelet web GUI")
    p.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1, local-only).")
    p.add_argument("--port", type=int, default=8000, help="Bind port (default 8000).")
    p.add_argument("--no-open", dest="open", action="store_false",
                   help="Do not open a browser window on startup.")
    p.set_defaults(open=True)
    args = p.parse_args(argv)

    try:
        import uvicorn
    except ImportError:
        print("The web GUI needs extra deps. Install with:\n"
              "  pip install -e \".[web]\"", file=sys.stderr)
        return 1

    from .server import create_app

    show_host = "127.0.0.1" if args.host in ("0.0.0.0", "") else args.host
    url = f"http://{show_host}:{args.port}"
    print(f"codelet web GUI -> {url}  (Ctrl-C to stop)")
    if args.open:
        threading.Thread(target=_open_when_ready, args=(url, args.host, args.port),
                         daemon=True).start()
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
