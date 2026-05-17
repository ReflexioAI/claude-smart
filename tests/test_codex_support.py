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
    assert manifest["skills"] == "./skills/"
    assert manifest["interface"]["displayName"] == "claude-smart"


def test_codex_skill_documents_command_mapping() -> None:
    skill = (REPO_ROOT / "plugin" / "skills" / "claude-smart" / "SKILL.md").read_text()

    assert "name: claude-smart" in skill
    assert "Codex does not currently support plugin-provided slash commands" in skill
    assert "bash ~/.reflexio/plugin-root/scripts/cli.sh show" in skill
    assert "bash ~/.reflexio/plugin-root/scripts/cli.sh learn --note" in skill
    assert "bash ~/.reflexio/plugin-root/scripts/cli.sh restart" in skill
    assert "bash ~/.reflexio/plugin-root/scripts/cli.sh clear-all --yes" in skill


def test_codex_marketplace_points_at_plugin_root() -> None:
    marketplace = _read_json(".agents/plugins/marketplace.json")
    entry = marketplace["plugins"][0]
    assert marketplace["name"] == cli._CODEX_MARKETPLACE_NAME
    assert (
        marketplace["interface"]["displayName"] == cli._CODEX_MARKETPLACE_DISPLAY_NAME
    )
    assert entry["name"] == "claude-smart"
    assert entry["source"]["path"] == "./plugin"
    assert entry["policy"]["installation"] == "AVAILABLE"


def test_prepare_codex_local_marketplace_uses_named_plugin_path(
    monkeypatch, tmp_path
) -> None:
    plugin_root = tmp_path / "repo" / "plugin"
    plugin_root.mkdir(parents=True)
    marketplace_root = tmp_path / "marketplaces" / "reflexioai"
    stale_file = marketplace_root / "old-copy" / "stale.txt"
    stale_file.parent.mkdir(parents=True)
    stale_file.write_text("stale")
    monkeypatch.setattr(cli, "_PLUGIN_ROOT", plugin_root)
    monkeypatch.setattr(cli, "_CODEX_LOCAL_MARKETPLACE_ROOT", marketplace_root)

    result = cli._prepare_codex_local_marketplace()

    assert result == marketplace_root
    manifest = json.loads(
        (marketplace_root / ".agents" / "plugins" / "marketplace.json").read_text()
    )
    entry = manifest["plugins"][0]
    assert entry["name"] == "claude-smart"
    assert entry["source"]["path"] == "./plugins/claude-smart"
    copied_plugin = marketplace_root / "plugins" / "claude-smart"
    assert copied_plugin.is_dir()
    assert not copied_plugin.is_symlink()
    assert not stale_file.exists()


def test_prepare_codex_local_marketplace_filters_dev_artifacts(
    monkeypatch, tmp_path
) -> None:
    plugin_root = tmp_path / "repo" / "plugin"
    (plugin_root / "src" / "claude_smart").mkdir(parents=True)
    (plugin_root / "src" / "claude_smart" / "cli.py").write_text("# real\n")
    (plugin_root / "src" / "claude_smart" / "__pycache__").mkdir()
    (
        plugin_root / "src" / "claude_smart" / "__pycache__" / "cli.cpython-312.pyc"
    ).write_text("x")
    (plugin_root / "src" / "claude_smart" / "cli.pyc").write_text("x")
    (plugin_root / ".venv" / "bin").mkdir(parents=True)
    (plugin_root / ".venv" / "bin" / "python").write_text("x")
    (plugin_root / ".pytest_cache").mkdir()
    (plugin_root / ".pytest_cache" / "CACHEDIR.TAG").write_text("x")
    (plugin_root / ".ruff_cache").mkdir()
    (plugin_root / "dashboard" / "node_modules").mkdir(parents=True)
    (plugin_root / "dashboard" / "node_modules" / "package").mkdir()
    (plugin_root / "dashboard" / ".next" / "cache").mkdir(parents=True)
    marketplace_root = tmp_path / "marketplaces" / "reflexioai"
    monkeypatch.setattr(cli, "_PLUGIN_ROOT", plugin_root)
    monkeypatch.setattr(cli, "_CODEX_LOCAL_MARKETPLACE_ROOT", marketplace_root)

    cli._prepare_codex_local_marketplace()

    copied = marketplace_root / "plugins" / "claude-smart"
    assert (copied / "src" / "claude_smart" / "cli.py").is_file()
    assert not (copied / "src" / "claude_smart" / "__pycache__").exists()
    assert not (copied / "src" / "claude_smart" / "cli.pyc").exists()
    assert not (copied / ".venv").exists()
    assert not (copied / ".pytest_cache").exists()
    assert not (copied / ".ruff_cache").exists()
    assert not (copied / "dashboard" / "node_modules").exists()
    assert not (copied / "dashboard" / ".next").exists()


def test_npm_package_includes_codex_marketplace_payload() -> None:
    package = _read_json("package.json")
    files = set(package["files"])

    assert ".agents/plugins/marketplace.json" in files
    assert "plugin" in files
    assert "!plugin/**/__pycache__" in files
    assert "!plugin/**/*.py[cod]" in files


def test_release_bump_updates_codex_manifest() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text()

    assert "plugin/.codex-plugin/plugin.json" in makefile


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
    codex_env = (REPO_ROOT / "plugin" / "scripts" / "_codex_env.sh").read_text()
    backend_service = (
        REPO_ROOT / "plugin" / "scripts" / "backend-service.sh"
    ).read_text()
    assert set(hooks) == {"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"}
    assert "PreToolUse" not in hooks
    assert 'CLAUDE_SMART_HOST="codex"' in codex_env
    assert 'CLAUDE_SMART_CLI_PATH="$PLUGIN_ROOT/scripts/codex-claude-compat.py"' in (
        backend_service
    )
    for command in _command_hooks("plugin/hooks/codex-hooks.json"):
        assert "_codex_env.sh" in command
        assert "printenv PATH" not in command
        assert "$HOME/plugins/claude-smart" not in command
        assert "CLAUDE_PLUGIN_ROOT" in command
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


def test_codex_stop_skips_internal_prompt_from_transcript(
    session_dir, tmp_path, monkeypatch
) -> None:
    runtime.set_host(runtime.HOST_CODEX)
    calls: list[dict[str, Any]] = []
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "content": (
                                "You are a helpful assistant. You will be presented "
                                "with a user prompt, and your job is to provide a "
                                "short title for a task that will be created from "
                                "that prompt."
                            )
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "Fix bug"}]},
                    }
                ),
            ]
        )
        + "\n"
    )
    monkeypatch.setattr(
        stop.publish, "publish_unpublished", lambda **kw: calls.append(kw)
    )

    stop.handle({"session_id": "s1", "transcript_path": str(transcript)})

    assert state.read_all("s1") == []
    assert calls == []


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
    marketplace_root = tmp_path / "marketplaces" / "reflexioai"
    plugin_cache = (
        tmp_path / ".codex" / "plugins" / "cache" / "reflexioai" / "claude-smart"
    )
    monkeypatch.setattr(cli, "_REFLEXIO_ENV_PATH", env_path)
    monkeypatch.setattr(cli, "_CODEX_CONFIG_PATH", config_path)
    monkeypatch.setattr(cli, "_CODEX_LOCAL_MARKETPLACE_ROOT", marketplace_root)
    monkeypatch.setattr(cli, "_CODEX_PLUGIN_CACHE_DIR", plugin_cache)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/bin/{name}")
    trusted_cwds: list[Path] = []

    calls: list[list[str]] = []

    def fake_run_codex(args: list[str]):
        calls.append(args)
        if args == ["features", "enable", "plugin_hooks"]:
            return type(
                "Result", (), {"returncode": 1, "stdout": "", "stderr": "unsupported"}
            )()
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(cli, "_run_codex", fake_run_codex)
    monkeypatch.setattr(
        cli,
        "_trust_codex_plugin_hooks",
        lambda cwd: trusted_cwds.append(cwd) or (True, "trusted 7 hooks"),
    )

    rc = cli.cmd_install(argparse.Namespace(host="codex", source="unused"))

    assert rc == 0
    assert "CLAUDE_SMART_USE_LOCAL_CLI=1" in env_path.read_text()
    config = config_path.read_text()
    assert "plugin_hooks = true" in config
    assert '[plugins."claude-smart@reflexioai"]' in config
    assert "enabled = true" in config
    version = _read_json("plugin/.codex-plugin/plugin.json")["version"]
    assert (plugin_cache / version / ".codex-plugin" / "plugin.json").is_file()
    assert [
        "plugin",
        "marketplace",
        "add",
        str(marketplace_root),
    ] in calls
    assert ["features", "enable", "hooks"] in calls
    assert trusted_cwds
    out = capsys.readouterr().out
    assert "Codex support is installed" in out
    assert "trusted 7 hooks" in out
    assert "should show claude-smart as installed" in out


def test_codex_install_fails_when_marketplace_registration_fails(
    monkeypatch, tmp_path, capsys
) -> None:
    env_path = tmp_path / "reflexio" / ".env"
    config_path = tmp_path / ".codex" / "config.toml"
    marketplace_root = tmp_path / "marketplaces" / "reflexioai"
    monkeypatch.setattr(cli, "_REFLEXIO_ENV_PATH", env_path)
    monkeypatch.setattr(cli, "_CODEX_CONFIG_PATH", config_path)
    monkeypatch.setattr(cli, "_CODEX_LOCAL_MARKETPLACE_ROOT", marketplace_root)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/bin/{name}")

    def fake_run_codex(args: list[str]):
        return type(
            "Result", (), {"returncode": 1, "stdout": "", "stderr": "unsupported"}
        )()

    monkeypatch.setattr(cli, "_run_codex", fake_run_codex)

    rc = cli.cmd_install(argparse.Namespace(host="codex", source="unused"))

    assert rc == 1
    text = config_path.read_text()
    assert "hooks = true" in text
    assert "plugin_hooks = true" in text
    out = capsys.readouterr().out
    assert "marketplace registration failed" in out
    assert "ReflexioAI marketplace" in out


def test_install_codex_plugin_cache_enables_plugin_and_copies_versioned_cache(
    monkeypatch, tmp_path
) -> None:
    plugin_root = tmp_path / "plugin"
    (plugin_root / ".codex-plugin").mkdir(parents=True)
    (plugin_root / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"name": "claude-smart", "version": "9.8.7"})
    )
    (plugin_root / "hooks").mkdir()
    (plugin_root / "hooks" / "codex-hooks.json").write_text("{}")
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text('[plugins."browser@openai-bundled"]\nenabled = true\n')
    plugin_cache = (
        tmp_path / ".codex" / "plugins" / "cache" / "reflexioai" / "claude-smart"
    )
    monkeypatch.setattr(cli, "_CODEX_CONFIG_PATH", config)
    monkeypatch.setattr(cli, "_CODEX_PLUGIN_CACHE_DIR", plugin_cache)

    ok, message = cli._install_codex_plugin_cache(plugin_root)

    assert ok is True
    assert "9.8.7" in message
    assert (plugin_cache / "9.8.7" / "hooks" / "codex-hooks.json").is_file()
    text = config.read_text()
    assert '[plugins."browser@openai-bundled"]' in text
    assert '[plugins."claude-smart@reflexioai"]' in text
    assert "enabled = true" in text


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


def test_remove_toml_sections_removes_codex_plugin_state(tmp_path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        '[plugins."claude-smart@reflexioai"]\n'
        "enabled = true\n"
        "\n"
        '[hooks.state."claude-smart@reflexioai:hooks/codex-hooks.json:stop:0:0"]\n'
        'trusted_hash = "sha256:abc"\n'
        "\n"
        "[marketplaces.reflexioai]\n"
        'source_type = "local"\n'
        "\n"
        '[plugins."browser@openai-bundled"]\n'
        "enabled = true\n"
    )

    assert cli._remove_toml_sections(
        config,
        exact={
            'plugins."claude-smart@reflexioai"',
            "marketplaces.reflexioai",
        },
        prefixes=('hooks.state."claude-smart@reflexioai:',),
    )

    text = config.read_text()
    assert "claude-smart@reflexioai" not in text
    assert "marketplaces.reflexioai" not in text
    assert '[plugins."browser@openai-bundled"]' in text


def test_codex_uninstall_cleans_plugin_config_cache_and_marketplace(
    monkeypatch, tmp_path, capsys
) -> None:
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text(
        '[plugins."claude-smart@reflexioai"]\n'
        "enabled = true\n"
        "\n"
        '[hooks.state."claude-smart@reflexioai:hooks/codex-hooks.json:stop:0:0"]\n'
        'trusted_hash = "sha256:abc"\n'
        "\n"
        "[marketplaces.reflexioai]\n"
        'source_type = "local"\n'
        "\n"
        '[plugins."browser@openai-bundled"]\n'
        "enabled = true\n"
        "\n"
        "[marketplaces.openai-bundled]\n"
        'source_type = "builtin"\n'
        "\n"
        "[features]\n"
        "plugin_hooks = true\n"
    )
    marketplace_root = tmp_path / "marketplaces" / "reflexioai"
    marketplace_root.mkdir(parents=True)
    cache_root = tmp_path / ".codex" / "plugins" / "cache" / "reflexioai"
    plugin_cache = cache_root / "claude-smart"
    plugin_cache.mkdir(parents=True)

    monkeypatch.setattr(cli, "_CODEX_CONFIG_PATH", config)
    monkeypatch.setattr(cli, "_CODEX_LOCAL_MARKETPLACE_ROOT", marketplace_root)
    monkeypatch.setattr(cli, "_CODEX_PLUGIN_CACHE_DIR", plugin_cache)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(
        cli,
        "_run_codex",
        lambda _args: type(
            "Result", (), {"returncode": 0, "stdout": "", "stderr": ""}
        )(),
    )

    rc = cli.cmd_uninstall(argparse.Namespace(host="codex"))

    assert rc == 0
    assert not marketplace_root.exists()
    assert not plugin_cache.exists()
    assert not cache_root.exists()
    text = config.read_text()
    assert "claude-smart@reflexioai" not in text
    assert "marketplaces.reflexioai" not in text
    assert "plugin_hooks = true" in text
    assert '[plugins."browser@openai-bundled"]' in text
    assert "[marketplaces.openai-bundled]" in text
    assert "plugin and marketplace state removed" in capsys.readouterr().out


def test_set_codex_hook_states_replaces_only_claude_smart_hooks(tmp_path) -> None:
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text(
        "[hooks.state]\n"
        "\n"
        '[hooks.state."claude-smart@reflexioai:hooks/codex-hooks.json:stop:0:0"]\n'
        'trusted_hash = "sha256:old"\n'
        "\n"
        '[hooks.state."other@market:hooks.json:stop:0:0"]\n'
        'trusted_hash = "sha256:other"\n'
    )

    ok = cli._set_codex_hook_states(
        config,
        {
            "claude-smart@reflexioai:hooks/codex-hooks.json:post_tool_use:0:0": (
                "sha256:new"
            ),
            "claude-smart@reflexioai:hooks/codex-hooks.json:session_start:0:0": (
                "sha256:start"
            ),
        },
    )

    assert ok is True
    text = config.read_text()
    assert "sha256:old" not in text
    assert "sha256:new" in text
    assert "sha256:start" in text
    assert "enabled = true" in text
    assert "other@market" in text


def test_trust_codex_plugin_hooks_uses_app_server_current_hashes(
    monkeypatch, tmp_path
) -> None:
    config = tmp_path / ".codex" / "config.toml"
    monkeypatch.setattr(cli, "_CODEX_CONFIG_PATH", config)

    def fake_list(_cwd: Path):
        return (
            True,
            [
                {
                    "pluginId": "claude-smart@reflexioai",
                    "key": "claude-smart@reflexioai:hooks/codex-hooks.json:stop:0:0",
                    "currentHash": "sha256:stop",
                },
                {
                    "pluginId": "other@market",
                    "key": "other@market:hooks.json:stop:0:0",
                    "currentHash": "sha256:other",
                },
            ],
            "ok",
        )

    monkeypatch.setattr(cli, "_list_codex_plugin_hooks", fake_list)

    ok, message = cli._trust_codex_plugin_hooks(tmp_path)

    assert ok is True
    assert "1 claude-smart Codex hooks" in message
    text = config.read_text()
    assert "sha256:stop" in text
    assert "sha256:other" not in text


def test_set_codex_hook_states_preserves_unrelated_hooks_state_root_table(
    tmp_path,
) -> None:
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text(
        "[hooks.state]\n"
        "\n"
        '[hooks.state."other@market:hooks.json:stop:0:0"]\n'
        'trusted_hash = "sha256:other"\n'
        "enabled = false\n"
        'extra = "preserved"\n'
    )

    ok = cli._set_codex_hook_states(
        config,
        {
            "claude-smart@reflexioai:hooks/codex-hooks.json:stop:0:0": "sha256:cs",
        },
    )

    assert ok is True
    text = config.read_text()
    # The unrelated plugin's full body must survive intact.
    assert '[hooks.state."other@market:hooks.json:stop:0:0"]' in text
    assert 'trusted_hash = "sha256:other"' in text
    assert "enabled = false" in text
    assert 'extra = "preserved"' in text
    # New entry was appended.
    assert (
        '[hooks.state."claude-smart@reflexioai:hooks/codex-hooks.json:stop:0:0"]'
        in text
    )
    assert "sha256:cs" in text


def test_trust_codex_plugin_hooks_returns_failure_when_app_server_lists_no_hooks(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(cli, "_CODEX_CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(
        cli,
        "_list_codex_plugin_hooks",
        lambda _cwd: (True, [], "found 0 claude-smart hooks"),
    )

    ok, message = cli._trust_codex_plugin_hooks(tmp_path)

    assert ok is False
    assert "did not report trust hashes" in message


def test_trust_codex_plugin_hooks_returns_failure_when_app_server_subprocess_errors(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(cli, "_CODEX_CONFIG_PATH", tmp_path / "config.toml")

    def fake_popen(*_args, **_kwargs):
        raise OSError("codex not on PATH")

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)

    ok, message = cli._trust_codex_plugin_hooks(tmp_path)

    assert ok is False
    assert "could not start Codex app-server" in message


def test_codex_install_exits_nonzero_when_only_trust_fails(
    monkeypatch, tmp_path, capsys
) -> None:
    env_path = tmp_path / "reflexio" / ".env"
    config_path = tmp_path / ".codex" / "config.toml"
    marketplace_root = tmp_path / "marketplaces" / "reflexioai"
    plugin_cache = (
        tmp_path / ".codex" / "plugins" / "cache" / "reflexioai" / "claude-smart"
    )
    monkeypatch.setattr(cli, "_REFLEXIO_ENV_PATH", env_path)
    monkeypatch.setattr(cli, "_CODEX_CONFIG_PATH", config_path)
    monkeypatch.setattr(cli, "_CODEX_LOCAL_MARKETPLACE_ROOT", marketplace_root)
    monkeypatch.setattr(cli, "_CODEX_PLUGIN_CACHE_DIR", plugin_cache)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)

    def fake_run_codex(_args: list[str]):
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    trust_calls = {"n": 0}

    def fake_trust(_cwd: Path):
        trust_calls["n"] += 1
        return False, "Codex did not report trust hashes for claude-smart hooks"

    monkeypatch.setattr(cli, "_run_codex", fake_run_codex)
    monkeypatch.setattr(cli, "_trust_codex_plugin_hooks", fake_trust)

    rc = cli.cmd_install(argparse.Namespace(host="codex", source="unused"))

    assert rc == 1
    # Trust must have been retried at least once.
    assert trust_calls["n"] >= 2
    captured = capsys.readouterr()
    assert "hook trust could not be completed" in captured.out
    assert "did not report trust hashes" in captured.err


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
