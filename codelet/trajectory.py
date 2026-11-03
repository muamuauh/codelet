"""Per-step trajectory: what the agent was asked, called, got back, and decided.

Telemetry answers "how much did this cost"; the eval report answers "did it
pass". Neither says *how* the agent got there -- which is what you need when a
task fails, or passes the slow way. A Trajectory is that record: one JSON line
per step, written as it happens, so a crash still leaves everything up to it.

Event kinds (every row also has v / run_id / agent / seq / step / ts):

  user_turn   prompt, turn policy label, read_only
  llm_call    latency_ms, stop_reason, usage, cost_usd, text, tool_calls
  tool_call   tool_use_id, name, input, outcome, is_error, output,
              output_chars, latency_ms, batch (size of the parallel round)
  compaction  cleared (L1 outputs), summaries (L2)
  turn_end    final_chars, llm_calls, max_turns_hit
  error       where, error

`step` is the index of the LLM call within one agent; a tool_call carries the
step of the LLM call that requested it. `outcome` is one of ok / error /
hook_blocked / permission_denied / readonly_blocked / user_rejected /
unknown_tool / disabled -- the gates are separate outcomes because "the model
asked for something it may not do" and "the tool broke" are different failures.

Inputs, outputs and texts are clipped (full tool outputs already go to disk via
the context layer) and passed through a secret mask before they are written.
Recording never raises into the loop: a full disk costs the trace, not the run.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
INPUT_CAP = 4000
OUTPUT_CAP = 2000
TEXT_CAP = 2000

# Same families the memory plugin refuses to store: provider keys, AWS keys,
# PEM blocks, and key=value credentials.
_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{16,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
    r"|(?i:(?:api[_-]?key|password|secret|token)\s*[=:]\s*)\S+)"
)


def mask(text: str) -> str:
    return _SECRET_RE.sub("[REDACTED]", text)


def clip(text: str, cap: int) -> str:
    text = mask(text)
    if len(text) <= cap:
        return text
    return f"{text[:cap]}…[+{len(text) - cap} chars]"


def _clip_value(value: Any, cap: int) -> Any:
    """Clip every string inside a tool input, keeping its shape for analysis."""
    if isinstance(value, str):
        return clip(value, cap)
    if isinstance(value, dict):
        return {k: _clip_value(v, cap) for k, v in value.items()}
    if isinstance(value, list):
        return [_clip_value(v, cap) for v in value]
    return value


class Trajectory:
    """Append-only step log for one run (an eval task, a CLI session)."""

    def __init__(self, path: Path | None = None, *, run_id: str | None = None,
                 agent: str = "main", meta: dict[str, Any] | None = None,
                 _shared: dict[str, Any] | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.agent = agent
        self.step = 0
        # seq and the in-memory event list are shared with child views so a
        # subagent's steps interleave correctly with its parent's.
        self._shared = _shared if _shared is not None else {"seq": 0, "events": []}
        if self.path is not None and _shared is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        if meta and _shared is None:
            self.record("run_start", **meta)

    @property
    def events(self) -> list[dict[str, Any]]:
        return self._shared["events"]

    def child(self, agent: str) -> "Trajectory":
        """A view for a subagent: same file and sequence, its own step counter."""
        return Trajectory(self.path, run_id=self.run_id, agent=agent, _shared=self._shared)

    def next_step(self) -> int:
        self.step += 1
        return self.step

    def record(self, kind: str, **fields: Any) -> dict[str, Any] | None:
        try:
            self._shared["seq"] += 1
            row = {
                "v": SCHEMA_VERSION,
                "run_id": self.run_id,
                "agent": self.agent,
                "seq": self._shared["seq"],
                "step": self.step,
                "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "kind": kind,
                **fields,
            }
            self.events.append(row)
            if self.path is not None:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            return row
        except Exception:  # noqa: BLE001 -- tracing must never break the agent
            return None

    # Convenience builders, so the loop's call sites stay one line each.

    def tool_input(self, tool_input: dict[str, Any]) -> Any:
        return _clip_value(tool_input, INPUT_CAP)

    @staticmethod
    def now_ms() -> float:
        return time.perf_counter() * 1000.0


def load(path: Path) -> list[dict[str, Any]]:
    """Read a trajectory file back; unparseable lines (a torn last write) are skipped."""
    rows: list[dict[str, Any]] = []
    with Path(path).open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows
