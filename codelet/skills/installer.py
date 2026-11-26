"""Install Skills from GitHub URLs (blob / tree / repo / raw) or direct `.md` URLs.

Handles the real-world shapes published on GitHub (Claude Code / Antigravity /
Agent Skills conventions):
  - Single file: `https://github.com/owner/repo/blob/main/skills/foo.md`
                 `https://github.com/owner/repo/blob/main/skills/foo/SKILL.md`
                 `https://raw.githubusercontent.com/...`
  - Directory bundle: `https://github.com/owner/repo/tree/main/skills/foo`
                      (downloads `SKILL.md` + companion `scripts/`, `references/`, etc.)
  - Repository root: `https://github.com/owner/repo`
                      (installs root `SKILL.md`, or a single `skills/<name>/SKILL.md`,
                       or lists available skills if the repo contains multiple)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
import yaml

USER_AGENT = "codelet/0.1 (+https://github.com/gjq00/my-codelet)"
MAX_FILES = 30
MAX_TOTAL_BYTES = 512 * 1024
MAX_SINGLE_FILE_BYTES = 128 * 1024

_SCRIPT_EXTS = {".py", ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd", ".js", ".ts", ".mjs", ".rb"}
_TEXT_EXTS = {
    ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".xml", ".csv",
    ".html", ".css", ".sql", ".graphql", ".env.example",
    *_SCRIPT_EXTS,
}
_RISK_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("pipes remote script directly into shell", re.compile(r"(curl|wget)\s+[^\n|]+\|\s*(ba)?sh", re.I)),
    ("executes dynamic shell/PowerShell expression", re.compile(r"\b(Invoke-Expression|iex\b|eval\s*\()", re.I)),
    ("contains destructive rm -rf command", re.compile(r"\brm\s+-[a-z]*r[a-z]*f\b", re.I)),
    ("references .env or credential files", re.compile(r"(^|[\s/'\"`])\.env(\b|$)", re.I)),
]


@dataclass(frozen=True)
class GitHubSource:
    kind: str  # "file" | "dir" | "repo" | "raw" | "url"
    url: str
    owner: str = ""
    repo: str = ""
    ref: str = ""
    path: str = ""


@dataclass
class FetchedSkill:
    suggested_name: str
    entry_file: str                 # relative path of main markdown (e.g. "SKILL.md" or "foo.md")
    files: dict[str, str]           # rel_path -> utf-8 text
    source_url: str
    warnings: list[str] = field(default_factory=list)


@dataclass
class InstallResult:
    name: str
    target_path: Path
    files_written: list[Path]
    warnings: list[str]
    scope: str
    plugin: str | None = None
    category: str = ""


def _slugify(raw: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", (raw or "").strip().lower()).strip("-._")
    return s or "custom-skill"


def parse_github_url(url: str) -> GitHubSource:
    """Classify a GitHub / raw / direct markdown URL."""
    clean = (url or "").strip()
    if not clean or not (clean.startswith("http://") or clean.startswith("https://")):
        raise ValueError("URL must start with http:// or https://")

    parsed = urlparse(clean)
    host = parsed.netloc.lower()
    parts = [unquote(p) for p in parsed.path.strip("/").split("/") if p]

    if host in ("github.com", "www.github.com") and len(parts) >= 2:
        owner = parts[0]
        repo = parts[1][:-4] if parts[1].endswith(".git") else parts[1]
        if len(parts) == 2:
            return GitHubSource(kind="repo", url=clean, owner=owner, repo=repo)
        mode = parts[2]
        if mode == "blob" and len(parts) >= 5:
            ref = parts[3]
            rel = "/".join(parts[4:])
            return GitHubSource(kind="file", url=clean, owner=owner, repo=repo, ref=ref, path=rel)
        if mode == "tree" and len(parts) >= 4:
            ref = parts[3]
            rel = "/".join(parts[4:])
            return GitHubSource(
                kind="dir" if rel else "repo",
                url=clean, owner=owner, repo=repo, ref=ref, path=rel,
            )

    if host == "raw.githubusercontent.com" and len(parts) >= 4:
        owner, repo, ref = parts[0], parts[1], parts[2]
        rel = "/".join(parts[3:])
        return GitHubSource(kind="raw", url=clean, owner=owner, repo=repo, ref=ref, path=rel)

    return GitHubSource(kind="url", url=clean, path=parts[-1] if parts else "")


def ensure_skill_frontmatter(
    raw_md: str,
    *,
    fallback_name: str,
    explicit_name: str = "",
    category: str = "",
    plugin: str = "",
) -> tuple[str, str]:
    """Ensure `raw_md` carries valid codelet YAML frontmatter (`name` + `description`).

    Returns `(normalized_markdown, resolved_skill_name)`.
    """
    text = (raw_md or "").lstrip("\ufeff")
    stripped = text.lstrip()
    fm: dict[str, Any] = {}
    body = text.strip()

    if stripped.startswith("---"):
        parts = stripped.split("---", 2)
        if len(parts) >= 3:
            try:
                loaded = yaml.safe_load(parts[1])
                if isinstance(loaded, dict):
                    fm = dict(loaded)
                    body = parts[2].strip()
            except yaml.YAMLError:
                pass

    name = (explicit_name or str(fm.get("name") or "") or fallback_name).strip()
    name = _slugify(name)
    fm["name"] = name

    desc = str(fm.get("description") or "").strip()
    if not desc:
        # Derive a short description from the first non-heading prose line or heading.
        for line in body.splitlines():
            ln = line.strip().lstrip("#").strip()
            if ln:
                desc = ln[:100]
                break
        if not desc:
            desc = f"Installed skill: {name}"
    fm["description"] = desc

    if category.strip():
        fm["category"] = category.strip()
    if plugin.strip():
        fm["plugin"] = plugin.strip()

    fm_yaml = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).strip()
    normalized = f"---\n{fm_yaml}\n---\n\n{body}\n"
    return normalized, name


def audit_skill_files(files: dict[str, str]) -> list[str]:
    """Scan downloaded skill files for executable scripts or risky command patterns."""
    warnings: list[str] = []
    scripts = sorted(p for p in files if Path(p).suffix.lower() in _SCRIPT_EXTS)
    if scripts:
        warnings.append(f"Contains executable script file(s): {', '.join(scripts[:6])}")

    seen: set[str] = set()
    for rel, content in files.items():
        for label, pat in _RISK_PATTERNS:
            if pat.search(content):
                key = f"{label} ({rel})"
                if key not in seen:
                    seen.add(key)
                    warnings.append(f"Security note: {key}")
    return warnings


def _github_raw_url(owner: str, repo: str, ref: str, path: str) -> str:
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path.lstrip('/')}"


def _is_allowed_companion(rel_path: str) -> bool:
    name = Path(rel_path).name
    if name.startswith("."):
        return False
    suf = Path(rel_path).suffix.lower()
    return suf in _TEXT_EXTS or name.upper() in ("SKILL.MD", "README.MD", "LICENSE")


def _fetch_github_dir(
    client: httpx.Client,
    owner: str,
    repo: str,
    ref: str,
    base_path: str,
) -> dict[str, str]:
    """Recursively download text files inside a GitHub directory via Contents API."""
    files: dict[str, str] = {}
    total_bytes = 0

    def walk(api_path: str, rel_prefix: str) -> None:
        nonlocal total_bytes
        if len(files) >= MAX_FILES or total_bytes >= MAX_TOTAL_BYTES:
            return
        url = f"https://api.github.com/repos/{owner}/{repo}/contents/{api_path.lstrip('/')}"
        params = {"ref": ref} if ref else {}
        resp = client.get(url, params=params, headers={"Accept": "application/vnd.github+json"})
        resp.raise_for_status()
        data = resp.json()
        entries = data if isinstance(data, list) else [data]
        for item in entries:
            if len(files) >= MAX_FILES or total_bytes >= MAX_TOTAL_BYTES:
                break
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            iname = str(item.get("name") or "")
            if not iname or iname.startswith("."):
                continue
            rel = f"{rel_prefix}/{iname}" if rel_prefix else iname
            if itype == "dir":
                if iname in ("node_modules", "__pycache__", ".git", "dist", "build"):
                    continue
                walk(str(item.get("path") or ""), rel)
            elif itype == "file":
                size = int(item.get("size") or 0)
                if size > MAX_SINGLE_FILE_BYTES or not _is_allowed_companion(rel):
                    continue
                dl_url = item.get("download_url")
                if not dl_url:
                    continue
                f_resp = client.get(str(dl_url))
                f_resp.raise_for_status()
                text = f_resp.text
                total_bytes += len(text.encode("utf-8", errors="ignore"))
                if total_bytes <= MAX_TOTAL_BYTES:
                    files[rel] = text

    walk(base_path, "")
    return files


def fetch_remote_skill(url: str, *, timeout: float = 20.0) -> FetchedSkill:
    """Download a Skill (single file or directory bundle) from GitHub or direct URL."""
    src = parse_github_url(url)
    headers = {"User-Agent": USER_AGENT}
    with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
        if src.kind == "file":
            raw_url = _github_raw_url(src.owner, src.repo, src.ref, src.path)
            resp = client.get(raw_url)
            resp.raise_for_status()
            fname = Path(src.path).name
            suggested = (
                Path(src.path).parent.name
                if fname.lower() == "skill.md" and Path(src.path).parent.name
                else Path(src.path).stem
            )
            files = {fname: resp.text}
            return FetchedSkill(
                suggested_name=_slugify(suggested),
                entry_file=fname,
                files=files,
                source_url=url,
                warnings=audit_skill_files(files),
            )

        if src.kind in ("raw", "url"):
            resp = client.get(src.url)
            resp.raise_for_status()
            ctype = resp.headers.get("content-type", "").lower()
            if "text/html" in ctype and "<html" in resp.text[:400].lower():
                raise ValueError("URL returned an HTML page rather than a raw Markdown skill file.")
            fname = Path(src.path).name or "SKILL.md"
            if not fname.lower().endswith(".md"):
                fname = "SKILL.md"
            suggested = (
                Path(src.path).parent.name
                if fname.lower() == "skill.md" and Path(src.path).parent.name
                else Path(fname).stem
            )
            files = {fname: resp.text}
            return FetchedSkill(
                suggested_name=_slugify(suggested),
                entry_file=fname,
                files=files,
                source_url=url,
                warnings=audit_skill_files(files),
            )

        if src.kind == "dir":
            try:
                files = _fetch_github_dir(client, src.owner, src.repo, src.ref, src.path)
            except httpx.HTTPStatusError:
                # Fallback when GitHub API rate-limits: fetch SKILL.md directly via raw URL.
                raw_url = _github_raw_url(src.owner, src.repo, src.ref, f"{src.path.rstrip('/')}/SKILL.md")
                resp = client.get(raw_url)
                resp.raise_for_status()
                files = {"SKILL.md": resp.text}

            entry = _pick_entry_markdown(files)
            if entry is None:
                raise ValueError(f"No SKILL.md or markdown skill file found under '{src.path}'.")
            suggested = Path(src.path).name or src.repo
            return FetchedSkill(
                suggested_name=_slugify(suggested),
                entry_file=entry,
                files=files,
                source_url=url,
                warnings=audit_skill_files(files),
            )

        # src.kind == "repo": inspect repo root for SKILL.md or skills/<name>/SKILL.md
        ref = src.ref
        if not ref:
            meta = client.get(
                f"https://api.github.com/repos/{src.owner}/{src.repo}",
                headers={"Accept": "application/vnd.github+json"},
            )
            if meta.status_code == 200:
                ref = str(meta.json().get("default_branch") or "main")
            else:
                ref = "main"

        # 1. Try root SKILL.md directly
        root_raw = client.get(_github_raw_url(src.owner, src.repo, ref, "SKILL.md"))
        if root_raw.status_code == 200 and root_raw.text.strip():
            files = {"SKILL.md": root_raw.text}
            return FetchedSkill(
                suggested_name=_slugify(src.repo),
                entry_file="SKILL.md",
                files=files,
                source_url=url,
                warnings=audit_skill_files(files),
            )

        # 2. Inspect `skills/` directory
        sk_api = client.get(
            f"https://api.github.com/repos/{src.owner}/{src.repo}/contents/skills",
            params={"ref": ref},
            headers={"Accept": "application/vnd.github+json"},
        )
        if sk_api.status_code == 200 and isinstance(sk_api.json(), list):
            items = [
                it for it in sk_api.json()
                if isinstance(it, dict) and (it.get("type") == "dir" or str(it.get("name", "")).endswith(".md"))
            ]
            if len(items) == 1:
                only = items[0]
                if only.get("type") == "dir":
                    sub_url = f"https://github.com/{src.owner}/{src.repo}/tree/{ref}/{only['path']}"
                    return fetch_remote_skill(sub_url, timeout=timeout)
                sub_url = f"https://github.com/{src.owner}/{src.repo}/blob/{ref}/{only['path']}"
                return fetch_remote_skill(sub_url, timeout=timeout)
            if len(items) > 1:
                listing = "\n".join(
                    f"  - {it['name']}: https://github.com/{src.owner}/{src.repo}/"
                    f"{'tree' if it.get('type') == 'dir' else 'blob'}/{ref}/{it['path']}"
                    for it in items[:20]
                )
                raise ValueError(
                    f"Repository contains {len(items)} skills under `skills/`. "
                    f"Specify one skill URL to install:\n{listing}"
                )

        raise ValueError(
            f"Could not find a SKILL.md at repo root or under `skills/` in {src.owner}/{src.repo}. "
            "Provide the direct GitHub `blob/` or `tree/` URL to the skill."
        )


def _pick_entry_markdown(files: dict[str, str]) -> str | None:
    for cand in ("SKILL.md", "skill.md"):
        if cand in files:
            return cand
    top_mds = sorted(k for k in files if "/" not in k and k.lower().endswith(".md"))
    if top_mds:
        return top_mds[0]
    any_mds = sorted(k for k in files if k.lower().endswith(".md"))
    return any_mds[0] if any_mds else None


def resolve_install_root(
    *,
    scope: str = "project",
    plugin: str = "",
    project_dir: Path | None = None,
    user_dir: Path | None = None,
) -> Path:
    proj = (project_dir or Path.cwd()).resolve()
    home_skills = (user_dir or (Path.home() / ".codelet" / "skills")).resolve()
    sc = (scope or "project").strip().lower()
    if sc == "plugin":
        pack = _slugify(plugin)
        if not plugin.strip():
            raise ValueError("scope='plugin' requires a non-empty plugin pack name")
        return proj / ".codelet" / "plugins" / pack / "skills"
    if sc == "global":
        return home_skills
    return proj / ".codelet" / "skills"


def install_fetched_skill(
    fetched: FetchedSkill,
    *,
    scope: str = "project",
    plugin: str = "",
    category: str = "",
    name: str = "",
    project_dir: Path | None = None,
    user_dir: Path | None = None,
    overwrite: bool = False,
) -> InstallResult:
    """Write a fetched skill cleanly into the target skill directory."""
    sc = (scope or "project").strip().lower()
    # When installing into a plugin pack directory, plugin ownership comes from
    # directory location; when installing standalone with `plugin=<existing>`,
    # attach it via frontmatter so it toggles with that plugin.
    attach_fm_plugin = plugin.strip() if (plugin.strip() and sc != "plugin") else ""

    entry_raw = fetched.files[fetched.entry_file]
    normalized_md, skill_name = ensure_skill_frontmatter(
        entry_raw,
        fallback_name=fetched.suggested_name,
        explicit_name=name,
        category=category,
        plugin=attach_fm_plugin,
    )

    root = resolve_install_root(
        scope=sc,
        plugin=plugin,
        project_dir=project_dir,
        user_dir=user_dir,
    )
    root.mkdir(parents=True, exist_ok=True)

    is_bundle = len(fetched.files) > 1 or fetched.entry_file.lower() == "skill.md"
    written: list[Path] = []

    if not is_bundle:
        dest = (root / f"{skill_name}.md").resolve()
        dest.relative_to(root.resolve())
        if dest.exists() and not overwrite:
            raise FileExistsError(f"Skill already exists at {dest} (pass overwrite=True to replace)")
        dest.write_text(normalized_md, encoding="utf-8")
        written.append(dest)
        target_path = dest
    else:
        bundle_dir = (root / skill_name).resolve()
        bundle_dir.relative_to(root.resolve())
        if bundle_dir.exists() and not overwrite:
            raise FileExistsError(f"Skill bundle already exists at {bundle_dir} (pass overwrite=True to replace)")
        bundle_dir.mkdir(parents=True, exist_ok=True)
        for rel, content in fetched.files.items():
            rel_norm = rel.replace("\\", "/").lstrip("/")
            if ".." in Path(rel_norm).parts:
                raise ValueError(f"Unsafe relative path in skill archive: {rel}")
            out_name = "SKILL.md" if rel == fetched.entry_file else rel_norm
            out_file = (bundle_dir / out_name).resolve()
            out_file.relative_to(bundle_dir)
            out_file.parent.mkdir(parents=True, exist_ok=True)
            out_file.write_text(normalized_md if rel == fetched.entry_file else content, encoding="utf-8")
            written.append(out_file)
        target_path = bundle_dir / "SKILL.md"

    return InstallResult(
        name=skill_name,
        target_path=target_path,
        files_written=written,
        warnings=list(fetched.warnings),
        scope=sc,
        plugin=plugin.strip() or None,
        category=category.strip(),
    )


def parse_install_args(args: str) -> dict[str, Any]:
    """Parse `/install <url> [--global] [--plugin <pack>] [--category <cat>] [--name <n>] [--overwrite]`."""
    import shlex

    tokens = shlex.split(args or "")
    out: dict[str, Any] = {
        "url": "",
        "scope": "project",
        "plugin": "",
        "category": "",
        "name": "",
        "overwrite": False,
    }
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in ("--global", "-g"):
            out["scope"] = "global"
        elif t in ("--overwrite", "-f", "--force"):
            out["overwrite"] = True
        elif t == "--plugin" and i + 1 < len(tokens):
            i += 1
            out["plugin"] = tokens[i]
            if out["scope"] == "project":
                out["scope"] = "plugin"
        elif t == "--category" and i + 1 < len(tokens):
            i += 1
            out["category"] = tokens[i]
        elif t == "--name" and i + 1 < len(tokens):
            i += 1
            out["name"] = tokens[i]
        elif not t.startswith("-") and not out["url"]:
            out["url"] = t
        i += 1
    return out

