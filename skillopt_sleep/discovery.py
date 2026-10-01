"""Discover instruction documents locally without modifying files or using providers."""
from __future__ import annotations

import os
import re
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

from .skill_resolver import skill_search_roots

_GUIDES = {"AGENTS.md", "CLAUDE.md", "CLAUDE.local.md", "Codex.local.md"}
_EXCLUDE = {"node_modules", "__pycache__", "venv", "dist", "build", "target"}
_OWNED = re.compile(r"<!--\s*(?:#\s*)?[\w.-]+-owned\s*:", re.I)
_PACKAGE = re.compile(
    r"(?im)^\s*(?:package[ _-]root)\s*:\s*(?:\"([^\"]+)\"|'([^']+)'|`([^`]+)`|([^\s]+))"
)
# ponytail: bounded Markdown scanning, use a parser if instruction syntax grows beyond local links/imports.
_REFERENCES = re.compile(
    r"\[[^\]\n]*\]\(\s*(?:<([^>\n]+)>|([^\s)]+))(?:\s+[^)]*)?\)"
    r"|`([^`\n]+\.md(?:#[^`\n]*)?)`"
    r"|(?<![\w@])@([^\s`<>\"']+\.md(?:#[^\s`<>\"']*)?)"
)
_MAX_DOCUMENTS = 2000
_MAX_DEPTH = 12
_MAX_BYTES = 256_000


def _path(value: str, base: Path | None = None) -> Path:
    path = Path(os.path.expanduser(value))
    if not path.is_absolute() and base is not None:
        path = base / path
    return Path(os.path.abspath(path))


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _linked(path: Path) -> bool:
    return any(part.is_symlink() for part in (path, *path.parents))


def _walk(root: Path, skills: bool = False):
    """Walk real directories, pruning dependencies, tool state, and hidden caches."""
    if not root.is_dir():
        return
    for count, (directory, subdirs, names) in enumerate(os.walk(root, followlinks=False)):
        if count >= 10_000:
            break
        subdirs[:] = sorted(name for name in subdirs if name not in _EXCLUDE
                            and (not name.startswith(".") or skills and name == ".system")
                            and not (Path(directory) / name).is_symlink())
        for name in sorted(names):
            yield Path(directory) / name


def _read(path: Path) -> str:
    try:
        with path.open("rb") as stream:
            return stream.read(_MAX_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _reason(path: Path, text: str = "") -> str:
    if _linked(path):
        return "symlink paths are read-only"
    try:
        if path.stat().st_nlink > 1:
            return "hardlinked files are read-only"
    except OSError:
        return "file is unavailable or unreadable"
    if _OWNED.search(text):
        return "generated ownership marker; edit the explicit source package instead"
    if any(part.lower() in {"cache", ".cache", "runtime"} or part.lower().endswith("-runtime")
           for part in path.parts):
        return "installed cache or runtime; edit the explicit source package instead"
    if not os.access(path, os.W_OK):
        return "file is not writable"
    return ""


def _references(text: str, parent: Path):
    for match in _REFERENCES.finditer(text):
        value = next(group for group in match.groups() if group is not None)
        try:
            parsed = urlsplit(value)
        except ValueError:
            continue
        if parsed.scheme or parsed.netloc:
            continue
        value = unquote(parsed.path)
        if not value.lower().endswith(".md") or "\x00" in value:
            continue
        yield _path(value, parent)


def _declared_packages(text: str, parent: Path):
    for match in _PACKAGE.finditer(text):
        value = next(group for group in match.groups() if group is not None).rstrip(".")
        if "\x00" in value or re.match(r"[\w+.-]+:", value):
            continue
        root = _path(value, parent)
        if root.is_dir() and not _linked(root):
            yield root


def _native_prompt(path: Path) -> bool:
    if path.suffix.lower() != ".md" or not {"agents", "commands"}.intersection(path.parts[:-1]):
        return False
    text = _read(path)
    frontmatter = re.match(r"\A---\s*\n(.*?)^---\s*$", text, re.M | re.S)
    return bool(frontmatter and re.search(r"^(?:name|description|tools)\s*:",
                                         frontmatter.group(1), re.M))


def discover_documents(cfg: object) -> list[dict]:
    """Return deterministic guidance/skill/prompt records; discovery itself is read-only.

    ``discovered_from`` names the project/root for initial records and the referring
    file for links. Unknown external references are reported without reading them;
    an explicit package-root declaration allows bounded read-only traversal.
    """
    project_value = str(getattr(cfg, "invoked_project", "") or "").strip()
    project = _path(project_value) if project_value else None
    roots = [Path(root) for root in skill_search_roots(cfg)]
    if project is not None:
        roots.append(project / ".codex" / "skills")
    codex_home = str(getattr(cfg, "codex_home", "") or "").strip()
    if codex_home:
        # The installed plugin layout is shared; reuse its existing version selection.
        roots.extend(Path(root) for root in skill_search_roots(
            SimpleNamespace(claude_home=codex_home)))
    roots.append(Path.home() / ".agents" / "skills")
    roots = sorted({root for root in roots if root.is_dir()}, key=str)
    original_roots = [_path(value, project) for value in getattr(cfg, "skill_roots", ())
                      if isinstance(value, str) and value.strip()]
    original_roots.extend(_path(value) for value in
                          (codex_home, str(getattr(cfg, "claude_home", "") or "")) if value.strip())
    linked_roots = [root.resolve() for root in original_roots if _linked(root)]
    records: dict[str, dict] = {}
    queue = deque()

    def add(path, kind, origin, scope, inherited="", depth=0, follow=True):
        key = str(path)
        if key in records or len(records) >= _MAX_DOCUMENTS or not path.is_file():
            return
        reason = _reason(path)
        text = "" if reason.startswith("symlink") or not follow else _read(path)
        reason = reason or _reason(path, text) or inherited
        records[key] = {"path": key, "kind": kind, "discovered_from": str(origin),
                        "writable": not bool(reason), "reason": reason}
        if kind != "guidance" and follow and not reason.startswith("symlink") and depth < _MAX_DEPTH:
            queue.append((path, text, scope, reason, depth))

    if project is not None:
        for path in _walk(project):
            if path.name in _GUIDES:
                add(path, "guidance", project, project)
            elif path.name == "SKILL.md":
                add(path, "skill", project, project)
            elif _native_prompt(path):
                add(path, "prompt", project, project)
        for agent in (".agents", ".claude", ".codex", ".cursor", ".devin"):
            for name in sorted(_GUIDES):
                add(project / agent / name, "guidance", project, project)
            for directory in ("agents", "commands"):
                for path in _walk(project / agent / directory):
                    if _native_prompt(path):
                        add(path, "prompt", project, project)
    for root in roots:
        for path in _walk(root, skills=True):
            if path.name == "SKILL.md":
                inherited = "symlinked configured root is read-only" if any(
                    _inside(root, linked) for linked in linked_roots) else ""
                add(path, "skill", root, root, inherited)

    while queue and len(records) < _MAX_DOCUMENTS:
        path, text, scope, inherited, depth = queue.popleft()
        declared = list(_declared_packages(text, path.parent))
        for reference in _references(text, path.parent):
            real = reference.resolve()
            local = _inside(real, scope.resolve()) or project is not None and _inside(real, project.resolve())
            package = next((root for root in declared if _inside(real, root.resolve())), None)
            if local:
                add(reference, "skill" if reference.name == "SKILL.md" else "prompt",
                    path, scope, inherited, depth + 1)
            elif package is not None:
                add(reference, "prompt", path, package,
                    "declared external package is read-only; supply its source as an explicit skill root",
                    depth + 1)
            else:
                add(reference, "prompt", path, scope, "reference is outside the project or skill package",
                    depth + 1, follow=False)
    return [records[key] for key in sorted(records)]
