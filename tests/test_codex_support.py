"""Tests for Codex plugin packaging and host-specific hook behavior."""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path
from typing import Any

from claude_smart import cli, cs_cite, hook, runtime, state
from claude_smart.events import post_tool, pre_tool, stop


REPO_ROOT = Path(__file__).resolve().parents[1]


def _read_json(path: str) -> dict[str, Any]:
    return json.loads((REPO_ROOT / path).read_text())


def _command_hooks(path: str) -> list[str]:
    parsed = _read_json(path)
    commands: list[str] = []
    for entries in (parsed.get("hooks") or {}).values():
        for entry in entries:
            for hook_def in entry.get("hooks") or []:
                if hook_def.get("type") == "command":
                    commands.append(str(hook_def.get("command") or ""))
    return commands


def test_codex_manifest_points_at_codex_hooks() -> None:
    manifest = _read_json("plugin/.codex-plugin/plugin.json")
    assert manifest["name"] == "claude-smart"
    assert manifest["hooks"] == "./hooks/codex-hooks.json"
    assert manifest["interface"]["displayName"] == "claude-smart"


def test_codex_marketplace_points_at_plugin_root() -> None:
    marketplace = _read_json(".agents/plugins/marketplace.json")
    entry = marketplace["plugins"][0]
    assert entry["name"] == "claude-smart"
    assert entry["source"]["path"] == "./plugin"
    assert entry["policy"]["installation"] == "AVAILABLE"


def test_hook_commands_pass_explicit_host() -> None:
    claude_commands = [
        c for c in _command_hooks("plugin/hooks/hooks.json") if "hook_entry.sh" in c
    ]
    codex_commands = _command_hooks("plugin/hooks/codex-hooks.json")
    assert claude_commands
    assert codex_commands
    assert all('hook_entry.sh" claude-code ' in c for c in claude_commands)
    assert all(
        'hook_entry.sh" codex ' in c for c in codex_commands if "hook_entry.sh" in c
    )


def test_codex_hooks_use_expected_events_and_marketplace_fallback() -> None:
    hooks = _read_json("plugin/hooks/codex-hooks.json")["hooks"]
    assert set(hooks) == {"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"}
    assert "PreToolUse" not in hooks
    for command in _command_hooks("plugin/hooks/codex-hooks.json"):
        assert "_codex_env.sh" in command
        assert "printenv PATH" not in command
        assert command.find("$HOME/plugins/claude-smart") < command.find(
            ".codex/plugins/cache/reflexioai/claude-smart"
        )
        assert ".codex/plugins/cache/reflexioai/claude-smart" in command


def test_hook_main_accepts_legacy_and_host_argv(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_read() -> dict[str, Any]:
        return {"session_id": "s1"}

    def fake_handlers():
        return {"stop": lambda _payload: calls.append((runtime.host(), "stop"))}

    monkeypatch.setattr(hook, "_read_stdin_json", fake_read)
    monkeypatch.setattr(hook, "_load_handlers", fake_handlers)

    assert hook.main(["stop"]) == 0
    assert hook.main(["codex", "stop"]) == 0
    assert calls == [("claude-code", "stop"), ("codex", "stop")]


def test_codex_pre_tool_does_not_inject_context(monkeypatch) -> None:
    runtime.set_host(runtime.HOST_CODEX)
    called = False

    def fail_emit(**_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(pre_tool.context_inject, "emit_context", fail_emit)
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)

    pre_tool.handle(
        {
            "session_id": "s1",
            "tool_name": "Bash",
            "tool_input": {"command": "uv run pytest"},
        }
    )

    assert called is False
    assert json.loads(buf.getvalue()) == {"continue": True}


def test_codex_citation_instruction_uses_text_marker_not_tool_call() -> None:
    runtime.set_host(runtime.HOST_CODEX)

    instruction = cs_cite.CITATION_INSTRUCTION

    assert "Do not call `cs-cite`" in instruction
    assert "✨ 1 claude-smart learning applied [cs:s1-ab12]" in instruction


def test_codex_stop_prefers_last_assistant_message(session_dir, monkeypatch) -> None:
    runtime.set_host(runtime.HOST_CODEX)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        stop.publish, "publish_unpublished", lambda **kw: calls.append(kw)
    )
    monkeypatch.setattr(stop.ids, "resolve_project_id", lambda *_a, **_kw: "demo")

    stop.handle(
        {
            "session_id": "s1",
            "cwd": str(REPO_ROOT),
            "last_assistant_message": "final answer from codex",
            "transcript_path": "/does/not/exist.jsonl",
        }
    )

    records = state.read_all("s1")
    assert records[-1]["role"] == "Assistant"
    assert records[-1]["content"] == "final answer from codex"
    assert calls and calls[0]["project_id"] == "demo"


def test_codex_stop_resolves_text_citations_from_last_assistant_message(
    session_dir, monkeypatch
) -> None:
    runtime.set_host(runtime.HOST_CODEX)
    monkeypatch.setattr(stop.publish, "publish_unpublished", lambda **_kw: ("ok", 1))
    monkeypatch.setattr(stop.ids, "resolve_project_id", lambda *_a, **_kw: "demo")
    state.append_injected(
        "s1",
        [
            {
                "id": "s2-17",
                "real_id": "17",
                "kind": "playbook",
                "source_kind": "user_playbook",
                "title": "Check editable installs",
            }
        ],
    )

    stop.handle(
        {
            "session_id": "s1",
            "cwd": str(REPO_ROOT),
            "last_assistant_message": (
                "The answer stays visible.\n\n"
                "✨ 1 claude-smart learning applied [cs:s2-17]"
            ),
            "transcript_path": "/does/not/exist.jsonl",
        }
    )

    records = state.read_all("s1")
    assert records[-1]["cited_items"] == [
        {
            "id": "s2-17",
            "real_id": "17",
            "kind": "playbook",
            "source_kind": "user_playbook",
            "title": "Check editable installs",
        }
    ]


def test_codex_post_tool_records_apply_patch_payload(session_dir) -> None:
    runtime.set_host(runtime.HOST_CODEX)

    post_tool.handle(
        {
            "session_id": "s1",
            "tool_name": "apply_patch",
            "tool_input": {"patch": "*** Begin Patch\n*** End Patch"},
            "tool_response": {"stdout": "ok"},
        }
    )

    record = state.read_all("s1")[0]
    assert record["tool_name"] == "apply_patch"
    assert record["tool_input"]["patch"].startswith("*** Begin Patch")
    assert record["tool_output"] == "ok"


def test_codex_install_succeeds_when_hooks_and_marketplace_succeed(
    monkeypatch, tmp_path, capsys
) -> None:
    env_path = tmp_path / "reflexio" / ".env"
    config_path = tmp_path / ".codex" / "config.toml"
    monkeypatch.setattr(cli, "_REFLEXIO_ENV_PATH", env_path)
    monkeypatch.setattr(cli, "_CODEX_CONFIG_PATH", config_path)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/bin/{name}")

    calls: list[list[str]] = []

    def fake_run_codex(args: list[str]):
        calls.append(args)
        if args == ["features", "enable", "plugin_hooks"]:
            return type(
                "Result", (), {"returncode": 1, "stdout": "", "stderr": "unsupported"}
            )()
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(cli, "_run_codex", fake_run_codex)

    rc = cli.cmd_install(argparse.Namespace(host="codex", source="unused"))

    assert rc == 0
    assert "CLAUDE_SMART_USE_LOCAL_CLI=1" in env_path.read_text()
    assert "plugin_hooks = true" in config_path.read_text()
    assert ["plugin", "marketplace", "add", str(cli._REPO_ROOT)] in calls
    assert "run /plugins" in capsys.readouterr().out


def test_codex_install_fails_when_marketplace_registration_fails(
    monkeypatch, tmp_path, capsys
) -> None:
    env_path = tmp_path / "reflexio" / ".env"
    config_path = tmp_path / ".codex" / "config.toml"
    monkeypatch.setattr(cli, "_REFLEXIO_ENV_PATH", env_path)
    monkeypatch.setattr(cli, "_CODEX_CONFIG_PATH", config_path)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/bin/{name}")

    def fake_run_codex(args: list[str]):
        return type(
            "Result", (), {"returncode": 1, "stdout": "", "stderr": "unsupported"}
        )()

    monkeypatch.setattr(cli, "_run_codex", fake_run_codex)

    rc = cli.cmd_install(argparse.Namespace(host="codex", source="unused"))

    assert rc == 1
    assert "plugin_hooks = true" in config_path.read_text()
    assert "marketplace registration failed" in capsys.readouterr().out


def test_codex_install_fails_when_required_files_missing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(cli, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/bin/{name}")

    rc = cli.cmd_install(argparse.Namespace(host="codex", source="unused"))

    assert rc == 1


def test_codex_install_fails_when_codex_cli_missing(monkeypatch) -> None:
    monkeypatch.setattr(
        cli.shutil, "which", lambda name: None if name == "codex" else f"/bin/{name}"
    )

    rc = cli.cmd_install(argparse.Namespace(host="codex", source="unused"))

    assert rc == 1


def test_run_codex_times_out(monkeypatch) -> None:
    def fake_run(*_args, **_kwargs):
        raise cli.subprocess.TimeoutExpired(cmd=["codex", "features"], timeout=30)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    result = cli._run_codex(["features", "enable", "plugin_hooks"])

    assert result.returncode == 124
    assert "timed out" in result.stderr


def test_set_toml_feature_updates_existing_spacing_variants(tmp_path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        "[features] # user comment\n"
        "plugin_hooks=false\n"
        "other = true\n"
        "[model]\n"
        'name = "gpt"\n'
    )

    assert cli._set_toml_feature(config, "plugin_hooks", True) is True

    text = config.read_text()
    assert text.count("plugin_hooks") == 1
    assert "plugin_hooks = true" in text
    assert text.index("plugin_hooks = true") < text.index("[model]")


def test_set_toml_feature_inserts_before_next_section(tmp_path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[features]\nother = true\n\n[model]\nname = "gpt"\n')

    assert cli._set_toml_feature(config, "plugin_hooks", True) is True

    text = config.read_text()
    assert text.index("plugin_hooks = true") < text.index("[model]")


def test_register_codex_marketplace_replaces_stale_source(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run_codex(args: list[str]):
        calls.append(args)
        if args[:3] == ["plugin", "marketplace", "add"]:
            code = 1 if len(calls) == 1 else 0
            return type(
                "Result",
                (),
                {
                    "returncode": code,
                    "stdout": "",
                    "stderr": (
                        "marketplace 'reflexioai' is already added "
                        "from a different source"
                        if code
                        else ""
                    ),
                },
            )()
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(cli, "_run_codex", fake_run_codex)

    ok, message = cli._register_codex_marketplace(Path("/tmp/marketplace"))

    assert ok is True
    assert "replaced stale" in message
    assert calls == [
        ["plugin", "marketplace", "add", "/tmp/marketplace"],
        ["plugin", "marketplace", "remove", "reflexioai"],
        ["plugin", "marketplace", "add", "/tmp/marketplace"],
    ]
