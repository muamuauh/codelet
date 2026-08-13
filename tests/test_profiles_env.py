"""Tests for .env loading + profile resolution (P5)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from codelet.settings import (
    DEFAULT_API_KEY_ENV,
    load_env_files,
    load_settings,
    resolve_profile,
)


@pytest.fixture
def clean_env(monkeypatch):
    """Strip any pre-existing API key env vars so tests are deterministic."""
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY",
                "OPENROUTER_API_KEY", "OLLAMA_API_KEY", "MOONSHOT_API_KEY",
                "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "LLM_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    yield monkeypatch


# ---------- .env loader ----------

def test_env_loader_reads_kv_pairs(tmp_path: Path, clean_env):
    project = tmp_path / "p"
    project.mkdir()
    (project / ".env").write_text(
        '# comment\n'
        'DEEPSEEK_API_KEY=sk-deepseek\n'
        'OPENAI_API_KEY="sk-openai"\n'
        "QUOTED_SINGLE='hello world'\n"
        '\n'
        'BARE=42\n',
        encoding="utf-8",
    )
    loaded = load_env_files(project_dir=project, user_dir=tmp_path / "u")
    assert any(p.name == ".env" for p in loaded)
    assert os.environ["DEEPSEEK_API_KEY"] == "sk-deepseek"
    assert os.environ["OPENAI_API_KEY"] == "sk-openai"
    assert os.environ["QUOTED_SINGLE"] == "hello world"
    assert os.environ["BARE"] == "42"


def test_env_loader_does_not_override_shell_env(tmp_path: Path, clean_env):
    """Shell env wins -- a .env entry must not clobber a pre-set value."""
    clean_env.setenv("ANTHROPIC_API_KEY", "from-shell")
    project = tmp_path / "p"
    project.mkdir()
    (project / ".env").write_text("ANTHROPIC_API_KEY=from-dotenv\n", encoding="utf-8")
    load_env_files(project_dir=project, user_dir=tmp_path / "u")
    assert os.environ["ANTHROPIC_API_KEY"] == "from-shell"


def test_env_loader_skips_malformed_lines(tmp_path: Path, clean_env):
    project = tmp_path / "p"
    project.mkdir()
    (project / ".env").write_text(
        "GOOD=ok\n"
        "no_equals_sign\n"
        "1starts_with_digit=bad\n"
        "  =empty_key\n",
        encoding="utf-8",
    )
    load_env_files(project_dir=project, user_dir=tmp_path / "u")
    assert os.environ["GOOD"] == "ok"
    assert "no_equals_sign" not in os.environ


def test_env_loader_no_files_no_error(tmp_path: Path, clean_env):
    loaded = load_env_files(project_dir=tmp_path / "missing", user_dir=tmp_path / "absent")
    assert loaded == []


# ---------- resolve_profile ----------

def test_resolve_profile_default_anthropic(clean_env):
    clean_env.setenv("ANTHROPIC_API_KEY", "sk-test")
    out = resolve_profile({}, profile_name=None)
    assert out["name"] == "anthropic"
    assert out["provider"] == "anthropic"
    assert out["api_key"] == "sk-test"
    assert out["api_key_env"] == "ANTHROPIC_API_KEY"


def test_resolve_profile_named_deepseek(clean_env):
    clean_env.setenv("DEEPSEEK_API_KEY", "sk-deepseek")
    settings = {
        "profiles": {
            "deepseek": {
                "provider": "openai",
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-chat",
                "api_key_env": "DEEPSEEK_API_KEY",
            },
        },
    }
    out = resolve_profile(settings, profile_name="deepseek")
    assert out["provider"] == "openai"
    assert out["base_url"] == "https://api.deepseek.com/v1"
    assert out["model"] == "deepseek-chat"
    assert out["api_key"] == "sk-deepseek"


def test_resolve_profile_missing_env_returns_none(clean_env):
    """If api_key_env points to an unset var, api_key is None (not blocking).
    The CLI will surface a friendly 'API key not set' message later."""
    settings = {
        "profiles": {
            "openrouter": {
                "provider": "openai",
                "base_url": "https://openrouter.ai/api/v1",
                "model": "anthropic/claude-3.5-sonnet",
                "api_key_env": "OPENROUTER_API_KEY",
            },
        },
    }
    out = resolve_profile(settings, profile_name="openrouter")
    assert out["api_key"] is None
    assert out["api_key_env"] == "OPENROUTER_API_KEY"


def test_resolve_profile_settings_default_picks_active(clean_env):
    """settings['profile'] sets the default when the CLI didn't specify."""
    clean_env.setenv("DEEPSEEK_API_KEY", "sk-d")
    settings = {
        "profile": "deepseek",
        "profiles": {
            "deepseek": {
                "provider": "openai",
                "model": "deepseek-chat",
                "api_key_env": "DEEPSEEK_API_KEY",
            },
        },
    }
    out = resolve_profile(settings, profile_name=None)
    assert out["name"] == "deepseek"
    assert out["api_key"] == "sk-d"


def test_resolve_profile_unknown_name_falls_back_to_provider(clean_env):
    """`--profile anthropic` works even with empty settings."""
    clean_env.setenv("ANTHROPIC_API_KEY", "k")
    out = resolve_profile({}, profile_name="anthropic")
    assert out["provider"] == "anthropic"
    assert out["api_key"] == "k"


def test_default_api_key_env_table_contains_main_providers():
    assert DEFAULT_API_KEY_ENV["anthropic"] == "ANTHROPIC_API_KEY"
    assert DEFAULT_API_KEY_ENV["openai"] == "OPENAI_API_KEY"


# ---------- env-driven (LLM_*) profile ----------

def test_env_driven_triple_resolves_without_named_profile(clean_env):
    """When LLM_API_KEY + LLM_BASE_URL + LLM_MODEL are set and no profile is
    chosen, build a one-shot profile from those env vars -- no settings.json
    needed at all."""
    clean_env.setenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
    clean_env.setenv("LLM_API_KEY", "sk-from-env")
    clean_env.setenv("LLM_MODEL", "deepseek-chat")

    out = resolve_profile({}, profile_name=None)
    assert out["name"] == "_env"
    assert out["provider"] == "openai"  # default for env-driven path
    assert out["base_url"] == "https://api.deepseek.com/v1"
    assert out["api_key"] == "sk-from-env"
    assert out["model"] == "deepseek-chat"


def test_env_driven_provider_override(clean_env):
    """LLM_PROVIDER=anthropic switches the env-driven path to Anthropic."""
    clean_env.setenv("LLM_API_KEY", "sk-anthropic-via-env")
    clean_env.setenv("LLM_PROVIDER", "anthropic")
    out = resolve_profile({}, profile_name=None)
    assert out["provider"] == "anthropic"
    assert out["api_key"] == "sk-anthropic-via-env"


def test_env_driven_path_only_when_no_explicit_name(clean_env):
    """If user passes --profile X, the env-driven shortcut MUST NOT override.
    Explicit choice always wins."""
    clean_env.setenv("LLM_API_KEY", "sk-env")
    clean_env.setenv("ANTHROPIC_API_KEY", "sk-ant")
    out = resolve_profile({}, profile_name="anthropic")
    assert out["name"] == "anthropic"
    assert out["api_key"] == "sk-ant"


def test_env_driven_skipped_when_no_env_vars_set(clean_env):
    """No LLM_* and no settings -> falls back to default 'anthropic'."""
    clean_env.setenv("ANTHROPIC_API_KEY", "k")
    out = resolve_profile({}, profile_name=None)
    assert out["name"] == "anthropic"


# ---------- inline api_key in profile ----------

def test_profile_inline_api_key_takes_priority(clean_env):
    """A profile that ships its own api_key inline doesn't need any env var."""
    settings = {
        "profiles": {
            "myproxy": {
                "provider": "openai",
                "base_url": "https://proxy.example/v1",
                "model": "gpt-4o",
                "api_key": "sk-inline",
            },
        },
    }
    out = resolve_profile(settings, profile_name="myproxy")
    assert out["api_key"] == "sk-inline"


def test_profile_inline_api_key_overrides_env(clean_env):
    clean_env.setenv("OPENAI_API_KEY", "sk-from-env")
    settings = {
        "profiles": {
            "myproxy": {
                "provider": "openai",
                "base_url": "https://proxy.example/v1",
                "api_key": "sk-inline",
            },
        },
    }
    out = resolve_profile(settings, profile_name="myproxy")
    # Inline wins over env var lookup.
    assert out["api_key"] == "sk-inline"


# ---------- CLI: --base-url alone infers openai ----------

def test_cli_base_url_without_provider_infers_openai(clean_env):
    """The user's case: pass just --base-url + --api-key + --model and it
    should Just Work without having to remember --provider openai."""
    from argparse import Namespace
    from codelet.cli import _build_config
    from codelet.config import LLMProvider

    clean_env.setenv("ANTHROPIC_API_KEY", "should-not-be-used")
    args = Namespace(
        profile=None, provider=None, model="custom-model",
        base_url="https://custom.example/v1", api_key="sk-custom",
        mode=None, max_turns=None,
    )
    cfg = _build_config(args, {})
    assert cfg.provider == LLMProvider.OPENAI
    assert cfg.base_url == "https://custom.example/v1"
    assert cfg.model == "custom-model"
    assert cfg.api_key == "sk-custom"


def test_cli_base_url_with_explicit_provider_does_not_infer(clean_env):
    """Explicit --provider anthropic + --base-url should stay anthropic."""
    from argparse import Namespace
    from codelet.cli import _build_config
    from codelet.config import LLMProvider

    args = Namespace(
        profile=None, provider="anthropic", model=None,
        base_url="https://custom.anthropic-proxy/v1", api_key=None,
        mode=None, max_turns=None,
    )
    cfg = _build_config(args, {})
    assert cfg.provider == LLMProvider.ANTHROPIC
    assert cfg.base_url == "https://custom.anthropic-proxy/v1"


# ---------- settings.profiles merging ----------

def test_settings_profiles_user_and_project_merge(tmp_path: Path):
    user = tmp_path / "user.json"
    project_dir = tmp_path / "p"
    user.write_text(json.dumps({
        "profiles": {
            "anthropic": {"provider": "anthropic", "model": "claude-from-user"},
        },
    }), encoding="utf-8")
    proj_settings = project_dir / ".codelet" / "settings.json"
    proj_settings.parent.mkdir(parents=True)
    proj_settings.write_text(json.dumps({
        "profiles": {
            "deepseek": {"provider": "openai", "model": "deepseek-chat"},
        },
    }), encoding="utf-8")

    merged = load_settings(project_dir=project_dir, user_path=user)
    assert "anthropic" in merged["profiles"]
    assert "deepseek" in merged["profiles"]
    # Project overrides user when names collide
    proj_settings.write_text(json.dumps({
        "profiles": {
            "anthropic": {"provider": "anthropic", "model": "claude-from-project"},
        },
    }), encoding="utf-8")
    merged = load_settings(project_dir=project_dir, user_path=user)
    assert merged["profiles"]["anthropic"]["model"] == "claude-from-project"
