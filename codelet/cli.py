"""CLI entry point + interactive REPL."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .agent_loop import AgentLoop, PromptBlocked
from .config import Config, LLMProvider, PermissionMode
from .persistence.session import (
    SessionStore,
    list_project_sessions,
    list_sessions,
    load_session,
    restore_into,
)
from .settings import load_env_files, load_settings, resolve_profile
from .slash.loader import SlashCommandIndex, expand_command, load_commands
from .tools.base import ToolRegistry

BANNER = """[bold cyan]codelet[/bold cyan] [dim]v0.1.0 (P5)[/dim]
Enhanced fork: subagent + parallel + skill + hooks + telemetry + multi-provider.

Built-in commands:
  [yellow]/tools[/yellow]          list available tools
  [yellow]/skills[/yellow]         list loaded skills (project + user)
  [yellow]/commands[/yellow]       list user-defined slash commands
  [yellow]/todos[/yellow]          show the current todo list
  [yellow]/usage[/yellow]          show telemetry panel
  [yellow]/profile[/yellow]        show active provider/model/base_url
  [yellow]/sessions[/yellow]       list saved sessions
  [yellow]/resume[/yellow] <id>    restore a session into the running agent
  [yellow]/save[/yellow]           write a session snapshot to disk
  [yellow]/mode[/yellow] [m]       show or change permission mode (ask|auto|plan)
  [yellow]/help[/yellow]           show this banner
  [yellow]/quit[/yellow]           exit
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="codelet",
        description="codelet -- enhanced distilled agent loop",
    )
    p.add_argument(
        "--profile", default=None,
        help="Named profile from settings.json (e.g. anthropic, deepseek, openai-gpt4o).",
    )
    p.add_argument("--provider", choices=["anthropic", "openai"], default=None,
                   help="Override profile's provider.")
    p.add_argument("--model", default=None, help="Override profile's model.")
    p.add_argument("--base-url", default=None, help="Override profile's base URL.")
    p.add_argument("--api-key", default=None,
                   help="Override resolved API key (else read from env via .env / shell).")
    p.add_argument("--mode", choices=["ask", "auto", "plan"], default=None)
    p.add_argument("--max-turns", type=int, default=None)
    p.add_argument("--no-telemetry", action="store_true", help="Suppress the per-turn usage panel.")
    p.add_argument("--no-stream", action="store_true",
                   help="Disable live token streaming + spinner (wait for the full reply).")
    p.add_argument("--no-persist", action="store_true",
                   help="Do not auto-save the session after each turn.")
    p.add_argument("--resume", default=None,
                   help="Resume a saved session by id (run /sessions to list).")
    p.add_argument("prompt", nargs="?", default=None, help="One-shot prompt (else interactive).")
    return p


def _build_config(args: argparse.Namespace, settings: dict[str, Any]) -> Config:
    """Compose Config from defaults <- settings.json <- profile <- CLI flags.

    Priority order (highest wins):
        1. CLI flags (--provider, --model, --base-url, --api-key)
        2. Resolved profile (settings.profiles[name])
        3. Top-level settings.json scalars (model, permission_mode, ...)
        4. Hardcoded defaults

    Profile resolution itself is delegated to settings.resolve_profile, which
    looks up `--profile` (or settings["profile"], or "anthropic") and reads
    the api key from os.environ via the profile's `api_key_env`.
    """
    cfg = Config()

    # ---- settings.json scalar layer ----
    if "permission_mode" in settings:
        try:
            cfg.permission_mode = PermissionMode(settings["permission_mode"])
        except ValueError:
            pass
    for key in ("max_turns", "max_tokens", "context_window", "compact_keep_recent",
                "compact_summary_target_tokens"):
        if key in settings and isinstance(settings[key], int):
            setattr(cfg, key, settings[key])
    if "compact_threshold_ratio" in settings:
        try:
            cfg.compact_threshold_ratio = float(settings["compact_threshold_ratio"])
        except (TypeError, ValueError):
            pass
    if "compact_model" in settings:
        cfg.compact_model = str(settings["compact_model"])
    if "stream" in settings and isinstance(settings["stream"], bool):
        cfg.stream = settings["stream"]
    if "hooks" in settings and isinstance(settings["hooks"], dict):
        cfg.hooks = settings["hooks"]
    if "pricing" in settings and isinstance(settings["pricing"], dict):
        cfg.pricing_overrides = settings["pricing"]
    if "plugins" in settings and isinstance(settings["plugins"], dict):
        cfg.plugins = settings["plugins"]

    # ---- profile layer (provider, model, base_url, api_key) ----
    profile = resolve_profile(settings, args.profile)
    cfg.profile_name = profile.get("name")
    try:
        cfg.provider = LLMProvider(profile["provider"])
    except (KeyError, ValueError):
        pass
    if profile.get("model"):
        cfg.model = profile["model"]
    if profile.get("base_url"):
        cfg.base_url = profile["base_url"]
    if profile.get("api_key"):
        cfg.api_key = profile["api_key"]

    # ---- CLI flag layer (highest) ----
    if args.provider is not None:
        cfg.provider = LLMProvider(args.provider)
    elif args.base_url is not None and args.profile is None:
        # User passed --base-url without --provider and isn't on a named
        # profile: they almost certainly want an OpenAI-compatible endpoint
        # (Anthropic's base_url rarely needs override). Infer it.
        cfg.provider = LLMProvider.OPENAI
    if args.model is not None:
        cfg.model = args.model
    if args.base_url is not None:
        cfg.base_url = args.base_url
    if args.api_key is not None:
        cfg.api_key = args.api_key
    if args.mode is not None:
        cfg.permission_mode = PermissionMode(args.mode)
    if args.max_turns is not None:
        cfg.max_turns = args.max_turns
    if getattr(args, "no_stream", False):
        cfg.stream = False

    return cfg


def run_interactive(
    agent: AgentLoop,
    console: Console,
    *,
    show_telemetry: bool,
    persist: bool,
    slash_commands: SlashCommandIndex,
    session_store: SessionStore | None,
) -> None:
    console.print(Panel(BANNER, expand=False, border_style="cyan"))
    if session_store is not None and persist:
        console.print(f"[dim]session id: {session_store.id} (auto-saving to {session_store.path})[/dim]")

    while True:
        try:
            user_input = console.input("\n[bold green]>[/bold green] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Goodbye![/dim]")
            break

        if not user_input:
            continue

        if user_input.startswith("/"):
            handled, prompt = _handle_slash(user_input, agent, console, slash_commands, session_store)
            if not handled:  # /quit returns False
                break
            if prompt is None:
                continue
            # Slash command expanded into a prompt -> feed to the agent.
            user_input = prompt

        console.print()
        try:
            agent.run(user_input)
            console.print()
            if show_telemetry:
                console.print(agent.telemetry.render_panel())
            if persist and session_store is not None:
                try:
                    session_store.record(agent)
                except Exception as exc:
                    console.print(f"[dim yellow][session save skipped: {exc}][/dim yellow]")
        except PromptBlocked as exc:
            console.print(f"\n[yellow]Prompt blocked by hook:[/yellow] {exc}")
        except KeyboardInterrupt:
            console.print("\n[yellow](interrupted)[/yellow]")
        except Exception as exc:
            console.print(f"\n[red]Error:[/red] {exc}")


def _print_sessions(console: Console, *, project_limit: int = 15, global_limit: int = 10) -> None:
    """Two stacked sections: sessions touched from this working directory first,
    then the most recent sessions saved anywhere. Each row leads with a title."""
    cwd = Path.cwd()

    proj = list_project_sessions()
    console.print()
    if proj:
        ptable = Table(title=f"This project  ({cwd})", title_justify="left",
                       header_style="bold", show_edge=False, pad_edge=False)
        ptable.add_column("Title", style="white", max_width=50)
        ptable.add_column("Session ID", style="cyan", no_wrap=True)
        ptable.add_column("Updated", style="dim", no_wrap=True)
        ptable.add_column("Msgs", justify="right")
        for e in proj[:project_limit]:
            ptable.add_row(e.get("title") or "(empty)", e["id"],
                           e.get("updated_at", "?"), str(e.get("message_count", "?")))
        console.print(ptable)
    else:
        console.print(f"[bold]This project[/bold] [dim]({cwd})[/dim]")
        console.print("  [dim](no sessions started here yet)[/dim]")

    glob = list_sessions()
    console.print()
    if glob:
        shown = min(len(glob), global_limit)
        gtable = Table(title=f"All saved sessions  (newest {shown} of {len(glob)})",
                       title_justify="left", header_style="bold", show_edge=False, pad_edge=False)
        gtable.add_column("Title", style="white", max_width=50)
        gtable.add_column("Session ID", style="cyan", no_wrap=True)
        gtable.add_column("Updated", style="dim", no_wrap=True)
        gtable.add_column("Model", style="dim")
        for e in glob[:global_limit]:
            gtable.add_row(e.get("summary") or "(empty)", e["id"],
                           e.get("updated_at", "?"),
                           f"{e.get('provider', '?')}/{e.get('model', '?')}")
        console.print(gtable)
    else:
        console.print("[dim](no saved sessions)[/dim]")

    console.print("\n[dim]Resume any with /resume <id>[/dim]")


def _handle_slash(
    line: str,
    agent: AgentLoop,
    console: Console,
    slash_commands: SlashCommandIndex,
    session_store: SessionStore | None,
) -> tuple[bool, str | None]:
    """Process a slash command.

    Returns:
        (continue_repl, expanded_prompt)
        - continue_repl=False  -> exit the REPL (only /quit)
        - expanded_prompt!=None -> caller feeds it into agent.run as a prompt
    """
    parts = line.split(maxsplit=1)
    cmd = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""

    if cmd in ("/quit", "/exit", "/q"):
        console.print("[dim]Goodbye![/dim]")
        return False, None

    if cmd == "/tools":
        console.print("\n[bold]Available tools:[/bold]")
        for tool in agent.registry.all_tools():
            console.print(f"  [cyan]{tool.name}[/cyan] -- {tool.description}")
        return True, None

    if cmd == "/skills":
        names = agent.skill_index.names()
        if not names:
            console.print("[dim]No skills loaded. Drop *.md files into "
                          ".codelet/skills/ (project) or "
                          "~/.codelet/skills/ (user).[/dim]")
            return True, None
        console.print("\n[bold]Loaded skills:[/bold]")
        for name in names:
            skill = agent.skill_index.get(name)
            assert skill is not None
            src = f" [dim]({skill.source})[/dim]" if skill.source else ""
            console.print(f"  [cyan]{name}[/cyan] -- {skill.description}{src}")
        return True, None

    if cmd == "/commands":
        names = slash_commands.names()
        if not names:
            console.print("[dim]No user commands. Drop *.md files into "
                          ".codelet/commands/ (project) or "
                          "~/.codelet/commands/ (user).[/dim]")
            return True, None
        console.print("\n[bold]User-defined slash commands:[/bold]")
        for name in names:
            cmd_obj = slash_commands.get(name)
            assert cmd_obj is not None
            desc = cmd_obj.description or "(no description)"
            console.print(f"  [cyan]/{name}[/cyan] -- {desc}")
        return True, None

    if cmd == "/todos":
        rendered = agent.todo_store.render()
        console.print()
        console.print(rendered)
        return True, None

    if cmd == "/usage":
        console.print()
        console.print(agent.telemetry.render_panel())
        return True, None

    if cmd == "/profile":
        c = agent.config
        console.print()
        console.print(f"  [cyan]profile[/cyan]   {c.profile_name or '(none)'}")
        console.print(f"  [cyan]provider[/cyan]  {c.provider.value}")
        console.print(f"  [cyan]model[/cyan]     {c.model}")
        console.print(f"  [cyan]base_url[/cyan]  {c.base_url or '(default)'}")
        console.print(f"  [cyan]api_key[/cyan]   {'set' if c.api_key else '[red]MISSING[/red]'}")
        return True, None

    if cmd == "/sessions":
        _print_sessions(console)
        return True, None

    if cmd == "/resume":
        target = rest.strip()
        if not target:
            console.print("[red]Usage:[/red] /resume <session-id>")
            return True, None
        try:
            snapshot = load_session(target)
        except FileNotFoundError:
            console.print(f"[red]No session matched id:[/red] {target}")
            return True, None
        except Exception as exc:
            console.print(f"[red]Failed to load session:[/red] {exc}")
            return True, None
        restore_into(agent, snapshot)
        console.print(f"[green]Resumed session {target}[/green] "
                      f"({len(agent.context.messages)} messages restored)")
        return True, None

    if cmd == "/save":
        if session_store is None:
            console.print("[yellow]Sessions are disabled (--no-persist). Restart without that flag to save.[/yellow]")
            return True, None
        try:
            path = session_store.record(agent)
            console.print(f"[green]Saved session to[/green] {path}")
        except Exception as exc:
            console.print(f"[red]Save failed:[/red] {exc}")
        return True, None

    if cmd == "/mode":
        parts2 = line.split()
        if len(parts2) > 1 and parts2[1] in ("ask", "auto", "plan"):
            agent.config.permission_mode = PermissionMode(parts2[1])
            console.print(f"Mode -> [yellow]{parts2[1]}[/yellow]")
        else:
            console.print(f"Current mode: [yellow]{agent.config.permission_mode.value}[/yellow]")
            console.print("Usage: /mode [ask|auto|plan]")
        return True, None

    if cmd == "/help":
        console.print(Panel(BANNER, expand=False, border_style="cyan"))
        return True, None

    # Plugin-registered commands (run in-process; show their status string)
    plugin_result = agent.run_command(cmd, rest)
    if plugin_result is not None:
        console.print(plugin_result)
        return True, None

    # User-defined slash command lookup
    user_cmd_name = cmd.lstrip("/")
    user_cmd = slash_commands.get(user_cmd_name)
    if user_cmd is not None:
        return True, expand_command(user_cmd, rest)

    console.print(f"[red]Unknown command:[/red] {cmd}. Try /help or /commands.")
    return True, None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    console = Console()

    # Load .env files first so api_key_env lookups in resolve_profile see them.
    load_env_files()
    settings = load_settings()
    config = _build_config(args, settings)

    try:
        registry = ToolRegistry.default()
        agent = AgentLoop(config=config, registry=registry, console=console)
    except NotImplementedError as exc:
        console.print(f"[red]Provider not implemented yet:[/red] {exc}")
        return 2
    except Exception as exc:
        console.print(f"[red]Failed to start agent:[/red] {exc}")
        return 1

    # Slash command index (project + user). Loaded once at startup; new files
    # require a restart -- intentional, prevents surprising mid-session changes.
    slash_commands = load_commands()

    # Session persistence: opt-out via --no-persist. If --resume <id> is given,
    # restore into the freshly built agent before the REPL starts.
    persist = not args.no_persist
    project_dir = Path.cwd() / ".codelet"
    session_store = SessionStore(project_dir=project_dir) if persist else None

    if args.resume:
        try:
            snapshot = load_session(args.resume)
            restore_into(agent, snapshot)
            console.print(f"[green]Resumed session {args.resume}[/green] "
                          f"({len(agent.context.messages)} messages)")
        except FileNotFoundError:
            console.print(f"[red]No session matched id:[/red] {args.resume}")
            return 1
        except Exception as exc:
            console.print(f"[red]Failed to load session:[/red] {exc}")
            return 1

    show_telemetry = not args.no_telemetry

    if args.prompt:
        try:
            agent.run(args.prompt)
            console.print()
            if show_telemetry:
                console.print(agent.telemetry.render_panel())
            if persist and session_store is not None:
                try:
                    session_store.record(agent)
                except Exception as exc:
                    console.print(f"[dim yellow][session save skipped: {exc}][/dim yellow]")
        except PromptBlocked as exc:
            console.print(f"[yellow]Prompt blocked by hook:[/yellow] {exc}")
            return 0
        except Exception as exc:
            console.print(f"[red]Error:[/red] {exc}", file=sys.stderr)
            return 1
        return 0

    run_interactive(
        agent, console,
        show_telemetry=show_telemetry,
        persist=persist,
        slash_commands=slash_commands,
        session_store=session_store,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
