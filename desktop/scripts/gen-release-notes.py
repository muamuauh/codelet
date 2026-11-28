"""Generate categorized English GitHub Release notes between the previous tag and current tag."""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_URL = "https://github.com/muamuauh/codelet"

_CONVENTIONAL_RE = re.compile(
    r"^(?P<type>[a-zA-Z]+)(?:\((?P<scope>[^)]+)\))?(?P<breaking>!)?:\s*(?P<desc>.+)$"
)
_RELEASE_BUMP_RE = re.compile(r"^chore\(release\):\s*bump version", re.IGNORECASE)


def _git(*args: str, cwd: Path | None = None) -> str:
    res = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return res.stdout.strip() if res.returncode == 0 else ""


def find_previous_tag(current_tag: str, cwd: Path | None = None) -> str:
    """Return the nearest reachable v* tag strictly before `current_tag`."""
    ref = f"{current_tag}^" if current_tag else "HEAD^"
    prev = _git("describe", "--tags", "--abbrev=0", "--match", "v*", ref, cwd=cwd)
    if prev and prev != current_tag:
        return prev
    tags = [
        t.strip()
        for t in _git("tag", "--sort=-v:refname", "--list", "v*", cwd=cwd).splitlines()
        if t.strip()
    ]
    for t in tags:
        if t != current_tag:
            return t
    return ""


def collect_commits(prev_tag: str, current_ref: str, cwd: Path | None = None) -> list[tuple[str, str]]:
    """Return [(short_sha, subject), ...] in chronological reverse order."""
    rev_range = f"{prev_tag}..{current_ref}" if prev_tag else current_ref
    raw = _git("log", "--no-merges", "--format=%h%x1f%s", rev_range, cwd=cwd)
    out: list[tuple[str, str]] = []
    for line in raw.splitlines():
        if "\x1f" not in line:
            continue
        sha, subject = line.split("\x1f", 1)
        subject = subject.strip()
        if not subject or _RELEASE_BUMP_RE.match(subject):
            continue
        out.append((sha.strip(), subject))
    return out


def format_entry(sha: str, subject: str) -> tuple[str, str]:
    """Classify one commit into (category_key, markdown_bullet)."""
    m = _CONVENTIONAL_RE.match(subject)
    if m:
        ctype = m.group("type").lower()
        scope = (m.group("scope") or "").strip()
        desc = m.group("desc").strip()
        if desc:
            desc = desc[0].upper() + desc[1:]
        prefix = f"**{scope}**: " if scope else ""
        bullet = f"- {prefix}{desc} (`{sha}`)"
        if ctype == "feat":
            return ("features", bullet)
        if ctype == "fix":
            return ("fixes", bullet)
        if ctype in ("perf", "refactor", "style", "ui", "enhance"):
            return ("improvements", bullet)
        if ctype in ("docs", "test"):
            return ("docs_tests", bullet)
        return ("maintenance", bullet)

    desc = subject[0].upper() + subject[1:] if subject else subject
    return ("improvements", f"- {desc} (`{sha}`)")


def build_release_notes(
    tag: str,
    prev_tag: str,
    commits: list[tuple[str, str]],
    repo_url: str = REPO_URL,
) -> str:
    """Render categorized English Markdown release notes."""
    buckets: dict[str, list[str]] = {
        "features": [],
        "fixes": [],
        "improvements": [],
        "docs_tests": [],
        "maintenance": [],
    }
    for sha, subject in commits:
        cat, bullet = format_entry(sha, subject)
        buckets[cat].append(bullet)

    lines: list[str] = [f"## What's Changed in {tag}", ""]

    sections = [
        ("features", "### ✨ New Features"),
        ("fixes", "### 🐛 Bug Fixes"),
        ("improvements", "### ⚡ Improvements"),
        ("docs_tests", "### 📝 Docs & Tests"),
        ("maintenance", "### 🔧 Maintenance"),
    ]
    has_changes = False
    for key, heading in sections:
        if buckets[key]:
            has_changes = True
            lines.append(heading)
            lines.extend(buckets[key])
            lines.append("")

    if not has_changes:
        lines.append("- Maintenance release and dependency updates.")
        lines.append("")

    lines.extend(
        [
            "### 📦 Downloads",
            "- **`codelet-Setup-*.exe`** *(Recommended)*: Standard Windows installer (per-user, no admin required, desktop/Start Menu shortcuts, Explorer right-click **\"Open with codelet\"**, fast cold start, auto-updates).",
            "- **`codelet-*-portable.exe`**: Single-file portable executable — download and run directly without installing.",
            "- **`codelet-*-win-x64-unpacked.zip`**: Unpacked directory build — extract and run `codelet.exe` for instant startup.",
            "",
        ]
    )

    if prev_tag and tag:
        lines.append(f"**Full Changelog**: {repo_url}/compare/{prev_tag}...{tag}")
        lines.append("")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate categorized English release notes.")
    parser.add_argument("--tag", required=True, help="Current release tag (e.g. v0.1.4)")
    parser.add_argument("--prev-tag", default="", help="Previous tag (auto-detected if omitted)")
    parser.add_argument("--output", default="", help="Output Markdown file path (stdout if omitted)")
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[2]
    prev_tag = args.prev_tag.strip() or find_previous_tag(args.tag, cwd=repo_root)
    ref = args.tag if _git("rev-parse", "--verify", args.tag, cwd=repo_root) else "HEAD"
    commits = collect_commits(prev_tag, ref, cwd=repo_root)
    body = build_release_notes(args.tag, prev_tag, commits)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(body, encoding="utf-8")
    else:
        sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
