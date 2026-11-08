"""Headless Grok calls send the prompt in a file and read JSON text."""
from __future__ import annotations

import json
from unittest import mock

from skillopt_sleep.backend import GrokCliBackend, get_backend
from skillopt_sleep.harvest_grok import GROK_REPLAY_SENTINEL
from skillopt_sleep.types import TaskRecord


class _Proc:
    def __init__(self, stdout: str, stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_get_backend_grok_alias() -> None:
    backend = get_backend("grok", grok_path="/usr/bin/grok", project_dir="/repo")
    assert isinstance(backend, GrokCliBackend)
    assert backend.grok_path == "/usr/bin/grok"
    assert backend.project_dir == "/repo"
    assert isinstance(get_backend("grok-cli"), GrokCliBackend)


def test_call_once_uses_prompt_file_and_json_text(tmp_path) -> None:
    prompt = "line one\nline two"
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = list(cmd)
        seen["kwargs"] = kwargs
        path = cmd[cmd.index("--prompt-file") + 1]
        with open(path, encoding="utf-8") as handle:
            seen["prompt"] = handle.read()
        return _Proc(json.dumps({"text": "hello from grok", "sessionId": "abc"}))

    backend = GrokCliBackend(grok_path="grok", project_dir=str(tmp_path))
    with mock.patch("skillopt_sleep.backend.subprocess.run", side_effect=fake_run):
        out = backend._call_once(prompt)

    assert out == "hello from grok"
    cmd = seen["cmd"]
    assert cmd[0] == "grok"
    assert "--prompt-file" in cmd
    assert "--output-format" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"
    assert cmd[cmd.index("--cwd") + 1] == str(tmp_path)
    assert prompt not in cmd
    assert seen["prompt"].startswith(GROK_REPLAY_SENTINEL + "\n\n")
    assert prompt in seen["prompt"]
    assert "input" not in seen["kwargs"]


def test_auth_error_is_not_retried() -> None:
    calls = {"n": 0}

    def fake_run(cmd, **kwargs):
        calls["n"] += 1
        return _Proc("", stderr="Not logged in. Run grok login", returncode=1)

    backend = GrokCliBackend(grok_path="grok")
    with mock.patch("skillopt_sleep.backend.subprocess.run", side_effect=fake_run), \
         mock.patch("time.sleep"):
        out = backend._call("hello")

    assert out == ""
    assert calls["n"] == 1
    assert "grok login" in backend.last_call_error


def test_attempt_with_tools_reads_call_log(tmp_path) -> None:
    def fake_run(cmd, **kwargs):
        work = cmd[cmd.index("--cwd") + 1]
        with open(work + "/_tool_calls.log", "w", encoding="utf-8") as handle:
            handle.write("search\n")
        assert cmd[cmd.index("--sandbox") + 1] == "workspace"
        return _Proc(json.dumps({"text": "final answer"}))

    backend = GrokCliBackend(grok_path="grok")
    task = TaskRecord(id="t", project="/p", intent="find the note")
    with mock.patch("skillopt_sleep.backend.subprocess.run", side_effect=fake_run):
        text, called = backend.attempt_with_tools(task, "use ./search", "", ["search"])

    assert text == "final answer"
    assert called == ["search"]
