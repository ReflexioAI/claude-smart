"""Tests for OpenCode host support."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

from claude_smart import cli, hook, runtime, state
from claude_smart.events import post_tool, stop, user_prompt

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read_json(path: str) -> dict[str, Any]:
    return json.loads((REPO_ROOT / path).read_text())


def _run_node_script(script: str, *args: str) -> subprocess.CompletedProcess[str] | None:
    node = shutil_which_node()
    if node is None:
        return None
    return subprocess.run(
        [node, "-e", script, *args],
        text=True,
        capture_output=True,
        check=False,
    )


def test_package_exposes_opencode_server_entrypoint() -> None:
    package = _read_json("package.json")

    assert package["exports"]["./server"]["import"] == "./plugin/opencode/dist/server.mjs"
    assert "build:opencode" in package["scripts"]
    assert "opencode" in package["keywords"]
    assert "opencode-plugin" in package["keywords"]
    assert package["engines"]["opencode"] == ">=1.17.0"


def test_opencode_bridge_does_not_call_pre_tool() -> None:
    server = (REPO_ROOT / "plugin" / "opencode" / "server.mts").read_text()

    assert '"tool.execute.before"' in server
    assert '"pre-tool"' not in server
    assert '["opencode", "post-tool"]' in server
    assert '["opencode", "user-prompt"]' in server
    assert '["opencode", "stop"]' in server


def test_runtime_accepts_opencode_host() -> None:
    assert runtime.set_host("opencode") == "opencode"
    assert runtime.host() == "opencode"
    assert runtime.is_opencode()
    assert runtime.agent_version() == "claude-code"


def test_hook_entry_accepts_opencode_host() -> None:
    script = (REPO_ROOT / "plugin" / "scripts" / "hook_entry.sh").read_text()

    assert "claude-code|codex|opencode" in script


def test_hook_main_accepts_opencode_host(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_read() -> dict[str, Any]:
        return {"session_id": "s1"}

    def fake_handlers():
        return {"stop": lambda _payload: calls.append((runtime.host(), "stop"))}

    monkeypatch.setattr(hook, "_read_stdin_json", fake_read)
    monkeypatch.setattr(hook, "_load_handlers", fake_handlers)

    assert hook.main(["opencode", "stop"]) == 0
    assert calls == [("opencode", "stop")]


def test_opencode_stop_uses_last_assistant_message(session_dir, monkeypatch) -> None:
    runtime.set_host(runtime.HOST_OPENCODE)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        stop.publish, "publish_unpublished", lambda **kw: calls.append(kw)
    )
    monkeypatch.setattr(stop.ids, "resolve_user_id", lambda *_a, **_kw: "demo")

    stop.handle(
        {
            "session_id": "s1",
            "cwd": str(REPO_ROOT),
            "last_assistant_message": "final answer from opencode",
            "transcript_path": "/does/not/exist.jsonl",
        }
    )

    records = state.read_all("s1")
    assert records[-1]["role"] == "Assistant"
    assert records[-1]["content"] == "final answer from opencode"
    assert calls and calls[0]["project_id"] == "demo"


def test_opencode_learning_loop_buffers_injects_tools_and_publishes(
    session_dir, monkeypatch
) -> None:
    runtime.set_host(runtime.HOST_OPENCODE)
    published: list[dict[str, Any]] = []
    injected: list[dict[str, Any]] = []

    monkeypatch.setattr(user_prompt.ids, "resolve_user_id", lambda *_a, **_kw: "demo")
    monkeypatch.setattr(stop.ids, "resolve_user_id", lambda *_a, **_kw: "demo")
    monkeypatch.setattr(
        user_prompt.context_inject,
        "emit_context",
        lambda **kw: injected.append(kw) or True,
    )
    monkeypatch.setattr(
        stop.publish,
        "publish_unpublished",
        lambda **kw: published.append(kw) or ("ok", 3),
    )

    user_prompt.handle(
        {
            "session_id": "s-loop",
            "cwd": str(REPO_ROOT),
            "prompt": "Use my learned rules before editing AGENTS.md",
        }
    )
    post_tool.handle(
        {
            "session_id": "s-loop",
            "tool_name": "Bash",
            "tool_input": {"command": "TOKEN=Abcdefghijk1234567890 echo safe"},
            "tool_response": {"stdout": "done"},
        }
    )
    result = stop.handle(
        {
            "session_id": "s-loop",
            "cwd": str(REPO_ROOT),
            "last_assistant_message": "Applied the remembered rule.",
        }
    )

    records = state.read_all("s-loop")
    assert result == ("ok", 3)
    assert injected == [
        {
            "session_id": "s-loop",
            "project_id": "demo",
            "query": "Use my learned rules before editing AGENTS.md",
            "hook_event_name": "UserPromptSubmit",
            "top_k": 3,
        }
    ]
    assert [record["role"] for record in records] == [
        "User",
        "Assistant_tool",
        "Assistant",
    ]
    assert records[0]["content"] == "Use my learned rules before editing AGENTS.md"
    assert records[1]["tool_output"] == "done"
    assert records[1]["tool_input"]["command"] == "TOKEN=<redacted:21> echo safe"
    assert records[2]["content"] == "Applied the remembered rule."
    assert published == [
        {
            "session_id": "s-loop",
            "project_id": "demo",
            "force_extraction": False,
            "skip_aggregation": False,
        }
    ]


def test_opencode_config_patch_adds_singular_plugin_and_preserves_unrelated(
    tmp_path: Path,
) -> None:
    config = tmp_path / ".opencode" / "opencode.jsonc"
    config.parent.mkdir()
    config.write_text(
        '{\n'
        '  // existing user config\n'
        '  "theme": "system",\n'
        '  "plugin": ["other-plugin",],\n'
        '}\n'
    )

    changed, path = cli._patch_opencode_plugin_config(config, install=True)

    assert changed is True
    assert path == config
    parsed = json.loads(config.read_text())
    assert parsed["theme"] == "system"
    assert parsed["plugin"] == ["other-plugin", "claude-smart"]


def test_opencode_config_patch_uninstall_removes_only_claude_smart(
    tmp_path: Path,
) -> None:
    config = tmp_path / ".opencode" / "opencode.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps(
            {
                "plugin": [
                    "other-plugin",
                    "claude-smart",
                    ["another-plugin", {"enabled": True}],
                ],
                "model": "anthropic/claude-sonnet-4",
            }
        )
    )

    changed, _ = cli._patch_opencode_plugin_config(config, install=False)

    assert changed is True
    parsed = json.loads(config.read_text())
    assert parsed["plugin"] == [
        "other-plugin",
        ["another-plugin", {"enabled": True}],
    ]
    assert parsed["model"] == "anthropic/claude-sonnet-4"


def test_opencode_config_patch_install_dedupes_existing_entries(
    tmp_path: Path,
) -> None:
    config = tmp_path / ".opencode" / "opencode.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps({"plugin": ["claude-smart", "other-plugin", "claude-smart"]})
    )

    changed, _ = cli._patch_opencode_plugin_config(config, install=True)

    assert changed is True
    assert json.loads(config.read_text())["plugin"] == ["other-plugin", "claude-smart"]


def test_opencode_jsonc_parser_preserves_comma_brace_inside_strings() -> None:
    parsed = cli._strip_jsonc('{"prompt": "keep,}", "plugin": ["claude-smart",],}\n')

    assert json.loads(parsed) == {"prompt": "keep,}", "plugin": ["claude-smart"]}


def test_opencode_config_patch_uninstall_noops_without_plugin_array(
    tmp_path: Path,
) -> None:
    config = tmp_path / ".opencode" / "opencode.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"theme": "system"}))

    changed, _ = cli._patch_opencode_plugin_config(config, install=False)

    assert changed is False
    assert json.loads(config.read_text()) == {"theme": "system"}


def test_opencode_install_from_python_wheel_path_points_to_npm(
    monkeypatch, tmp_path, capsys
) -> None:
    monkeypatch.setattr(cli, "_SCRIPTS_DIR", tmp_path)

    rc = cli.cmd_install(argparse.Namespace(host="opencode", global_config=False))

    assert rc == 1
    assert "npx claude-smart install --host opencode" in capsys.readouterr().err


def test_opencode_install_fails_without_extraction_provider(monkeypatch, tmp_path) -> None:
    env_path = tmp_path / ".reflexio" / ".env"
    monkeypatch.setattr(cli, "_REFLEXIO_ENV_PATH", env_path)
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)

    rc = cli.cmd_install(argparse.Namespace(host="opencode", global_config=False))

    assert rc == 1


def test_opencode_install_patches_project_config_after_bootstrap(
    monkeypatch, tmp_path, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_REFLEXIO_ENV_PATH", tmp_path / ".reflexio" / ".env")
    monkeypatch.setattr(
        cli.shutil, "which", lambda name: f"/bin/{name}" if name == "codex" else None
    )
    monkeypatch.setattr(cli, "_bootstrap_opencode_install", lambda _read_only: (True, "/plugin"))

    rc = cli.cmd_install(argparse.Namespace(host="opencode", global_config=False))

    assert rc == 0
    parsed = json.loads((tmp_path / ".opencode" / "opencode.json").read_text())
    assert parsed["plugin"] == ["claude-smart"]
    assert "Restart OpenCode" in capsys.readouterr().out


def test_node_installer_patches_opencode_jsonc(tmp_path: Path) -> None:
    if shutil_which_node() is None:
        return
    config = tmp_path / "opencode.jsonc"
    config.write_text('{"plugin": ["other",], // keep parseable\n"theme": "system"}\n')
    script = (
        f"const installer = require({json.dumps(str(REPO_ROOT / 'bin' / 'claude-smart.js'))});"
        f"installer.patchOpenCodePluginConfig({json.dumps(str(config))}, {{install: true}});"
        "process.stdout.write(require('fs').readFileSync(process.argv[1], 'utf8'));"
    )

    result = _run_node_script(script, str(config))
    assert result is not None

    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed["plugin"] == ["other", "claude-smart"]
    assert parsed["theme"] == "system"


def test_node_installer_uninstalls_and_preserves_other_plugin_shapes(
    tmp_path: Path,
) -> None:
    if shutil_which_node() is None:
        return
    config = tmp_path / "opencode.json"
    config.write_text(
        json.dumps(
            {
                "plugin": [
                    "claude-smart",
                    "other",
                    ["tuple-plugin", {"enabled": True}],
                    "claude-smart",
                ],
                "theme": "system",
            }
        )
    )
    script = (
        f"const installer = require({json.dumps(str(REPO_ROOT / 'bin' / 'claude-smart.js'))});"
        f"installer.patchOpenCodePluginConfig({json.dumps(str(config))}, {{install: false}});"
        "process.stdout.write(require('fs').readFileSync(process.argv[1], 'utf8'));"
    )

    result = _run_node_script(script, str(config))
    assert result is not None

    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed["plugin"] == ["other", ["tuple-plugin", {"enabled": True}]]
    assert parsed["theme"] == "system"


def test_node_installer_uninstall_noops_without_plugin_array(tmp_path: Path) -> None:
    if shutil_which_node() is None:
        return
    config = tmp_path / "opencode.json"
    config.write_text(json.dumps({"theme": "system"}))
    script = (
        f"const installer = require({json.dumps(str(REPO_ROOT / 'bin' / 'claude-smart.js'))});"
        f"const result = installer.patchOpenCodePluginConfig({json.dumps(str(config))}, {{install: false}});"
        "process.stdout.write(JSON.stringify({ result, text: require('fs').readFileSync(process.argv[1], 'utf8') }));"
    )

    result = _run_node_script(script, str(config))
    assert result is not None

    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed["result"]["changed"] is False
    assert json.loads(parsed["text"]) == {"theme": "system"}


def test_node_jsonc_parser_preserves_comma_brace_inside_strings() -> None:
    script = (
        f"const installer = require({json.dumps(str(REPO_ROOT / 'bin' / 'claude-smart.js'))});"
        "process.stdout.write(installer.stripJsonc('{\"prompt\":\"keep,}\",\"plugin\":[\"claude-smart\",],}\\n'));"
    )

    result = _run_node_script(script)
    assert result is not None

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"prompt": "keep,}", "plugin": ["claude-smart"]}


def test_node_opencode_payload_normalizes_tool_contracts() -> None:
    script = f"""
      import({json.dumps(str(REPO_ROOT / "plugin" / "opencode" / "dist" / "payload.js"))}).then((payload) => {{
        const edit = payload.toolBeforePayload(
          {{ sessionID: "s1", tool: "edit" }},
          {{ args: {{ filePath: "README.md", oldString: "old", newString: "new" }} }},
          "/repo"
        );
        const patch = payload.toolBeforePayload(
          {{ sessionID: "s1", tool: "apply_patch" }},
          {{ args: {{ patchText: "*** Begin Patch" }} }},
          "/repo"
        );
        const shell = payload.toolAfterPayload(
          {{ sessionID: "s1", tool: "shell", args: {{ cmd: "npm test" }} }},
          {{ output: "ok", title: "test", metadata: {{ error: "boom" }} }},
          "/repo"
        );
        process.stdout.write(JSON.stringify({{ edit, patch, shell }}));
      }}).catch((err) => {{
        console.error(err);
        process.exit(1);
      }});
    """

    result = _run_node_script(script)
    assert result is not None

    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed["edit"]["tool_name"] == "Edit"
    assert parsed["edit"]["tool_input"] == {
        "filePath": "README.md",
        "oldString": "old",
        "newString": "new",
        "file_path": "README.md",
        "old_string": "old",
        "new_string": "new",
    }
    assert parsed["patch"]["tool_name"] == "apply_patch"
    assert parsed["patch"]["tool_input"]["command"] == "*** Begin Patch"
    assert parsed["shell"]["tool_name"] == "Bash"
    assert parsed["shell"]["tool_input"]["command"] == "npm test"
    assert parsed["shell"]["tool_response"] == {
        "output": "ok",
        "stdout": "ok",
        "title": "test",
        "metadata": {"error": "boom"},
        "error": "boom",
    }


def test_node_opencode_assistant_buffer_tracks_last_assistant_turn() -> None:
    script = f"""
      import({json.dumps(str(REPO_ROOT / "plugin" / "opencode" / "dist" / "assistant-buffer.js"))}).then((mod) => {{
        const buffer = new mod.AssistantBuffer();
        buffer.update({{ type: "message.updated", properties: {{ sessionID: "s1", info: {{ id: "m1", role: "assistant" }} }} }});
        buffer.update({{ type: "message.part.updated", properties: {{ sessionID: "s1", part: {{ id: "p1", messageID: "m1", type: "text", text: "hello" }} }} }});
        buffer.update({{ type: "message.part.delta", properties: {{ sessionID: "s1", partID: "p1", delta: " world" }} }});
        buffer.update({{ type: "message.part.updated", properties: {{ sessionID: "s1", part: {{ id: "p2", messageID: "m1", type: "reasoning", text: "reason" }} }} }});
        buffer.update({{ type: "message.part.delta", properties: {{ sessionID: "s1", partID: "p2", delta: " leaked" }} }});
        buffer.update({{ type: "message.part.delta", properties: {{ sessionID: "s1", partID: "p3", delta: " unknown" }} }});
        const beforeClear = buffer.text("s1");
        buffer.clear("s1");
        process.stdout.write(JSON.stringify({{ beforeClear, afterClear: buffer.text("s1") }}));
      }}).catch((err) => {{
        console.error(err);
        process.exit(1);
      }});
    """

    result = _run_node_script(script)
    assert result is not None

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "beforeClear": "hello world",
        "afterClear": "",
    }


def test_opencode_dist_files_are_packaged() -> None:
    package = _read_json("package.json")

    assert "plugin" in package["files"]
    assert package["exports"]["./server"]["import"] == "./plugin/opencode/dist/server.mjs"


def test_node_parse_host_accepts_opencode() -> None:
    installer = (REPO_ROOT / "bin" / "claude-smart.js").read_text()

    assert 'value !== "claude-code" && value !== "codex" && value !== "opencode"' in installer
    assert "runInstallOpenCode" in installer
    assert "runUninstallOpenCode" in installer


def shutil_which_node() -> str | None:
    import shutil

    return shutil.which("node")
