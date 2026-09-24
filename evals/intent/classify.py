"""Router classification eval: does it restrict the right turns?

Two numbers matter more than accuracy:

  false restriction  gold edit/command turns the router made read-only. Each one
                     costs the user a confirmation round-trip. Keep it near zero.
  protection         gold question/plan/unclear turns that did run read-only.

Anything the router leaves unrouted runs exactly as without it, so a miss in
either direction is never worse than not having the router.

Usage:
    python -m evals.intent.classify                      # rules only, dev + test
    python -m evals.intent.classify --llm --model claude-haiku-4-5
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter

from rich.console import Console
from rich.table import Table

from codelet.plugins.builtin.router import RESTRICTED, classify, llm_classify
from evals.intent.dataset import split

GOLD_LABELS = ("question", "plan", "unclear", "edit", "command")


def evaluate(items: list[tuple[str, str]], llm=None) -> dict:
    confusion: Counter = Counter()
    llm_calls = 0
    rows = []
    for text, gold in items:
        pred, _ = classify(text)
        if pred is None and llm is not None:
            llm_calls += 1
            pred = llm(text)
        confusion[(gold, pred or "unrouted")] += 1
        rows.append((text, gold, pred))
    ro_gold = [(g, p) for _, g, p in rows if g in RESTRICTED]
    rw_gold = [(g, p) for _, g, p in rows if g not in RESTRICTED]
    return {
        "n": len(rows),
        "exact": sum(g == p for _, g, p in rows),
        "false_restriction": sum(p in RESTRICTED for _, p in rw_gold),
        "n_should_run": len(rw_gold),
        "protected": sum(p in RESTRICTED for _, p in ro_gold),
        "n_should_restrict": len(ro_gold),
        "unrouted": sum(p is None for _, _, p in rows),
        "llm_calls": llm_calls,
        "confusion": confusion,
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--llm", action="store_true", help="Ask a model when the rules have no signal.")
    p.add_argument("--model", default=None)
    p.add_argument("--profile", default=None)
    p.add_argument("--show", choices=["dev", "test"], default=None, help="Print that split's misses.")
    args = p.parse_args(argv)

    llm = None
    if args.llm:
        from codelet.cli import _build_config
        from codelet.llm.factory import build_client
        from codelet.settings import load_env_files, load_settings
        load_env_files()
        cfg = _build_config(argparse.Namespace(profile=args.profile, provider=None, model=None,
                                               base_url=None, api_key=None, mode=None, max_turns=None),
                            load_settings())
        client, model = build_client(cfg), args.model or cfg.compact_model
        llm = lambda text: llm_classify(client, model, text)  # noqa: E731

    console = Console()
    table = Table(title="Intent router" + (" + LLM fallback" if llm else " (rules only)"))
    for col in ("split", "exact label", "false restriction", "protection", "left unrouted", "LLM calls"):
        table.add_column(col, justify="left" if col == "split" else "right")
    results = {}
    for name in ("dev", "test"):
        r = results[name] = evaluate(split(name), llm)
        table.add_row(name, f"{r['exact']}/{r['n']}",
                      f"{r['false_restriction']}/{r['n_should_run']}",
                      f"{r['protected']}/{r['n_should_restrict']}",
                      f"{r['unrouted']}/{r['n']}", str(r["llm_calls"]))
    console.print(table)

    for name in ("dev", "test") if args.show is None else (args.show,):
        if args.show is None:
            continue
        for text, gold, pred in results[name]["rows"]:
            if gold != pred:
                console.print(f"  [{gold:8} -> {pred or 'unrouted':8}] {text}", markup=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
