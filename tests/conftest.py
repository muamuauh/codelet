"""Global pytest fixtures to keep test runs isolated from the user's real ~/.codelet."""
from __future__ import annotations

from pathlib import Path
import pytest


@pytest.fixture(autouse=True)
def _isolate_global_user_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Redirect default SessionStore and workspace_states away from ~/.codelet."""
    monkeypatch.setattr("codelet.persistence.session.SESSION_DIR", tmp_path / "_test_sessions")
    try:
        import codelet.web.server  # noqa: F401
        monkeypatch.setattr(
            "codelet.web.server._workspace_states_file",
            lambda: tmp_path / "_test_workspace_states.json",
        )
    except Exception:
        pass
