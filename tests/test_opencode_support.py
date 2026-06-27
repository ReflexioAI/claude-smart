"""Tests for OpenCode host support."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import textwrap
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest  # type: ignore[reportMissingImports]

from claude_smart import cli, hook, runtime, state
from claude_smart.events import post_tool, stop, user_prompt

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def restore_runtime_host() -> Generator[None, None, None]:
    previous_host = getattr(runtime, "_current_host", None)
    previous_env = os.environ.get(runtime.HOST_ENV)
    yield
    runtime._current_host = previous_host
    if previous_env is None:
        os.environ.pop(runtime.HOST_ENV, None)
    else:
        os.environ[runtime.HOST_ENV] = previous_env


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


def _write_fake_bash_recorder(tmp_path: Path, *, user_prompt_context: str = "") -> Path:
    fake_bash = tmp_path / "bash"
    response = (
        f'{{"hookSpecificOutput":{{"additionalContext":{json.dumps(user_prompt_context)}}}}}'
        if user_prompt_context
        else "{}"
    )
    fake_bash.write_text(
        "#!/bin/sh\n"
        "script=\"$1\"\n"
        "shift\n"
        "payload=$(cat || true)\n"
        "PAYLOAD=\"$payload\" node -e 'require(\"fs\").appendFileSync(process.env.CALL_LOG, JSON.stringify({ script: process.argv[1], args: process.argv.slice(2), payload: process.env.PAYLOAD }) + \"\\n\")' \"$script\" \"$@\"\n"
        "if [ \"${2:-}\" = \"user-prompt\" ]; then\n"
        f"  printf '%s\\n' '{response}'\n"
        "else\n"
        "  printf '%s\\n' '{}'\n"
        "fi\n",
    )
    fake_bash.chmod(0o755)
    return fake_bash


def test_package_exposes_opencode_server_entrypoint() -> None:
    package = _read_json("package.json")

    assert package["exports"]["./server"]["import"] == "./plugin/opencode/dist/server.mjs"
    assert "build:opencode" in package["scripts"]
    assert "opencode" in package["keywords"]
    assert "opencode-plugin" in package["keywords"]
    assert package["engines"]["opencode"] == ">=1.17.0"
    assert package["description"].startswith("Self-improving Claude Code, Codex, and OpenCode")
    assert package["oc-plugin"] == ["server"]


def test_opencode_package_includes_local_cli_bridge() -> None:
    for name in (
        "opencode-claude-compat",
        "opencode-claude-compat.cmd",
        "opencode-claude-compat.js",
    ):
        assert (REPO_ROOT / "plugin" / "scripts" / name).is_file()


def test_opencode_bridge_does_not_call_pre_tool() -> None:
    server = (REPO_ROOT / "plugin" / "opencode" / "server.mts").read_text()

    assert '"tool.execute.before"' not in server
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


def test_opencode_config_patch_preserves_legacy_plugin_field_and_unrelated(
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


def test_opencode_config_patch_fresh_config_uses_current_plugin_field(
    tmp_path: Path,
) -> None:
    config = tmp_path / "opencode.json"

    changed, _ = cli._patch_opencode_plugin_config(config, install=True)

    assert changed is True
    assert json.loads(config.read_text()) == {"plugin": ["claude-smart"]}


def test_opencode_config_patch_migrates_existing_plugins_field(
    tmp_path: Path,
) -> None:
    config = tmp_path / "opencode.json"
    config.write_text(json.dumps({"plugins": ["other-plugin"], "theme": "system"}))

    changed, _ = cli._patch_opencode_plugin_config(config, install=True)

    parsed = json.loads(config.read_text())
    assert changed is True
    assert parsed["plugin"] == ["other-plugin", "claude-smart"]
    assert "plugins" not in parsed
    assert parsed["theme"] == "system"


def test_opencode_config_patch_uninstall_migrates_existing_plugins_field(
    tmp_path: Path,
) -> None:
    config = tmp_path / "opencode.json"
    config.write_text(
        json.dumps({"plugins": ["claude-smart", "other-plugin", "claude-smart"]})
    )

    changed, _ = cli._patch_opencode_plugin_config(config, install=False)

    assert changed is True
    assert json.loads(config.read_text()) == {"plugin": ["other-plugin"]}


def test_opencode_jsonc_parser_preserves_comma_brace_inside_strings() -> None:
    parsed = cli._strip_jsonc('{"prompt": "keep,}", "plugin": ["claude-smart",],}\n')

    assert json.loads(parsed) == {"prompt": "keep,}", "plugin": ["claude-smart"]}


def test_opencode_jsonc_parser_skips_comments_after_trailing_commas() -> None:
    parsed = cli._strip_jsonc(
        '{\n'
        '  "plugin": [\n'
        '    "other-plugin", // keep plugin note\n'
        '  ], /* trailing array comment */\n'
        '}\n'
    )

    assert json.loads(parsed) == {"plugin": ["other-plugin"]}


def test_opencode_config_patch_rejects_non_array_plugin_without_rewrite(
    tmp_path: Path,
) -> None:
    config = tmp_path / ".opencode" / "opencode.json"
    config.parent.mkdir()
    original = json.dumps({"plugin": "other-plugin", "theme": "system"})
    config.write_text(original)

    with pytest.raises(ValueError, match='field "plugin" must be a JSON array'):
        cli._patch_opencode_plugin_config(config, install=True)

    assert config.read_text() == original


def test_opencode_config_patch_rejects_non_array_plugins_without_rewrite(
    tmp_path: Path,
) -> None:
    config = tmp_path / "opencode.json"
    original = json.dumps({"plugins": "other-plugin", "theme": "system"})
    config.write_text(original)

    with pytest.raises(ValueError, match='field "plugins" must be a JSON array'):
        cli._patch_opencode_plugin_config(config, install=True)

    assert config.read_text() == original


def test_opencode_config_patch_uninstall_noops_without_plugin_array(
    tmp_path: Path,
) -> None:
    config = tmp_path / ".opencode" / "opencode.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"theme": "system"}))

    changed, _ = cli._patch_opencode_plugin_config(config, install=False)

    assert changed is False
    assert json.loads(config.read_text()) == {"theme": "system"}


def test_opencode_config_patch_backs_up_jsonc_config(tmp_path: Path) -> None:
    config = tmp_path / ".opencode" / "opencode.jsonc"
    config.parent.mkdir()
    original = (
        '{\n'
        '  // keep my notes\n'
        '  "theme": "system",\n'
        '  "plugin": ["other-plugin"]\n'
        '}\n'
    )
    config.write_text(original)

    changed, _ = cli._patch_opencode_plugin_config(config, install=True)

    assert changed is True
    assert config.with_suffix(config.suffix + ".bak").read_text() == original
    assert json.loads(config.read_text())["plugin"] == ["other-plugin", "claude-smart"]


def test_opencode_config_patch_skips_backup_for_plain_json(tmp_path: Path) -> None:
    config = tmp_path / ".opencode" / "opencode.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"plugin": ["other-plugin"]}))

    changed, _ = cli._patch_opencode_plugin_config(config, install=True)

    assert changed is True
    assert not config.with_suffix(config.suffix + ".bak").exists()


def test_opencode_global_config_path_honors_xdg_config_home(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    path = cli._opencode_config_path(global_config=True)

    assert path == tmp_path / "xdg" / "opencode" / "opencode.json"


def test_opencode_global_config_path_defaults_without_xdg(monkeypatch) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    path = cli._opencode_config_path(global_config=True)

    assert path == Path.home() / ".config" / "opencode" / "opencode.json"


def test_opencode_project_config_path_defaults_to_root(tmp_path: Path) -> None:
    path = cli._opencode_config_path(cwd=tmp_path)

    assert path == tmp_path / "opencode.json"


def test_opencode_project_config_path_prefers_existing_root_config(
    tmp_path: Path,
) -> None:
    root_config = tmp_path / "opencode.jsonc"
    legacy_config = tmp_path / ".opencode" / "opencode.json"
    legacy_config.parent.mkdir()
    root_config.write_text('{"plugin": []}\n')
    legacy_config.write_text('{"plugin": ["legacy"]}\n')

    path = cli._opencode_config_path(cwd=tmp_path)

    assert path == root_config


def test_opencode_project_config_path_honors_existing_dot_opencode_config(
    tmp_path: Path,
) -> None:
    legacy_config = tmp_path / ".opencode" / "opencode.json"
    legacy_config.parent.mkdir()
    legacy_config.write_text('{"plugin": []}\n')

    path = cli._opencode_config_path(cwd=tmp_path)

    assert path == legacy_config


def test_opencode_install_from_python_wheel_path_points_to_npm(
    monkeypatch, tmp_path, capsys
) -> None:
    monkeypatch.setattr(cli, "_SCRIPTS_DIR", tmp_path)

    rc = cli.cmd_install(argparse.Namespace(host="opencode", global_config=False))

    assert rc == 1
    assert "npx claude-smart install --host opencode" in capsys.readouterr().err


def test_opencode_bootstrap_guard_rejects_unsupported_package(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(cli, "_SCRIPTS_DIR", tmp_path)

    bootstrapped, message = cli._bootstrap_opencode_install(read_only=False)

    assert bootstrapped is False
    assert "npx claude-smart install --host opencode" in message


def test_opencode_install_fails_without_extraction_provider(monkeypatch, tmp_path) -> None:
    env_path = tmp_path / ".reflexio" / ".env"
    monkeypatch.setattr(cli, "_REFLEXIO_ENV_PATH", env_path)
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)

    rc = cli.cmd_install(argparse.Namespace(host="opencode", global_config=False))

    assert rc == 1


def test_opencode_extraction_provider_requires_executable_cli_path(
    monkeypatch, tmp_path
) -> None:
    cli_path = tmp_path / "claude-smart-provider"
    cli_path.write_text("#!/bin/sh\nexit 0\n")
    cli_path.chmod(0o644)
    monkeypatch.setattr(cli, "_REFLEXIO_ENV_PATH", tmp_path / ".reflexio" / ".env")
    monkeypatch.setenv("CLAUDE_SMART_CLI_PATH", str(cli_path))
    monkeypatch.delenv(cli.env_config.REFLEXIO_API_KEY_ENV, raising=False)
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)

    assert cli._has_extraction_provider() is False

    cli_path.chmod(0o755)
    assert cli._has_extraction_provider() is True


def test_opencode_extraction_provider_accepts_opencode_only(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(cli, "_REFLEXIO_ENV_PATH", tmp_path / ".reflexio" / ".env")
    monkeypatch.delenv("CLAUDE_SMART_CLI_PATH", raising=False)
    monkeypatch.delenv(cli.env_config.REFLEXIO_API_KEY_ENV, raising=False)
    monkeypatch.setattr(
        cli.shutil, "which", lambda name: f"/bin/{name}" if name == "opencode" else None
    )

    assert cli._has_extraction_provider() is True


def test_opencode_backend_service_prefers_opencode_bridge() -> None:
    service = (REPO_ROOT / "plugin" / "scripts" / "backend-service.sh").read_text()

    assert 'CLAUDE_SMART_HOST:-claude-code}" = "opencode"' in service
    assert 'CLAUDE_SMART_CLI_PATH="$PLUGIN_ROOT/scripts/opencode-claude-compat"' in service
    assert 'CLAUDE_SMART_CLI_PATH="$PLUGIN_ROOT/scripts/codex-claude-compat"' in service


def test_opencode_cli_bridge_translates_claude_contract(tmp_path: Path) -> None:
    if shutil_which_node() is None:
        return
    bridge = REPO_ROOT / "plugin" / "scripts" / "opencode-claude-compat.js"
    fake_opencode = tmp_path / "opencode"
    call_log = tmp_path / "calls.json"
    fake_opencode.write_text(
        f"""#!/usr/bin/env node
const fs = require("fs");
fs.writeFileSync(
  {json.dumps(str(call_log))},
  JSON.stringify({{
    args: process.argv.slice(2),
    cwd: process.cwd(),
    stdin: fs.readFileSync(0, "utf8"),
    env: {{
      CLAUDE_SMART_HOST: process.env.CLAUDE_SMART_HOST,
      CLAUDE_SMART_INTERNAL: process.env.CLAUDE_SMART_INTERNAL
    }}
  }})
);
process.stdout.write(JSON.stringify({{
  type: "text",
  part: {{ type: "text", text: "bridge output" }}
}}) + "\\n");
"""
    )
    fake_opencode.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}",
    }

    result = subprocess.run(
        [
            shutil_which_node() or "node",
            str(bridge),
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--append-system-prompt",
            "system text",
            "--model",
            "claude-sonnet-4-6",
        ],
        input="user prompt",
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "type": "result",
        "subtype": "success",
        "result": "bridge output",
    }
    call = json.loads(call_log.read_text())
    assert call["env"] == {
        "CLAUDE_SMART_HOST": "opencode",
        "CLAUDE_SMART_INTERNAL": "1",
    }
    assert call["args"][:4] == ["run", "--pure", "--format", "json"]
    assert "--agent" in call["args"]
    assert "--dir" in call["args"]
    assert "--model" not in call["args"]
    assert "system text\n\n## Task\nuser prompt" not in call["args"]
    assert call["stdin"] == "system text\n\n## Task\nuser prompt"


def test_opencode_cli_bridge_pipes_large_prompt_via_stdin(tmp_path: Path) -> None:
    if shutil_which_node() is None:
        return
    bridge = REPO_ROOT / "plugin" / "scripts" / "opencode-claude-compat.js"
    fake_opencode = tmp_path / "opencode"
    call_log = tmp_path / "calls.json"
    fake_opencode.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env node
            const fs = require("fs");
            const stdin = fs.readFileSync(0, "utf8");
            fs.writeFileSync(
              {json.dumps(str(call_log))},
              JSON.stringify({{
                args: process.argv.slice(2),
                stdinLength: stdin.length
              }})
            );
            process.stdout.write(JSON.stringify({{
              type: "result",
              result: "large prompt ok"
            }}) + "\\n");
            """
        )
    )
    fake_opencode.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    prompt = "x" * 200_000

    result = subprocess.run(
        [
            shutil_which_node() or "node",
            str(bridge),
            "-p",
            "--output-format",
            "stream-json",
        ],
        input=prompt,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["result"] == "large prompt ok"
    call = json.loads(call_log.read_text())
    assert prompt not in call["args"]
    assert call["stdinLength"] == len(prompt)


def test_node_installer_accepts_opencode_only_extraction_provider(tmp_path: Path) -> None:
    if shutil_which_node() is None:
        return
    fake_opencode = tmp_path / "opencode"
    fake_opencode.write_text("#!/bin/sh\nexit 0\n")
    fake_opencode.chmod(0o755)
    script = (
        f"process.env.PATH = {json.dumps(str(tmp_path))} + ':' + process.env.PATH;"
        f"const installer = require({json.dumps(str(REPO_ROOT / 'bin' / 'claude-smart.js'))});"
        "process.stdout.write(String(installer.hasExtractionProvider()));"
    )

    result = _run_node_script(script)
    assert result is not None

    assert result.returncode == 0, result.stderr
    assert result.stdout == "true"


def _fake_opencode_path(tmp_path: Path) -> Path:
    fake_opencode = tmp_path / "opencode"
    fake_opencode.write_text("#!/bin/sh\nexit 0\n")
    fake_opencode.chmod(0o755)
    return fake_opencode


def _isolated_installer_env(tmp_path: Path) -> dict[str, str]:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _fake_opencode_path(fake_bin)
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
            "PATH": f"{fake_bin}{os.pathsep}{env.get('PATH', '')}",
        }
    )
    env.pop("REFLEXIO_API_KEY", None)
    env.pop("CLAUDE_SMART_CLI_PATH", None)
    return env


def test_node_opencode_install_from_npx_root_patches_config_without_bootstrap(
    tmp_path: Path,
) -> None:
    node = shutil_which_node()
    if node is None:
        return
    package_root = tmp_path / "_npx" / "abc" / "node_modules" / "claude-smart"
    bin_dir = package_root / "bin"
    bin_dir.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "bin" / "claude-smart.js", bin_dir / "claude-smart.js")
    project = tmp_path / "project"
    project.mkdir()

    result = subprocess.run(
        [node, str(bin_dir / "claude-smart.js"), "install", "--host", "opencode"],
        cwd=project,
        env=_isolated_installer_env(tmp_path),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "npx's temporary package cache" in result.stdout
    assert json.loads((project / "opencode.json").read_text()) == {
        "plugin": ["claude-smart"]
    }


def test_node_opencode_install_stable_root_bootstrap_failure_leaves_config_unchanged(
    tmp_path: Path,
) -> None:
    node = shutil_which_node()
    if node is None:
        return
    package_root = tmp_path / "global" / "node_modules" / "claude-smart"
    bin_dir = package_root / "bin"
    bin_dir.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "bin" / "claude-smart.js", bin_dir / "claude-smart.js")
    project = tmp_path / "project"
    project.mkdir()
    config = project / "opencode.json"
    original = json.dumps({"theme": "system"}) + "\n"
    config.write_text(original)

    result = subprocess.run(
        [node, str(bin_dir / "claude-smart.js"), "install", "--host", "opencode"],
        cwd=project,
        env=_isolated_installer_env(tmp_path),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "dependency bootstrap" in result.stderr
    assert config.read_text() == original


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
    parsed = json.loads((tmp_path / "opencode.json").read_text())
    assert parsed["plugin"] == ["claude-smart"]
    assert "Restart OpenCode" in capsys.readouterr().out


def test_opencode_install_patches_existing_dot_opencode_config(
    monkeypatch, tmp_path
) -> None:
    legacy_config = tmp_path / ".opencode" / "opencode.json"
    legacy_config.parent.mkdir()
    legacy_config.write_text(json.dumps({"plugin": ["other-plugin"]}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_REFLEXIO_ENV_PATH", tmp_path / ".reflexio" / ".env")
    monkeypatch.setattr(
        cli.shutil, "which", lambda name: f"/bin/{name}" if name == "codex" else None
    )
    monkeypatch.setattr(cli, "_bootstrap_opencode_install", lambda _read_only: (True, "/plugin"))

    rc = cli.cmd_install(argparse.Namespace(host="opencode", global_config=False))

    assert rc == 0
    assert not (tmp_path / "opencode.json").exists()
    assert json.loads(legacy_config.read_text())["plugin"] == [
        "other-plugin",
        "claude-smart",
    ]


def test_opencode_install_migrates_existing_plugins_field(
    monkeypatch, tmp_path
) -> None:
    config = tmp_path / "opencode.json"
    config.write_text(json.dumps({"plugins": ["other-plugin"], "theme": "system"}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_REFLEXIO_ENV_PATH", tmp_path / ".reflexio" / ".env")
    monkeypatch.setattr(
        cli.shutil, "which", lambda name: f"/bin/{name}" if name == "codex" else None
    )
    monkeypatch.setattr(cli, "_bootstrap_opencode_install", lambda _read_only: (True, "/plugin"))

    rc = cli.cmd_install(argparse.Namespace(host="opencode", global_config=False))

    parsed = json.loads(config.read_text())
    assert rc == 0
    assert parsed["plugin"] == ["other-plugin", "claude-smart"]
    assert "plugins" not in parsed


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


def test_node_installer_migrates_existing_plugins_field(tmp_path: Path) -> None:
    if shutil_which_node() is None:
        return
    config = tmp_path / "opencode.json"
    config.write_text(json.dumps({"plugins": ["other"], "theme": "system"}))
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
    assert "plugins" not in parsed
    assert parsed["theme"] == "system"


def test_node_installer_uninstall_migrates_existing_plugins_field(tmp_path: Path) -> None:
    if shutil_which_node() is None:
        return
    config = tmp_path / "opencode.json"
    config.write_text(json.dumps({"plugins": ["claude-smart", "other", "claude-smart"]}))
    script = (
        f"const installer = require({json.dumps(str(REPO_ROOT / 'bin' / 'claude-smart.js'))});"
        f"installer.patchOpenCodePluginConfig({json.dumps(str(config))}, {{install: false}});"
        "process.stdout.write(require('fs').readFileSync(process.argv[1], 'utf8'));"
    )

    result = _run_node_script(script, str(config))
    assert result is not None

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"plugin": ["other"]}


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


def test_node_jsonc_parser_skips_comments_after_trailing_commas() -> None:
    if shutil_which_node() is None:
        return
    script = (
        f"const installer = require({json.dumps(str(REPO_ROOT / 'bin' / 'claude-smart.js'))});"
        "process.stdout.write(installer.stripJsonc('{\"plugin\":[\"other\", // note\\n], /* end */}\\n'));"
    )

    result = _run_node_script(script)
    assert result is not None

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"plugin": ["other"]}


def test_node_installer_backs_up_jsonc_config(tmp_path: Path) -> None:
    if shutil_which_node() is None:
        return
    config = tmp_path / "opencode.jsonc"
    original = '{\n  // keep my notes\n  "plugin": ["other"]\n}\n'
    config.write_text(original)
    script = (
        f"const installer = require({json.dumps(str(REPO_ROOT / 'bin' / 'claude-smart.js'))});"
        f"const result = installer.patchOpenCodePluginConfig({json.dumps(str(config))}, {{install: true}});"
        "process.stdout.write(JSON.stringify({ result, text: require('fs').readFileSync(process.argv[1], 'utf8'), backup: require('fs').readFileSync(`${process.argv[1]}.bak`, 'utf8') }));"
    )

    result = _run_node_script(script, str(config))
    assert result is not None

    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed["result"]["backupPath"] == f"{config}.bak"
    assert parsed["backup"] == original
    assert json.loads(parsed["text"])["plugin"] == ["other", "claude-smart"]


def test_node_installer_project_config_path_uses_root_by_default_and_legacy_if_present(
    tmp_path: Path,
) -> None:
    if shutil_which_node() is None:
        return
    script = (
        f"const installer = require({json.dumps(str(REPO_ROOT / 'bin' / 'claude-smart.js'))});"
        "const fs = require('fs');"
        "const path = require('path');"
        "const root = installer.opencodeConfigPath([], process.argv[1]);"
        "fs.mkdirSync(path.join(process.argv[1], '.opencode'));"
        "fs.writeFileSync(path.join(process.argv[1], '.opencode', 'opencode.json'), '{\"plugin\":[]}\\n');"
        "const legacy = installer.opencodeConfigPath([], process.argv[1]);"
        "process.stdout.write(JSON.stringify({ root, legacy }));"
    )

    result = _run_node_script(script, str(tmp_path))
    assert result is not None

    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed == {
        "root": str(tmp_path / "opencode.json"),
        "legacy": str(tmp_path / ".opencode" / "opencode.json"),
    }


def test_node_installer_detects_npx_package_root() -> None:
    if shutil_which_node() is None:
        return
    script = (
        f"const installer = require({json.dumps(str(REPO_ROOT / 'bin' / 'claude-smart.js'))});"
        "process.stdout.write(JSON.stringify({"
        "unix: installer.isNpxPackageRoot('/home/me/.npm/_npx/abc/node_modules/claude-smart'),"
        "win: installer.isNpxPackageRoot('C:\\\\Users\\\\me\\\\AppData\\\\Local\\\\npm-cache\\\\_npx\\\\abc\\\\node_modules\\\\claude-smart'),"
        "global: installer.isNpxPackageRoot('/opt/homebrew/lib/node_modules/claude-smart')"
        "}));"
    )

    result = _run_node_script(script)
    assert result is not None

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"unix": True, "win": True, "global": False}


def test_node_installer_skips_backup_for_plain_json(tmp_path: Path) -> None:
    if shutil_which_node() is None:
        return
    config = tmp_path / "opencode.json"
    config.write_text(json.dumps({"plugin": ["other"]}))
    script = (
        f"const installer = require({json.dumps(str(REPO_ROOT / 'bin' / 'claude-smart.js'))});"
        f"const result = installer.patchOpenCodePluginConfig({json.dumps(str(config))}, {{install: true}});"
        "process.stdout.write(JSON.stringify({ result, backupExists: require('fs').existsSync(`${process.argv[1]}.bak`) }));"
    )

    result = _run_node_script(script, str(config))
    assert result is not None

    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed["result"]["backupPath"] is None
    assert parsed["backupExists"] is False


def test_node_installer_rejects_non_array_plugin_without_rewrite(
    tmp_path: Path,
) -> None:
    if shutil_which_node() is None:
        return
    config = tmp_path / "opencode.json"
    original = json.dumps({"plugin": "other", "theme": "system"})
    config.write_text(original)
    script = (
        f"const installer = require({json.dumps(str(REPO_ROOT / 'bin' / 'claude-smart.js'))});"
        "try {"
        f"  installer.patchOpenCodePluginConfig({json.dumps(str(config))}, {{install: true}});"
        "  process.stdout.write('unexpected success');"
        "  process.exit(2);"
        "} catch (err) {"
        "  process.stdout.write(JSON.stringify({ message: err.message, text: require('fs').readFileSync(process.argv[1], 'utf8') }));"
        "}"
    )

    result = _run_node_script(script, str(config))
    assert result is not None

    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert 'field "plugin" must be a JSON array' in parsed["message"]
    assert parsed["text"] == original


def test_node_opencode_payload_normalizes_tool_contracts() -> None:
    if shutil_which_node() is None:
        return
    script = f"""
      import({json.dumps(str(REPO_ROOT / "plugin" / "opencode" / "dist" / "payload.js"))}).then((payload) => {{
        const edit = payload.toolAfterPayload(
          {{ sessionID: "s1", tool: "edit", args: {{ filePath: "README.md", oldString: "old", newString: "new" }} }},
          {{ output: "edited" }},
          "/repo"
        );
        const patch = payload.toolAfterPayload(
          {{ sessionID: "s1", tool: "apply_patch", args: {{ patchText: "*** Begin Patch" }} }},
          {{ output: "patched" }},
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
    assert parsed["edit"]["tool_response"] == {"output": "edited", "stdout": "edited"}
    assert parsed["patch"]["tool_name"] == "apply_patch"
    assert parsed["patch"]["tool_input"]["command"] == "*** Begin Patch"
    assert parsed["patch"]["tool_response"] == {"output": "patched", "stdout": "patched"}
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
        buffer.update({{ type: "message.part.updated", properties: {{ sessionID: "s1", part: {{ id: "p4", messageID: "m1", type: "text", text: "" }} }} }});
        buffer.update({{ type: "message.part.delta", properties: {{ sessionID: "s1", partID: "p4", delta: "streamed later" }} }});
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
        "beforeClear": "hello world\n\nstreamed later",
        "afterClear": "",
    }


def test_node_opencode_server_injects_cached_context_for_session_ids(
    tmp_path: Path,
) -> None:
    if shutil_which_node() is None:
        return
    fake_bash = tmp_path / "bash"
    fake_bash.write_text(
        "#!/bin/sh\n"
        "cat >/dev/null || true\n"
        "if [ \"${3:-}\" = \"user-prompt\" ]; then\n"
        "  printf '%s\\n' '{\"hookSpecificOutput\":{\"additionalContext\":\"learned context\"}}'\n"
        "else\n"
        "  printf '%s\\n' '{}'\n"
        "fi\n"
    )
    fake_bash.chmod(0o755)
    script = f"""
      process.env.PATH = {json.dumps(str(tmp_path))} + ":" + process.env.PATH;
      import({json.dumps(str(REPO_ROOT / "plugin" / "opencode" / "dist" / "server.mjs"))}).then(async (mod) => {{
        const plugin = await mod.default.server({{ directory: "/repo" }});
        const output1 = {{ system: [] }};
        await plugin["chat.message"]({{ sessionID: "s1" }}, {{ parts: [{{ type: "text", text: "remember" }}] }});
        await plugin["experimental.chat.system.transform"]({{ sessionID: "s1" }}, output1);
        const output2 = {{ system: [] }};
        await plugin["chat.message"]({{ session_id: "s2" }}, {{ message: {{ content: "remember again" }} }});
        await plugin["experimental.chat.system.transform"]({{ session_id: "s2" }}, output2);
        process.stdout.write(JSON.stringify({{ output1, output2 }}));
      }}).catch((err) => {{
        console.error(err);
        process.exit(1);
      }});
    """

    result = _run_node_script(script)
    assert result is not None

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "output1": {"system": ["learned context"]},
        "output2": {"system": ["learned context"]},
    }


def test_node_opencode_server_tolerates_closed_child_stdin(tmp_path: Path) -> None:
    if shutil_which_node() is None:
        return
    fake_bash = tmp_path / "bash"
    fake_bash.write_text("#!/bin/sh\nexit 0\n")
    fake_bash.chmod(0o755)
    script = f"""
      process.env.PATH = {json.dumps(str(tmp_path))} + ":" + process.env.PATH;
      import({json.dumps(str(REPO_ROOT / "plugin" / "opencode" / "dist" / "server.mjs"))}).then(async (mod) => {{
        const plugin = await mod.default.server({{ directory: "/repo" }});
        await plugin["chat.message"]({{ sessionID: "s1" }}, {{ parts: [{{ type: "text", text: "remember" }}] }});
        await new Promise((resolve) => setTimeout(resolve, 100));
        process.stdout.write("ok");
      }}).catch((err) => {{
        console.error(err);
        process.exit(1);
      }});
    """

    result = _run_node_script(script)
    assert result is not None

    assert result.returncode == 0, result.stderr
    assert result.stdout == "ok"


def test_node_opencode_server_dispatches_lifecycle_tool_idle_and_dispose(
    tmp_path: Path,
) -> None:
    if shutil_which_node() is None:
        return
    log_path = tmp_path / "calls.jsonl"
    _write_fake_bash_recorder(tmp_path, user_prompt_context="learned context")
    script = f"""
      process.env.PATH = {json.dumps(str(tmp_path))} + ":" + process.env.PATH;
      process.env.CALL_LOG = {json.dumps(str(log_path))};
      import({json.dumps(str(REPO_ROOT / "plugin" / "opencode" / "dist" / "server.mjs"))}).then(async (mod) => {{
        const plugin = await mod.default.server({{ directory: "/repo" }});
        await plugin.event({{ event: {{ type: "session.created", properties: {{ sessionID: "s-life", info: {{ directory: "/repo" }} }} }} }});
        await plugin["chat.message"]({{ sessionID: "s-life" }}, {{ message: {{ content: "Use remembered rules" }} }});
        const systemOutput = {{ system: [] }};
        await plugin["experimental.chat.system.transform"]({{ sessionID: "s-life" }}, systemOutput);
        await plugin.event({{ event: {{ type: "message.updated", properties: {{ sessionID: "s-life", info: {{ id: "m1", role: "assistant" }} }} }} }});
        await plugin.event({{ event: {{ type: "message.part.updated", properties: {{ sessionID: "s-life", part: {{ id: "p1", messageID: "m1", type: "text", text: "final" }} }} }} }});
        await plugin.event({{ event: {{ type: "message.part.delta", properties: {{ sessionID: "s-life", partID: "p1", delta: " answer" }} }} }});
        await plugin["tool.execute.after"]({{ sessionID: "s-life", tool: "shell", args: {{ cmd: "echo ok" }} }}, {{ output: "ok" }});
        await plugin.event({{ event: {{ type: "session.idle", properties: {{ sessionID: "s-life" }} }} }});
        await plugin.dispose();
        process.stdout.write(JSON.stringify({{ systemOutput, calls: require("fs").readFileSync(process.env.CALL_LOG, "utf8").trim().split("\\n").map(JSON.parse) }}));
      }}).catch((err) => {{
        console.error(err);
        process.exit(1);
      }});
    """

    result = _run_node_script(script)
    assert result is not None

    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed["systemOutput"] == {"system": ["learned context"]}
    command_pairs = [(Path(call["script"]).name, call["args"]) for call in parsed["calls"]]
    assert ("backend-service.sh", ["start"]) in command_pairs
    assert ("dashboard-service.sh", ["start"]) in command_pairs
    assert ("hook_entry.sh", ["opencode", "session-start"]) in command_pairs
    assert ("hook_entry.sh", ["opencode", "user-prompt"]) in command_pairs
    assert ("hook_entry.sh", ["opencode", "post-tool"]) in command_pairs
    assert ("hook_entry.sh", ["opencode", "stop"]) in command_pairs
    assert ("dashboard-service.sh", ["session-end"]) in command_pairs
    assert ("backend-service.sh", ["session-end"]) in command_pairs
    stop_calls = [
        json.loads(call["payload"])
        for call in parsed["calls"]
        if Path(call["script"]).name == "hook_entry.sh" and call["args"] == ["opencode", "stop"]
    ]
    assert stop_calls == [
        {"session_id": "s-life", "cwd": "/repo", "last_assistant_message": "final answer"}
    ]


def test_node_opencode_server_dispose_flushes_active_session_without_idle(
    tmp_path: Path,
) -> None:
    if shutil_which_node() is None:
        return
    log_path = tmp_path / "calls.jsonl"
    _write_fake_bash_recorder(tmp_path)
    script = f"""
      process.env.PATH = {json.dumps(str(tmp_path))} + ":" + process.env.PATH;
      process.env.CALL_LOG = {json.dumps(str(log_path))};
      import({json.dumps(str(REPO_ROOT / "plugin" / "opencode" / "dist" / "server.mjs"))}).then(async (mod) => {{
        const plugin = await mod.default.server({{ directory: "/repo" }});
        await plugin["chat.message"]({{ sessionID: "s-dispose" }}, {{ message: {{ content: "remember this" }} }});
        await plugin.event({{ event: {{ type: "message.updated", properties: {{ sessionID: "s-dispose", info: {{ id: "m1", role: "assistant" }} }} }} }});
        await plugin.event({{ event: {{ type: "message.part.updated", properties: {{ sessionID: "s-dispose", part: {{ id: "p1", messageID: "m1", type: "text", text: "remembered" }} }} }} }});
        await Promise.all([plugin.dispose(), plugin.dispose()]);
        process.stdout.write(require("fs").readFileSync(process.env.CALL_LOG, "utf8"));
      }}).catch((err) => {{
        console.error(err);
        process.exit(1);
      }});
    """

    result = _run_node_script(script)
    assert result is not None

    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in result.stdout.strip().split("\n")]
    stop_calls = [
        json.loads(call["payload"])
        for call in calls
        if Path(call["script"]).name == "hook_entry.sh" and call["args"] == ["opencode", "stop"]
    ]
    assert stop_calls == [
        {"session_id": "s-dispose", "cwd": "/repo", "last_assistant_message": "remembered"}
    ]


def test_node_opencode_server_text_complete_flushes_once(
    tmp_path: Path,
) -> None:
    if shutil_which_node() is None:
        return
    log_path = tmp_path / "calls.jsonl"
    _write_fake_bash_recorder(tmp_path)
    script = f"""
      process.env.PATH = {json.dumps(str(tmp_path))} + ":" + process.env.PATH;
      process.env.CALL_LOG = {json.dumps(str(log_path))};
      import({json.dumps(str(REPO_ROOT / "plugin" / "opencode" / "dist" / "server.mjs"))}).then(async (mod) => {{
        const plugin = await mod.default.server({{ directory: "/repo" }});
        await plugin["chat.message"]({{ sessionID: "s-complete" }}, {{ message: {{ content: "remember this" }} }});
        await plugin["experimental.text.complete"]({{ sessionID: "s-complete" }}, {{ text: "done" }});
        await plugin.event({{ event: {{ type: "session.idle", properties: {{ sessionID: "s-complete" }} }} }});
        await plugin.dispose();
        process.stdout.write(require("fs").readFileSync(process.env.CALL_LOG, "utf8"));
      }}).catch((err) => {{
        console.error(err);
        process.exit(1);
      }});
    """

    result = _run_node_script(script)
    assert result is not None

    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in result.stdout.strip().split("\n")]
    stop_calls = [
        json.loads(call["payload"])
        for call in calls
        if Path(call["script"]).name == "hook_entry.sh" and call["args"] == ["opencode", "stop"]
    ]
    assert stop_calls == [
        {"session_id": "s-complete", "cwd": "/repo", "last_assistant_message": "done"}
    ]


def test_node_opencode_server_keeps_multi_segment_assistant_text(
    tmp_path: Path,
) -> None:
    if shutil_which_node() is None:
        return
    log_path = tmp_path / "calls.jsonl"
    _write_fake_bash_recorder(tmp_path)
    script = f"""
      process.env.PATH = {json.dumps(str(tmp_path))} + ":" + process.env.PATH;
      process.env.CALL_LOG = {json.dumps(str(log_path))};
      import({json.dumps(str(REPO_ROOT / "plugin" / "opencode" / "dist" / "server.mjs"))}).then(async (mod) => {{
        const plugin = await mod.default.server({{ directory: "/repo" }});
        await plugin["chat.message"]({{ sessionID: "s-multi" }}, {{ message: {{ content: "remember this" }} }});
        await plugin.event({{ event: {{ type: "message.updated", properties: {{ sessionID: "s-multi", info: {{ id: "m1", role: "assistant" }} }} }} }});
        await plugin.event({{ event: {{ type: "message.part.updated", properties: {{ sessionID: "s-multi", part: {{ id: "p1", messageID: "m1", type: "text", text: "first" }} }} }} }});
        await plugin["experimental.text.complete"]({{ sessionID: "s-multi" }}, {{ text: "first" }});
        await plugin.event({{ event: {{ type: "message.part.updated", properties: {{ sessionID: "s-multi", part: {{ id: "p2", messageID: "m1", type: "text", text: "second" }} }} }} }});
        await plugin["experimental.text.complete"]({{ sessionID: "s-multi" }}, {{ text: "second" }});
        await plugin.event({{ event: {{ type: "session.idle", properties: {{ sessionID: "s-multi" }} }} }});
        await plugin.dispose();
        process.stdout.write(require("fs").readFileSync(process.env.CALL_LOG, "utf8"));
      }}).catch((err) => {{
        console.error(err);
        process.exit(1);
      }});
    """

    result = _run_node_script(script)
    assert result is not None

    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in result.stdout.strip().split("\n")]
    stop_calls = [
        json.loads(call["payload"])
        for call in calls
        if Path(call["script"]).name == "hook_entry.sh" and call["args"] == ["opencode", "stop"]
    ]
    assert stop_calls == [
        {"session_id": "s-multi", "cwd": "/repo", "last_assistant_message": "first\n\nsecond"}
    ]


def test_opencode_dist_files_are_packaged() -> None:
    package = _read_json("package.json")

    assert "plugin" in package["files"]
    assert package["exports"]["./server"]["import"] == "./plugin/opencode/dist/server.mjs"


def test_opencode_dist_matches_typescript_sources(tmp_path: Path) -> None:
    import filecmp
    import shutil

    if shutil.which("npx") is None:
        return
    out_dir = tmp_path / "dist"
    result = subprocess.run(
        [
            "npx",
            "tsc",
            "-p",
            str(REPO_ROOT / "plugin" / "opencode" / "tsconfig.json"),
            "--outDir",
            str(out_dir),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    for filename in ("assistant-buffer.js", "internal.js", "payload.js", "server.mjs"):
        expected = REPO_ROOT / "plugin" / "opencode" / "dist" / filename
        generated = out_dir / filename
        assert filecmp.cmp(expected, generated, shallow=False), filename


def test_node_parse_host_accepts_opencode() -> None:
    installer = (REPO_ROOT / "bin" / "claude-smart.js").read_text()

    assert 'value !== "claude-code" && value !== "codex" && value !== "opencode"' in installer
    assert "runInstallOpenCode" in installer
    assert "runUninstallOpenCode" in installer


def shutil_which_node() -> str | None:
    import shutil

    return shutil.which("node")
