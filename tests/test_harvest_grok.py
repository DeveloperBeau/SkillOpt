"""Grok session harvest keeps message text and drops tool payloads."""
from __future__ import annotations

import json
import os
from urllib.parse import quote

from skillopt_sleep.config import load_config
from skillopt_sleep.harvest_grok import GROK_REPLAY_SENTINEL, harvest_grok
from skillopt_sleep.harvest_sources import harvest_for_config


def _record(kind: str, **update) -> dict:
    body = {"sessionUpdate": kind}
    body.update(update)
    return {"method": "session/update", "params": {"update": body}}


def _write_session(root: str, cwd: str, session_id: str, records: list, *, updated_at: str) -> None:
    encoded = quote(cwd, safe="")
    session_dir = os.path.join(root, encoded, session_id)
    os.makedirs(session_dir)
    summary = {
        "info": {"id": session_id, "cwd": cwd},
        "created_at": "2026-11-01T00:00:00Z",
        "updated_at": updated_at,
        "head_branch": "main",
    }
    with open(os.path.join(session_dir, "summary.json"), "w", encoding="utf-8") as handle:
        json.dump(summary, handle)
    with open(os.path.join(session_dir, "updates.jsonl"), "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    with open(os.path.join(session_dir, "system_prompt.txt"), "w", encoding="utf-8") as handle:
        handle.write("SECRET SYSTEM PROMPT do not harvest")


def _user(text: str) -> dict:
    return _record("user_message_chunk", content={"type": "text", "text": text})


def _assistant(text: str) -> dict:
    return _record("agent_message_chunk", content={"type": "text", "text": text})


def _thought(text: str) -> dict:
    return _record("agent_thought_chunk", content={"type": "text", "text": text})


def _tool(name: str, secret: str, path: str = "") -> dict:
    update = {
        "title": "do the thing",
        "rawInput": {"secret": secret},
        "_meta": {"x.ai/tool": {"name": name}},
    }
    if path:
        update["locations"] = [{"path": path}]
    return _record("tool_call", **update)


def test_harvest_keeps_messages_and_drops_payloads(tmp_path) -> None:
    root = str(tmp_path / "sessions")
    _write_session(
        root,
        "/repo/Yoshi",
        "sess-yoshi",
        [
            _user("fix the login"),
            _thought("private reasoning about the token"),
            _tool("read_file", "raw-tool-argument", "/repo/Yoshi/src/main.py"),
            _assistant("fixed the login"),
        ],
        updated_at="2026-11-02T00:00:00Z",
    )
    _write_session(
        root,
        "/repo/Other",
        "sess-other",
        [_user("fix the other project"), _assistant("done")],
        updated_at="2026-11-03T00:00:00Z",
    )
    _write_session(
        root,
        "/repo/Yoshi",
        "sess-replay",
        [
            _user(GROK_REPLAY_SENTINEL + "\n\nreplay this task"),
            _assistant("replay answer"),
        ],
        updated_at="2026-11-04T00:00:00Z",
    )

    digests = harvest_grok(root, scope="invoked", invoked_project="/repo/Yoshi")
    assert [item.session_id for item in digests] == ["sess-yoshi"]
    digest = digests[0]
    assert digest.user_prompts == ["fix the login"]
    assert digest.assistant_finals == ["fixed the login"]
    assert digest.tools_used == ["read_file"]
    assert digest.files_touched == ["/repo/Yoshi/src/main.py"]
    assert digest.git_branch == "main"
    blob = json.dumps(digest.to_dict())
    assert "SECRET SYSTEM PROMPT" not in blob
    assert "private reasoning" not in blob
    assert "raw-tool-argument" not in blob


def test_since_and_limit(tmp_path) -> None:
    root = str(tmp_path / "sessions")
    _write_session(
        root, "/repo/A", "old",
        [_user("old request"), _assistant("old answer")],
        updated_at="2026-10-01T00:00:00Z",
    )
    _write_session(
        root, "/repo/A", "new",
        [_user("new request"), _assistant("new answer")],
        updated_at="2026-11-08T00:00:00Z",
    )
    recent = harvest_grok(root, scope="all", since_iso="2026-11-01T00:00:00Z")
    assert [item.session_id for item in recent] == ["new"]
    limited = harvest_grok(root, scope="all", limit=1)
    assert len(limited) == 1


def test_grok_source_dispatches(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("skillopt_sleep.config._user_config_path", lambda: None)
    seen = {}

    def _fake(sessions_dir, **kwargs):
        seen["dir"] = sessions_dir
        seen.update(kwargs)
        return []

    monkeypatch.setattr("skillopt_sleep.harvest_sources.harvest_grok", _fake)
    home = tmp_path / "grok-home"
    cfg = load_config(
        transcript_source="Grok",
        grok_home=str(home),
        projects="invoked",
        invoked_project="/repo/Yoshi",
    )
    assert harvest_for_config(cfg, since_iso="2026-11-01T00:00:00Z", limit=2) == []
    assert seen["dir"] == os.path.join(os.path.abspath(home), "sessions")
    assert seen["scope"] == "invoked"
    assert seen["invoked_project"] == "/repo/Yoshi"
    assert seen["limit"] == 2
