"""settings.json layered loader + .env loader + profile resolver (P5).

Discovery order for settings.json (project overrides user; both optional):
    1. ~/.codelet/settings.json
    2. ./.codelet/settings.json   (project)

Discovery order for .env files (project loaded first; shell env always wins):
    1. ./.env                            (project root)
    2. ~/.codelet/.env

Schema (all fields optional):
    {
      "profile": "anthropic",            // selects from `profiles` below
      "profiles": {
        "anthropic": {
          "provider": "anthropic",
          "model": "claude-sonnet-4-5",
          "api_key_env": "ANTHROPIC_API_KEY"
        },
        "deepseek": {
          "provider": "openai",
          "base_url": "https://api.deepseek.com/v1",
          "model": "deepseek-chat",
          "api_key_env": "DEEPSEEK_API_KEY"
        }
        // ...
      },
      "model": "claude-sonnet-4-5",      // fallback if no profile
      "permission_mode": "ask",
      "max_turns": 30,
      "max_tokens": 8192,
      "context_window": 200000,
      "compact_threshold_ratio": 0.75,
      "compact_keep_recent": 4,
      "compact_model": "claude-haiku-4-5",
      "max_skills_in_index": 30,
      "hooks": {
        "PreToolUse":       [{"matcher": "bash", "command": "..."}],
        "PostToolUse":      [{"matcher": "*",    "command": "..."}],
        "UserPromptSubmit": [{"matcher": "*",    "command": "..."}]
      },
      "pricing": {
        "claude-sonnet-4-5": {"input": 3.0, "output": 15.0}
      }
    }

Merge semantics:
    - top-level scalar keys: project value replaces user value
    - dict-typed keys (hooks, pricing, profiles): merged shallowly per inner key
    - list-typed keys inside hooks (PreToolUse, ...): project list APPENDS to
      user list (so hooks compose). To override, use only project file.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# Built-in provider defaults: env var to read API key from when a profile
# doesn't specify `api_key_env`. Values are conventional names from each
# provider's docs.
DEFAULT_API_KEY_ENV: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError):
        return {}


def _merge_hooks(user_hooks: dict[str, Any], project_hooks: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    merged: dict[str, list[dict[str, Any]]] = {}
    for event in {"PreToolUse", "PostToolUse", "UserPromptSubmit"}:
        u = user_hooks.get(event) or []
        p = project_hooks.get(event) or []
        if u or p:
            merged[event] = list(u) + list(p)
    return merged


# ---------- .env loader ----------

def load_env_files(
    project_dir: str | Path | None = None,
    user_dir: str | Path | None = None,
) -> list[Path]:
    """Read KEY=VALUE pairs from .env files into os.environ.

    Honors three rules a Python dev expects from a .env loader:
      - shell env wins (`os.environ.setdefault`, never overwrites pre-set vars)
      - project `.env` is loaded BEFORE the user one, so the project's keys
        get first claim on each name (project tends to be more specific)
      - lines starting with `#`, blank lines, malformed lines are skipped
        without raising

    Quoting: surrounding "..." or '...' are stripped. No interpolation,
    multi-line values, or shell expansion -- this is intentionally tiny.

    Returns the list of files actually read (useful for tests / `/usage` panel).
    """
    if project_dir is None:
        project_dir = Path.cwd()
    else:
        project_dir = Path(project_dir)
    if user_dir is None:
        user_dir = Path.home() / ".codelet"
    else:
        user_dir = Path(user_dir)

    paths_in_order = [project_dir / ".env", user_dir / ".env"]
    loaded: list[Path] = []
    for path in paths_in_order:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if not key.isidentifier():  # skip junk like "1foo"
                continue
            value = value.strip()
            if (value.startswith('"') and value.endswith('"')) or (
                value.startswith("'") and value.endswith("'")
            ):
                value = value[1:-1]
            os.environ.setdefault(key, value)
        loaded.append(path)
    return loaded


# ---------- settings.json ----------

def load_settings(
    project_dir: str | Path | None = None,
    user_path: str | Path | None = None,
) -> dict[str, Any]:
    """Load and merge user + project settings.json. Missing files are silently ignored."""
    if user_path is None:
        user_path = Path.home() / ".codelet" / "settings.json"
    else:
        user_path = Path(user_path)

    if project_dir is None:
        project_dir = Path.cwd()
    else:
        project_dir = Path(project_dir)
    project_path = project_dir / ".codelet" / "settings.json"

    user_cfg = _read_json(user_path)
    project_cfg = _read_json(project_path)

    merged: dict[str, Any] = dict(user_cfg)
    for key, value in project_cfg.items():
        if key == "hooks":
            merged["hooks"] = _merge_hooks(user_cfg.get("hooks") or {}, value or {})
        elif key == "pricing" and isinstance(value, dict):
            base = dict(user_cfg.get("pricing") or {})
            base.update(value)
            merged["pricing"] = base
        elif key == "profiles" and isinstance(value, dict):
            base = dict(user_cfg.get("profiles") or {})
            base.update(value)  # project profile entries replace user ones with same name
            merged["profiles"] = base
        else:
            merged[key] = value

    # Ensure hooks is always a dict so callers can index without checks.
    merged.setdefault("hooks", _merge_hooks(user_cfg.get("hooks") or {}, project_cfg.get("hooks") or {}))
    return merged


# ---------- profile resolution ----------

def resolve_profile(settings: dict[str, Any], profile_name: str | None = None) -> dict[str, Any]:
    """Pick the active profile and resolve its api_key from os.environ.

    Resolution order for the profile *name*:
        1. explicit `profile_name` argument (typically the CLI --profile flag)
        2. settings["profile"]
        3. the special name `"_env"` if `LLM_API_KEY` or `LLM_BASE_URL` is set
           in the environment -- lets users run any OpenAI-compatible endpoint
           by setting just the (LLM_BASE_URL, LLM_API_KEY, LLM_MODEL) triple
           in `.env` without touching settings.json
        4. "anthropic"

    For api_key resolution within a profile (highest priority first):
        a. inline `api_key` field in the profile entry (self-contained config)
        b. env var named by `api_key_env`
        c. env var derived from provider name (DEFAULT_API_KEY_ENV)

    The returned dict has the shape:
        {
            "name": str,                 // resolved profile name
            "provider": "anthropic" | "openai",
            "model": str | None,
            "base_url": str | None,
            "api_key": str | None,       // None if missing -> CLI surfaces a friendly error
            "api_key_env": str | None,   // for diagnostics ("export X=...")
        }

    Falls back gracefully when the named profile is missing from settings:
    treats the name itself as a provider (so `--profile anthropic` works
    even with an empty settings.json).
    """
    profiles = settings.get("profiles") or {}
    name = profile_name or settings.get("profile")

    # Implicit env-driven profile: when the user hasn't picked one and the
    # generic LLM_* triple is set, build a one-shot profile from those vars.
    if name is None:
        if os.environ.get("LLM_API_KEY") or os.environ.get("LLM_BASE_URL"):
            return {
                "name": "_env",
                "provider": os.environ.get("LLM_PROVIDER") or "openai",
                "model": os.environ.get("LLM_MODEL") or settings.get("model"),
                "base_url": os.environ.get("LLM_BASE_URL"),
                "api_key": os.environ.get("LLM_API_KEY"),
                "api_key_env": "LLM_API_KEY",
            }
        name = "anthropic"

    if name in profiles and isinstance(profiles[name], dict):
        prof = dict(profiles[name])
    else:
        # No profile entry; treat name as a bare provider.
        prof = {"provider": name}

    provider = prof.get("provider") or name

    # api_key resolution: inline > api_key_env > provider default
    api_key: str | None = prof.get("api_key")
    api_key_env = prof.get("api_key_env") or DEFAULT_API_KEY_ENV.get(provider, "")
    if api_key is None and api_key_env:
        api_key = os.environ.get(api_key_env)

    # Top-level `model` (outside profiles) is a fallback for profiles that
    # don't specify their own.
    model = prof.get("model") or settings.get("model")

    return {
        "name": name,
        "provider": provider,
        "model": model,
        "base_url": prof.get("base_url"),
        "api_key": api_key,
        "api_key_env": api_key_env or None,
    }
