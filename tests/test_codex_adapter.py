"""Codex process, pane, hook and install contracts."""

import json
import io
import runpy
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from sessioncoord import cli, liveness, procio, tmuxio
from sessioncoord.tmuxio import SendRefused
from sessioncoord.model import Presence

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("argv,want", [
    (["/opt/codex/bin/codex", "--dangerously-bypass-approvals-and-sandbox"], True),
    (["node", "/opt/lib/node_modules/@openai/codex/bin/codex.js"], True),
    (["guarded", "codex"], False),
    (["node", "/opt/other/codex.js"], False),
    (["claude"], False),
])
def test_codex_process_identity(argv, want):
    assert procio.is_codex(argv) is want


def test_codex_ancestor_walks_through_guarded_wrapper(monkeypatch):
    parents = {40: 30, 30: 20, 20: 10}
    argv = {30: ["sh", "hook"], 20: ["/opt/bin/codex"], 10: ["guarded", "codex"]}
    monkeypatch.setattr(procio, "parent_of", lambda pid: parents.get(pid))
    monkeypatch.setattr(procio, "argv_of", lambda pid: argv.get(pid))
    assert procio.codex_ancestor(40) == 20


@pytest.mark.parametrize("argv,want", [
    (["/opt/bin/codex", "exec", "one-shot"], True),
    (["node", "/opt/node_modules/@openai/codex/bin/codex.js", "exec", "one-shot"], True),
    (["/opt/bin/codex", "--dangerously-bypass-approvals-and-sandbox", "exec", "one-shot"], True),
    (["node", "/opt/node_modules/@openai/codex/bin/codex.js", "--dangerously-bypass-approvals-and-sandbox", "exec", "one-shot"], True),
    (["/opt/bin/codex", "--dangerously-bypass-approvals-and-sandbox", "prompt"], False),
])
def test_headless_codex_is_not_a_session(argv, want):
    assert procio.is_codex_headless(argv) is want


def test_codex_pane_discovers_native_child_after_node_wrapper(monkeypatch):
    kids = {10: [20], 20: [30]}
    monkeypatch.setattr(tmuxio, "_children", lambda: kids)
    monkeypatch.setattr(tmuxio, "_is_claude", lambda pid: False)
    monkeypatch.setattr(tmuxio, "_argv_of", lambda pid: {
        20: ["node", "/opt/lib/node_modules/@openai/codex/bin/codex.js"],
        30: ["/opt/lib/vendor/bin/codex"],
    }.get(pid))
    def run(argv):
        if argv[1] == "list-panes":
            return "%3\tprobe:1\t10\t/repo\tcodex-coord"
        if argv[1] == "capture-pane":
            return (ROOT / "tests" / "data" / "codex-idle.txt").read_text()
        return "ok"
    table = tmuxio.panes(run)
    assert table["%3"]["engine"] == "codex"
    assert table["%3"]["claude_pid"] == 30
    assert tmuxio.prompt_is_busy("%3", run, engine="codex") is False


def test_claude_tool_child_does_not_relabel_codex_pane(monkeypatch):
    monkeypatch.setattr(tmuxio, "_is_claude", lambda pid: pid == 30)
    monkeypatch.setattr(tmuxio, "_argv_of", lambda pid: {
        20: ["/opt/bin/codex"], 30: ["claude", "-p", "one-shot"]}.get(pid))
    assert tmuxio.claude_under(10, {10: [20], 20: [30]}) is None


def test_codex_busy_fixture_overrides_empty_input():
    screen = (ROOT / "tests" / "data" / "codex-busy.txt").read_text()
    assert tmuxio.prompt_is_busy("%3", lambda argv: screen, engine="codex") is True


def test_codex_busy_line_beyond_eight_rows_is_not_idle():
    screen = "• Working (12s • esc to interrupt)\n" + ("detail\n" * 20) + "›  \x1b[2mAsk Codex to do anything\x1b[0m\n"
    assert tmuxio.prompt_is_busy("%3", lambda argv: screen, engine="codex") is True


def test_codex_with_no_visible_activity_state_is_unknown():
    screen = ("detail\n" * 20) + "›  \x1b[2mAsk Codex to do anything\x1b[0m\n"
    assert tmuxio.prompt_is_busy("%3", lambda argv: screen, engine="codex") is None


def test_exited_grandchild_does_not_hide_all_panes(monkeypatch):
    monkeypatch.setattr(tmuxio, "_argv_of", lambda pid: {
        20: ["node", "/opt/node_modules/@openai/codex/bin/codex.js"],
        30: ["/opt/bin/codex"], 40: None}.get(pid))
    monkeypatch.setattr(tmuxio, "_pid_exited", lambda pid: pid == 40)
    assert tmuxio.codex_under(10, {10: [20], 20: [40, 30]}) == 30


def test_unreadable_process_is_unknown_for_its_pane_only(monkeypatch):
    monkeypatch.setattr(tmuxio, "_children", lambda: {10: [40], 11: [50]})
    monkeypatch.setattr(tmuxio, "_is_claude", lambda pid: False)
    monkeypatch.setattr(tmuxio, "_argv_of", lambda pid: {
        40: None, 50: ["/opt/bin/codex"]}.get(pid))
    monkeypatch.setattr(tmuxio, "_pid_exited", lambda pid: False)
    def run(argv):
        if argv[1] == "list-panes":
            return "%3\tprobe:1\t10\t/repo\tpeer\n%4\tprobe:2\t11\t/repo\tother"
        return "ok"
    table = tmuxio.panes(run)
    assert table["%3"]["claude_pid"] is tmuxio.UNKNOWN_PID
    assert table["%4"]["claude_pid"] == 50
    peer = Presence(session_id="peer", repo_root="/repo", workdir="/repo",
                    pane="%3", claude_pid=40, title="peer", window="probe:1")
    assert liveness.is_live(peer, tmuxio.claude_pids(run))
    assert cli._live([peer], table)[0].pane == "%3"
    monkeypatch.setattr(tmuxio, "panes", lambda run=None: table)
    with pytest.raises(SendRefused, match="process could not be read"):
        tmuxio.send("%3", "session-coord inbox", run)


def test_codex_unknown_prompt_refuses_doorbell(monkeypatch):
    monkeypatch.setattr(tmuxio, "panes", lambda run=None: {
        "%3": {"pane": "%3", "window": "probe:1", "path": "/repo",
               "title": "codex-coord", "claude_pid": 30, "engine": "codex"}
    })
    calls = []
    def run(argv):
        calls.append(argv)
        return None if argv[1] == "capture-pane" else "ok"
    with pytest.raises(SendRefused, match="prompt"):
        tmuxio.send("%3", "session-coord inbox", run)
    assert not any(argv[1] == "send-keys" for argv in calls)


def test_idle_codex_receives_literal_doorbell(monkeypatch):
    monkeypatch.setattr(tmuxio, "panes", lambda run=None: {
        "%3": {"pane": "%3", "window": "probe:1", "path": "/repo",
               "title": "codex-coord", "claude_pid": 30, "engine": "codex"}
    })
    screen = (ROOT / "tests" / "data" / "codex-idle.txt").read_text()
    calls = []
    def run(argv):
        calls.append(argv)
        return screen if argv[1] == "capture-pane" else "ok"
    assert tmuxio.send("%3", "session-coord inbox", run, expect_pid=30) == "%3"
    sends = [argv for argv in calls if argv[1] == "send-keys"]
    assert sends[0][:6] == ["tmux", "send-keys", "-t", "%3", "-l", "--"]
    assert sends[1][-1] == "Enter"


def test_working_codex_keeps_doorbell_untyped(monkeypatch):
    monkeypatch.setattr(tmuxio, "panes", lambda run=None: {
        "%3": {"pane": "%3", "window": "probe:1", "path": "/repo",
               "title": "codex-coord", "claude_pid": 30, "engine": "codex"}
    })
    screen = (ROOT / "tests" / "data" / "codex-busy.txt").read_text()
    calls = []
    def run(argv):
        calls.append(argv)
        return screen if argv[1] == "capture-pane" else "ok"
    with pytest.raises(SendRefused, match="is working"):
        tmuxio.send("%3", "session-coord inbox", run)
    assert not any(argv[1] == "send-keys" for argv in calls)


def test_session_start_hook_registers_codex_process(monkeypatch, tmp_path):
    module = runpy.run_path(str(ROOT / "hooks" / "session-coord-codex-register"),
                            run_name="codex_test")
    monkeypatch.setattr(procio, "codex_ancestor", lambda: 1234)
    monkeypatch.setenv("TMUX_PANE", "%3")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "session_id": "thread-1", "cwd": str(tmp_path), "source": "startup",
        "transcript_path": str(tmp_path / "rollout.jsonl")})))
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: (
        calls.append((argv, kwargs)) or SimpleNamespace(returncode=0, stdout="")))
    assert module["main"]() == 0
    argv, opts = calls[0]
    assert argv[1:5] == ["register", "--engine", "codex", "--session"]
    assert "--pid" in argv and argv[argv.index("--pid") + 1] == "1234"
    assert "--pane" in argv and argv[argv.index("--pane") + 1] == "%3"
    assert opts["cwd"] == str(tmp_path)
    parsed = cli.build_parser().parse_args(argv[1:])
    assert parsed.engine == "codex" and parsed.session == "thread-1"
    assert parsed.transcript == str(tmp_path / "rollout.jsonl")


def test_session_start_hook_registers_a_forked_thread(monkeypatch, tmp_path):
    module = runpy.run_path(str(ROOT / "hooks" / "session-coord-codex-register"),
                            run_name="codex_test")
    monkeypatch.setattr(procio, "codex_ancestor", lambda: 1234)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "session_id": "thread-1", "cwd": str(tmp_path), "source": "fork"})))
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: (
        calls.append(argv) or SimpleNamespace(returncode=0, stdout="")))
    assert module["main"]() == 0
    assert calls and "thread-1" in calls[0]


def test_codex_pretool_payload_reaches_shared_tree_gate(tmp_path):
    hooks = tmp_path / "hooks"
    binary = tmp_path / "bin"
    hooks.mkdir()
    binary.mkdir()
    for name in ("session-coord-gate", "session-coord-gate-check"):
        shutil.copy2(ROOT / "hooks" / name, hooks / name)
    fake = binary / "session-coord"
    fake.write_text("#!/usr/bin/env python3\nimport json\nprint(json.dumps([{'code': 'shared-tree', 'sessions': ['peer'], 'detail': 'same tree', 'remedy': 'use worktrees'}]))\n")
    fake.chmod(0o755)
    payload = json.dumps({
        "session_id": "thread-123", "cwd": str(tmp_path),
        "hook_event_name": "PreToolUse", "tool_name": "Bash",
        "tool_use_id": "tool-1", "tool_input": {"command": "git add -A"},
        "permission_mode": "bypassPermissions",
    })
    out = subprocess.run([str(hooks / "session-coord-gate")], input=payload,
                         text=True, capture_output=True, cwd=tmp_path, timeout=30)
    assert out.returncode == 0
    decision = json.loads(out.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"


def test_installer_is_idempotent_and_preserves_existing_config(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[features]\nhooks = true\n\n[[hooks.PreToolUse]]\nmatcher = "Bash"\n')
    skill = tmp_path / "skills"
    runtime = tmp_path / "session-coord"
    cmd = [sys.executable, str(ROOT / "codex" / "install.py"), "--config", str(config),
           "--skills", str(skill), "--runtime", str(runtime)]
    subprocess.run(cmd, check=True)
    once = config.read_text()
    subprocess.run(cmd, check=True)
    assert config.read_text() == once
    assert once.count("session-coord-codex-register") == 1
    assert "UserPromptSubmit" in once
    assert "SessionEnd" in once
    assert 'matcher = "Bash"' in once
    assert (skill / "session-coord" / "SKILL.md").exists()
    assert (runtime / "hooks" / "session-coord-codex-register").exists()
    assert (runtime / "hooks" / "session-coord-gate").exists()
    assert (runtime / "hooks" / "session-coord-mail").exists()
    parsed = tomllib.loads(once)
    assert parsed["hooks"]["UserPromptSubmit"][0]["hooks"][0]["type"] == "command"
    assert parsed["hooks"]["PreToolUse"][-1]["matcher"] == ""
