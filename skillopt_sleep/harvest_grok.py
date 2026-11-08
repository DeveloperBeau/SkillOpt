"""SkillOpt-Sleep Grok session harvesting.

Reads ``~/.grok/sessions/<encoded-cwd>/<session-id>/updates.jsonl`` and
``summary.json``. The digest keeps user text, assistant text, and tool
names. It does not copy system prompts, tool arguments, or tool output.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import unquote

from skillopt_sleep.harvest import (
    _detect_feedback,
    _is_agent_session,
    _is_meta_prompt,
    _iter_jsonl,
    _project_matches,
    _safe_mtime,
)
from skillopt_sleep.staging import _SECRET_PATTERNS
from skillopt_sleep.types import SessionDigest

GROK_REPLAY_SENTINEL = "[skillopt-sleep:grok-engine:v1]"

_MESSAGE_UPDATES = {
    "user_message_chunk": "user",
    "agent_message_chunk": "assistant",
}


def _is_grok_replay(digest: SessionDigest) -> bool:
    """Detect a session written by SkillOpt's Grok replay backend."""
    if not digest.user_prompts:
        return False
    return digest.user_prompts[0].startswith(GROK_REPLAY_SENTINEL + "\n\n")


def _update(rec: Dict[str, Any]) -> Dict[str, Any]:
    params = rec.get("params")
    if not isinstance(params, dict):
        return {}
    update = params.get("update")
    return update if isinstance(update, dict) else {}


def _sanitize_text(text: str) -> str:
    sanitized = (text or "").replace("\x00", "").strip()
    if not sanitized or _is_meta_prompt(sanitized):
        return ""
    for pattern, replacement in _SECRET_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)
    return sanitized


def _sanitize_tool_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.:-]+", "_", name)[:80]


def _tool_name(update: Dict[str, Any]) -> str:
    meta = update.get("_meta")
    if not isinstance(meta, dict):
        return ""
    tool = meta.get("x.ai/tool")
    if not isinstance(tool, dict):
        return ""
    name = tool.get("name")
    if isinstance(name, str) and name.strip():
        return _sanitize_tool_name(name.strip())
    return ""


def _location_paths(update: Dict[str, Any]) -> List[str]:
    locations = update.get("locations")
    if not isinstance(locations, list):
        return []
    paths: List[str] = []
    for item in locations:
        path = ""
        if isinstance(item, str):
            path = item
        elif isinstance(item, dict) and isinstance(item.get("path"), str):
            path = item["path"]
        path = path.replace("\x00", "").strip()
        if path and len(path) <= 512:
            paths.append(path)
    return paths


def _chunk_text(update: Dict[str, Any]) -> str:
    content = update.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return content["text"]
    return ""


def _dedup(xs: Iterable[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for item in xs:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _load_summary(path: str) -> Dict[str, Any]:
    try:
        import json
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _session_project(session_dir: str, summary: Dict[str, Any]) -> str:
    info = summary.get("info")
    if isinstance(info, dict):
        cwd = info.get("cwd")
        if isinstance(cwd, str) and cwd.strip():
            return cwd.strip()
    group = os.path.dirname(session_dir)
    cwd_file = os.path.join(group, ".cwd")
    try:
        with open(cwd_file, encoding="utf-8") as handle:
            recorded = handle.read().strip()
        if recorded:
            return recorded
    except OSError:
        pass
    encoded = os.path.basename(group)
    if encoded:
        return unquote(encoded)
    return ""


def digest_grok_session(session_dir: str, project: str = "") -> Optional[SessionDigest]:
    """Build a ``SessionDigest`` from one Grok session directory."""
    updates_path = os.path.join(session_dir, "updates.jsonl")
    if not os.path.isfile(updates_path):
        return None
    summary = _load_summary(os.path.join(session_dir, "summary.json"))
    info = summary.get("info") if isinstance(summary.get("info"), dict) else {}
    session_id = info.get("id") if isinstance(info.get("id"), str) and info.get("id") else ""
    if not session_id:
        session_id = os.path.basename(session_dir)
    session_project = _session_project(session_dir, summary)
    branch = summary.get("head_branch")
    started = summary.get("created_at") if isinstance(summary.get("created_at"), str) else ""
    ended = summary.get("updated_at") if isinstance(summary.get("updated_at"), str) else ""
    if not ended and isinstance(summary.get("last_active_at"), str):
        ended = summary["last_active_at"]

    user_prompts: List[str] = []
    assistant_finals: List[str] = []
    tools: List[str] = []
    files: List[str] = []
    feedback: List[str] = []
    n_user = 0
    n_asst = 0

    for rec in _iter_jsonl(updates_path):
        if not isinstance(rec, dict):
            continue
        update = _update(rec)
        kind = update.get("sessionUpdate")
        if kind == "tool_call":
            tool = _tool_name(update)
            if tool:
                tools.append(tool)
            files.extend(_location_paths(update))
            continue
        if kind == "tool_call_update":
            files.extend(_location_paths(update))
            continue
        role = _MESSAGE_UPDATES.get(kind) if isinstance(kind, str) else None
        if role is None:
            continue
        sanitized = _sanitize_text(_chunk_text(update))
        if not sanitized:
            continue
        if role == "user":
            n_user += 1
            user_prompts.append(sanitized)
            feedback.extend(_detect_feedback(sanitized))
        else:
            n_asst += 1
            assistant_finals.append(sanitized)

    if project and not _project_matches(session_project or "", "invoked", project):
        return None
    if n_user == 0 and n_asst == 0:
        return None

    return SessionDigest(
        session_id=session_id,
        project=session_project,
        git_branch=branch if isinstance(branch, str) else "",
        started_at=started,
        ended_at=ended or started,
        user_prompts=user_prompts,
        assistant_finals=assistant_finals[-5:],
        tools_used=_dedup(tools),
        files_touched=_dedup(files),
        feedback_signals=feedback,
        n_user_turns=n_user,
        n_assistant_turns=n_asst,
        raw_path=updates_path,
    )


def _iter_session_dirs(sessions_dir: str) -> List[str]:
    found: List[str] = []
    if not os.path.isdir(sessions_dir):
        return found
    try:
        groups = os.listdir(sessions_dir)
    except OSError:
        return found
    for group in groups:
        group_path = os.path.join(sessions_dir, group)
        if not os.path.isdir(group_path):
            continue
        try:
            names = os.listdir(group_path)
        except OSError:
            continue
        for name in names:
            session_dir = os.path.join(group_path, name)
            if os.path.isfile(os.path.join(session_dir, "updates.jsonl")):
                found.append(session_dir)
    found.sort(key=lambda path: _safe_mtime(os.path.join(path, "updates.jsonl")), reverse=True)
    return found


def harvest_grok(
    sessions_dir: str,
    *,
    scope: Any = "all",
    invoked_project: str = "",
    since_iso: Optional[str] = None,
    limit: int = 0,
) -> List[SessionDigest]:
    """Walk a Grok sessions directory and return matching digests."""
    digests: List[SessionDigest] = []
    project_hint = invoked_project if scope == "invoked" else ""
    for session_dir in _iter_session_dirs(sessions_dir):
        digest = digest_grok_session(session_dir, project=project_hint)
        if digest is None:
            continue
        if not _project_matches(digest.project or "", scope, invoked_project):
            continue
        if _is_agent_session(digest) or _is_grok_replay(digest):
            continue
        if since_iso and digest.ended_at and digest.ended_at < since_iso:
            continue
        digests.append(digest)
        if limit and len(digests) >= limit:
            break
    return digests
