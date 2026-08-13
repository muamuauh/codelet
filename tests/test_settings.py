"""settings.json layered loader tests."""
from __future__ import annotations

import json
from pathlib import Path

from codelet.settings import load_settings


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_loads_empty_when_files_absent(tmp_path: Path):
    out = load_settings(project_dir=tmp_path, user_path=tmp_path / "missing.json")
    assert out == {"hooks": {}}


def test_project_overrides_user_for_scalars(tmp_path: Path):
    user = tmp_path / "user.json"
    project_dir = tmp_path / "p"
    _write(user, {"model": "user-model", "max_turns": 10})
    _write(project_dir / ".codelet" / "settings.json", {"model": "project-model"})

    merged = load_settings(project_dir=project_dir, user_path=user)
    assert merged["model"] == "project-model"
    assert merged["max_turns"] == 10  # user-only key survives


def test_hooks_lists_compose_user_then_project(tmp_path: Path):
    user = tmp_path / "user.json"
    project_dir = tmp_path / "p"
    _write(user, {"hooks": {"PreToolUse": [{"matcher": "bash", "command": "u1"}]}})
    _write(project_dir / ".codelet" / "settings.json", {
        "hooks": {"PreToolUse": [{"matcher": "*", "command": "p1"}]},
    })

    merged = load_settings(project_dir=project_dir, user_path=user)
    assert merged["hooks"]["PreToolUse"] == [
        {"matcher": "bash", "command": "u1"},
        {"matcher": "*", "command": "p1"},
    ]


def test_pricing_dict_merges_per_model(tmp_path: Path):
    user = tmp_path / "user.json"
    project_dir = tmp_path / "p"
    _write(user, {"pricing": {"claude-sonnet-4-5": {"input": 3.0, "output": 15.0}}})
    _write(project_dir / ".codelet" / "settings.json", {
        "pricing": {"claude-haiku-4-5": {"input": 1.0, "output": 5.0}},
    })

    merged = load_settings(project_dir=project_dir, user_path=user)
    assert "claude-sonnet-4-5" in merged["pricing"]
    assert "claude-haiku-4-5" in merged["pricing"]


def test_malformed_json_is_silently_ignored(tmp_path: Path):
    project_dir = tmp_path / "p"
    bad = project_dir / ".codelet" / "settings.json"
    bad.parent.mkdir(parents=True)
    bad.write_text("{ not json", encoding="utf-8")

    merged = load_settings(project_dir=project_dir, user_path=tmp_path / "missing.json")
    assert merged == {"hooks": {}}
