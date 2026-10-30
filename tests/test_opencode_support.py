"""Tests for OpenCode host support."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import textwrap
from collections.abc import Generator
from pathlib import Path, PosixPath
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


def _run_node_script(
    script: str, *args: str, timeout: float | None = 120
) -> subprocess.CompletedProcess[str] | None:
    node = shutil_which_node()
    if node is None:
        return None
    return subprocess.run(
        [node, "-e", script, *args],
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
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


def _write_minimal_plugin_root(root: Path, *, name: str = "claude-smart") -> None:
    (root / "scripts").mkdir(parents=True)
    (root / "pyproject.toml").write_text(f"[project]\nname = \"{name}\"\n")
    (root / "uv.lock").write_text("")
    (root / "scripts" / "hook_entry.sh").write_text("#!/bin/sh\n")


def _copy_opencode_dist(root: Path) -> Path:
    dist = root / "opencode" / "dist"
    shutil.copytree(REPO_ROOT / "plugin" / "opencode" / "dist", dist)
    return dist / "server.mjs"


def _run_opencode_server_and_read_calls(
    *,
    tmp_path: Path,
    import_path: Path,
    home: Path,
    extra_env: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    log_path = tmp_path / "calls.jsonl"
    _write_fake_bash_recorder(tmp_path)
    env_lines = ""
    for key, value in (extra_env or {}).items():
        env_lines += f"process.env[{json.dumps(key)}] = {json.dumps(value)};\n"
    script = f"""
      process.env.PATH = {json.dumps(str(tmp_path))} + ":" + process.env.PATH;
      process.env.CALL_LOG = {json.dumps(str(log_path))};
      process.env.HOME = {json.dumps(str(home))};
      delete process.env.CLAUDE_SMART_PLUGIN_ROOT;
      {env_lines}
      import({json.dumps(str(import_path))}).then(async (mod) => {{
        const plugin = await mod.default.server({{ directory: "/repo" }});
        await plugin["chat.message"]({{ sessionID: "s1" }}, {{ message: {{ content: "remember" }} }});
        process.stdout.write(require("fs").readFileSync(process.env.CALL_LOG, "utf8"));
      }}).catch((err) => {{
        console.error(err);
        process.exit(1);
      }});
    """

    result = _run_node_script(script)
    assert result is not None

    assert result.returncode == 0, result.stderr
    return [json.loads(line) for line in result.stdout.strip().split("\n")]


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


def test_opencode_hook_bootstrap_lockfile_matches_pyproject() -> None:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not installed")
    assert uv is not None

    result = subprocess.run(
        [
            uv,
            "lock",
            "--check",
            "--python",
            "3.12",
            "--project",
            str(REPO_ROOT / "plugin"),
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_package_target_checks_standalone_lock_freshness() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text()

    assert "check-standalone-lock:" in makefile
    assert "bash scripts/standalone-lock.sh --check" in makefile
    assert "package: check-locked-project-version check-standalone-lock" in makefile


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


def test_runtime_keeps_unknown_attribution_separate_from_compatibility_host() -> None:
    assert runtime.set_host("unsupported-host") == runtime.HOST_CLAUDE_CODE
    assert runtime.host() == runtime.HOST_CLAUDE_CODE
    assert runtime.attribution_host() == runtime.HOST_UNKNOWN

    assert runtime.set_host(None) == runtime.HOST_CLAUDE_CODE
    assert runtime.host() == runtime.HOST_CLAUDE_CODE
    assert runtime.attribution_host() == runtime.HOST_UNKNOWN


def test_runtime_reports_explicit_host_for_attribution() -> None:
    runtime.set_host(runtime.HOST_CODEX)
    assert runtime.attribution_host() == runtime.HOST_CODEX


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
        # The Stop drain deadline is a monotonic instant; its bound is
        # covered by test_stop_bounds_its_synchronous_drain_inside_the_hook_limit.
        lambda deadline, **kw: published.append(kw) or ("ok", 3),
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
            "top_k": 5,
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
    plugin_spec = cli._opencode_local_plugin_spec()
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
    assert parsed["plugin"] == ["other-plugin", plugin_spec]


def test_opencode_config_patch_uninstall_removes_only_claude_smart(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "local-package"
    package_root.mkdir()
    (package_root / "package.json").write_text('{"name":"claude-smart"}\n')
    config = tmp_path / ".opencode" / "opencode.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps(
            {
                "plugin": [
                    "other-plugin",
                    "claude-smart",
                    package_root.as_uri(),
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
    plugin_spec = cli._opencode_local_plugin_spec()
    config = tmp_path / ".opencode" / "opencode.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps({"plugin": ["claude-smart", "other-plugin", "claude-smart"]})
    )

    changed, _ = cli._patch_opencode_plugin_config(config, install=True)

    assert changed is True
    assert json.loads(config.read_text())["plugin"] == ["other-plugin", plugin_spec]


def test_opencode_config_patch_installs_local_file_spec_and_removes_bare_name(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "local-package"
    package_root.mkdir()
    old_package = tmp_path / "old-cache" / "node_modules" / "claude-smart"
    old_package.mkdir(parents=True)
    (old_package / "package.json").write_text('{"name":"claude-smart"}\n')
    config = tmp_path / "opencode.json"
    config.write_text(
        json.dumps(
            {
                "plugin": [
                    "other-plugin",
                    "claude-smart",
                    old_package.as_uri(),
                ]
            }
        )
    )

    changed, _ = cli._patch_opencode_plugin_config(
        config,
        install=True,
        plugin_spec=package_root.as_uri(),
    )

    assert changed is True
    assert json.loads(config.read_text())["plugin"] == [
        "other-plugin",
        package_root.as_uri(),
    ]


def test_opencode_config_patch_preserves_unrelated_file_specs_named_claude_smart(
    tmp_path: Path,
) -> None:
    missing_manifest = tmp_path / "vendor" / "claude-smart"
    wrong_manifest = tmp_path / "other" / "claude-smart"
    real_package = tmp_path / "real-package"
    missing_manifest.mkdir(parents=True)
    wrong_manifest.mkdir(parents=True)
    real_package.mkdir()
    (wrong_manifest / "package.json").write_text('{"name":"not-claude-smart"}\n')
    (real_package / "package.json").write_text('{"name":"claude-smart"}\n')
    config = tmp_path / "opencode.json"
    config.write_text(
        json.dumps(
            {
                "plugin": [
                    "other-plugin",
                    missing_manifest.as_uri(),
                    wrong_manifest.as_uri(),
                    real_package.as_uri(),
                    "claude-smart@0.2.46",
                ]
            }
        )
    )

    changed, _ = cli._patch_opencode_plugin_config(config, install=False)

    assert changed is True
    assert json.loads(config.read_text())["plugin"] == [
        "other-plugin",
        missing_manifest.as_uri(),
        wrong_manifest.as_uri(),
    ]


def test_opencode_config_patch_fresh_config_uses_current_plugin_field(
    tmp_path: Path,
) -> None:
    plugin_spec = cli._opencode_local_plugin_spec()
    config = tmp_path / "opencode.json"

    changed, _ = cli._patch_opencode_plugin_config(config, install=True)

    assert changed is True
    assert json.loads(config.read_text()) == {"plugin": [plugin_spec]}


def test_opencode_config_patch_migrates_existing_plugins_field(
    tmp_path: Path,
) -> None:
    plugin_spec = cli._opencode_local_plugin_spec()
    config = tmp_path / "opencode.json"
    config.write_text(json.dumps({"plugins": ["other-plugin"], "theme": "system"}))

    changed, _ = cli._patch_opencode_plugin_config(config, install=True)

    parsed = json.loads(config.read_text())
    assert changed is True
    assert parsed["plugin"] == ["other-plugin", plugin_spec]
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
    plugin_spec = cli._opencode_local_plugin_spec()
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
    assert json.loads(config.read_text())["plugin"] == ["other-plugin", plugin_spec]


def test_opencode_config_patch_skips_backup_for_plain_json(tmp_path: Path) -> None:
    config = tmp_path / ".opencode" / "opencode.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"plugin": ["other-plugin"]}))

    changed, _ = cli._patch_opencode_plugin_config(config, install=True)

    assert changed is True
    assert not config.with_suffix(config.suffix + ".bak").exists()


def test_opencode_scope_config_paths_cover_every_config_the_scope_loads(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    assert cli._opencode_scope_config_paths(global_config=True) == [
        tmp_path / "xdg" / "opencode" / "opencode.json",
        tmp_path / "xdg" / "opencode" / "opencode.jsonc",
    ]
    assert cli._opencode_scope_config_paths(cwd=tmp_path) == [
        tmp_path / "opencode.json",
        tmp_path / "opencode.jsonc",
        tmp_path / ".opencode" / "opencode.json",
        tmp_path / ".opencode" / "opencode.jsonc",
    ]


def _stub_opencode_install(monkeypatch, package_root: Path) -> None:
    monkeypatch.setattr(cli, "_OPENCODE_LOCAL_PACKAGE_DIR", package_root)
    monkeypatch.setattr(
        cli, "_opencode_install_supported_from_this_package", lambda: True
    )
    monkeypatch.setattr(cli, "_opencode_prerequisite_error", lambda: None)
    monkeypatch.setattr(cli, "_configure_reflexio_setup", lambda host: False)
    monkeypatch.setattr(cli, "_persist_opencode_path", lambda: [])
    monkeypatch.setattr(cli, "_has_extraction_provider", lambda: True)
    monkeypatch.setattr(
        cli, "_bootstrap_opencode_install", lambda read_only: (True, str(package_root))
    )


def test_opencode_python_install_removes_legacy_entries_from_every_scope_config(
    monkeypatch, tmp_path, capsys
) -> None:
    package_root = tmp_path / "pkg"
    (package_root / "plugin").mkdir(parents=True)
    _stub_opencode_install(monkeypatch, package_root)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    project = tmp_path / "project"
    (project / ".opencode").mkdir(parents=True)
    legacy = {"plugin": ["other", package_root.as_uri()]}
    configs = [
        project / "opencode.json",
        project / "opencode.jsonc",
        project / ".opencode" / "opencode.json",
    ]
    for config in configs:
        config.write_text(json.dumps(legacy) + "\n")
    monkeypatch.chdir(project)

    rc = cli.cmd_install(argparse.Namespace(host="opencode", global_config=False))

    assert rc == 0
    # OpenCode merges every one of these; an entry left in any is a second
    # load spec beside the loader ("Duplicate plugin ID" on V2).
    for config in configs:
        assert json.loads(config.read_text())["plugin"] == ["other"], config
    out = capsys.readouterr().out
    for config in configs:
        assert str(config) in out


def test_opencode_python_legacy_removal_validates_every_config_before_writing(
    tmp_path: Path,
) -> None:
    package_root_uri = (tmp_path / "pkg").as_uri()
    good = tmp_path / "opencode.json"
    good.write_text(json.dumps({"plugin": ["claude-smart", package_root_uri]}) + "\n")
    original = good.read_text()
    (tmp_path / "opencode.jsonc").write_text('{"plugin": "not-a-list"}\n')

    with pytest.raises(ValueError, match="must be a JSON array"):
        cli._remove_opencode_legacy_entries(cwd=tmp_path)

    # No partial edit: the valid config is untouched when another one fails.
    assert good.read_text() == original


def test_opencode_python_uninstall_removes_legacy_entries_from_every_scope_config(
    monkeypatch, tmp_path
) -> None:
    package_root = tmp_path / "state" / "opencode" / "claude-smart"
    (package_root / "plugin").mkdir(parents=True)
    monkeypatch.setattr(cli, "_OPENCODE_LOCAL_PACKAGE_DIR", package_root)
    monkeypatch.setattr(cli, "_run_service", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_repair_plugin_root", lambda host: None)
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    (xdg / "opencode").mkdir(parents=True)
    for name in ("opencode.json", "opencode.jsonc"):
        (xdg / "opencode" / name).write_text(
            json.dumps({"plugin": ["claude-smart@1.0.0"]}) + "\n"
        )
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)

    rc = cli.cmd_uninstall(argparse.Namespace(host="opencode", global_config=True))

    assert rc == 0
    for name in ("opencode.json", "opencode.jsonc"):
        assert json.loads((xdg / "opencode" / name).read_text())["plugin"] == []


@pytest.mark.parametrize("occupant", ["non_utf8_file", "directory"])
def test_opencode_python_install_reports_unusable_loader_path_as_conflict(
    monkeypatch, tmp_path, capsys, occupant: str
) -> None:
    package_root = tmp_path / "pkg"
    (package_root / "plugin").mkdir(parents=True)
    _stub_opencode_install(monkeypatch, package_root)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    project = tmp_path / "project"
    loader = project / ".opencode" / "plugins" / "claude-smart.js"
    loader.parent.mkdir(parents=True)
    config = project / "opencode.json"
    config.write_text(json.dumps({"plugin": ["claude-smart"]}) + "\n")
    if occupant == "directory":
        loader.mkdir()
    else:
        # Invalid UTF-8 (and invalid in many locales): must be reported, not crash.
        loader.write_bytes(b"// hand-written\nexport default \xff\xfe\x80;\n")
    original = loader.read_bytes() if loader.is_file() else None
    monkeypatch.chdir(project)

    rc = cli.cmd_install(argparse.Namespace(host="opencode", global_config=False))

    assert rc == 1
    err = capsys.readouterr().err
    assert f"left an existing OpenCode plugin in place at {loader}" in err
    if occupant == "directory":
        assert "cannot be read as a file" in err
        assert loader.is_dir()
    else:
        assert "not a claude-smart loader" in err
        assert loader.read_bytes() == original
    # Found before any config change.
    assert json.loads(config.read_text())["plugin"] == ["claude-smart"]


def test_opencode_python_loader_write_and_remove_spare_unusable_paths(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "pkg"
    project = tmp_path / "project"
    loader = project / ".opencode" / "plugins" / "claude-smart.js"
    loader.mkdir(parents=True)

    assert cli._write_opencode_v2_plugin(package_root, cwd=project) == (
        loader,
        "conflict",
    )
    assert cli._remove_opencode_v2_plugin(cwd=project) is False
    assert loader.is_dir()

    loader.rmdir()
    loader.write_bytes(b"\xff\xfe foreign\n")
    assert cli._write_opencode_v2_plugin(package_root, cwd=project)[1] == "conflict"
    assert cli._remove_opencode_v2_plugin(cwd=project) is False
    assert loader.read_bytes() == b"\xff\xfe foreign\n"


def test_opencode_python_install_writes_loader_and_removes_legacy_entry(
    monkeypatch, tmp_path
) -> None:
    package_root = tmp_path / "pkg"
    (package_root / "plugin").mkdir(parents=True)
    monkeypatch.setattr(cli, "_OPENCODE_LOCAL_PACKAGE_DIR", package_root)
    monkeypatch.setattr(cli, "_opencode_install_supported_from_this_package", lambda: True)
    monkeypatch.setattr(cli, "_opencode_prerequisite_error", lambda: None)
    monkeypatch.setattr(cli, "_configure_reflexio_setup", lambda host: False)
    monkeypatch.setattr(cli, "_persist_opencode_path", lambda: [])
    monkeypatch.setattr(cli, "_has_extraction_provider", lambda: True)
    monkeypatch.setattr(
        cli, "_bootstrap_opencode_install", lambda read_only: (True, str(package_root))
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    project = tmp_path / "project"
    project.mkdir()
    # A legacy install left a config entry behind; install must migrate to the
    # loader as the single load path.
    (project / "opencode.json").write_text(
        json.dumps({"plugin": [package_root.as_uri()]}) + "\n"
    )
    monkeypatch.chdir(project)

    rc = cli.cmd_install(argparse.Namespace(host="opencode", global_config=False))

    assert rc == 0
    loader = project / ".opencode" / "plugins" / "claude-smart.js"
    assert loader.read_text() == cli._opencode_v2_plugin_body(package_root)
    assert json.loads((project / "opencode.json").read_text())["plugin"] == []

    # A fresh project gets the loader and no config file at all.
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    monkeypatch.chdir(fresh)
    rc = cli.cmd_install(argparse.Namespace(host="opencode", global_config=False))
    assert rc == 0
    assert not (fresh / "opencode.json").exists()
    assert (fresh / ".opencode" / "plugins" / "claude-smart.js").is_file()


def test_opencode_python_uninstall_removes_loader_and_keeps_shared_package_per_scope(
    monkeypatch, tmp_path
) -> None:
    package_root = tmp_path / "state" / "opencode" / "claude-smart"
    (package_root / "plugin").mkdir(parents=True)
    (package_root / "plugin" / "keep.txt").write_text("keep\n")
    monkeypatch.setattr(cli, "_OPENCODE_LOCAL_PACKAGE_DIR", package_root)
    monkeypatch.setattr(cli, "_run_service", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_repair_plugin_root", lambda host: None)
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    cli._write_opencode_v2_plugin(package_root, global_config=False)
    cli._write_opencode_v2_plugin(package_root, global_config=True)
    # The global config references the package in the object form.
    global_config = xdg / "opencode" / "opencode.json"
    global_config.parent.mkdir(parents=True, exist_ok=True)
    global_config.write_text(
        json.dumps({"plugin": [{"package": package_root.as_uri()}]}) + "\n"
    )

    rc = cli.cmd_uninstall(argparse.Namespace(host="opencode", global_config=False))
    assert rc == 0
    # The project loader is gone; the global install keeps working, so the
    # shared package stays.
    assert not (project / ".opencode" / "plugins" / "claude-smart.js").exists()
    assert (xdg / "opencode" / "plugins" / "claude-smart.js").is_file()
    assert (package_root / "plugin" / "keep.txt").read_text() == "keep\n"

    # Mirror direction: with the project loader back, a --global uninstall must
    # keep the package the project loader still imports.
    cli._write_opencode_v2_plugin(package_root, global_config=False)
    rc = cli.cmd_uninstall(argparse.Namespace(host="opencode", global_config=True))
    assert rc == 0
    # The object-form entry is recognized and removed with the global loader.
    assert json.loads(global_config.read_text())["plugin"] == []
    assert not (xdg / "opencode" / "plugins" / "claude-smart.js").exists()
    assert (package_root / "plugin" / "keep.txt").read_text() == "keep\n"

    # Nothing references the package any more: it goes.
    rc = cli.cmd_uninstall(argparse.Namespace(host="opencode", global_config=False))
    assert rc == 0
    assert not (project / ".opencode" / "plugins" / "claude-smart.js").exists()
    assert not package_root.exists()


def test_opencode_python_loader_body_matches_node_loader(tmp_path: Path) -> None:
    if shutil_which_node() is None:
        pytest.skip("node is not installed")
    package_root = tmp_path / "pkg"
    (package_root / "plugin" / "opencode" / "dist").mkdir(parents=True)
    script = f"""
      const installer = require({json.dumps(str(REPO_ROOT / "bin" / "claude-smart.js"))});
      const result = installer.writeOpenCodeV2Plugin([], {json.dumps(str(package_root))}, {json.dumps(str(tmp_path / "proj"))});
      process.stdout.write(require("fs").readFileSync(result.path, "utf8"));
    """

    result = _run_node_script(script)
    assert result is not None

    assert result.returncode == 0, result.stderr
    # Both installers must write (and recognize) the same loader bytes, or
    # whichever runs second would treat the other's loader as a conflict.
    assert cli._opencode_v2_plugin_body(package_root) == result.stdout


def test_opencode_install_from_python_wheel_path_points_to_npm(
    monkeypatch, tmp_path, capsys
) -> None:
    monkeypatch.setattr(cli, "_SCRIPTS_DIR", tmp_path)

    rc = cli.cmd_install(argparse.Namespace(host="opencode", global_config=False))

    assert rc == 1
    assert "npx claude-smart install --host opencode" in capsys.readouterr().err


def test_opencode_python_install_conflict_fails_loudly(
    monkeypatch, tmp_path, capsys
) -> None:
    package_root = tmp_path / "state" / "opencode" / "claude-smart"
    (package_root / "plugin").mkdir(parents=True)
    loader_path = tmp_path / "project" / ".opencode" / "plugins" / "claude-smart.js"
    monkeypatch.setattr(cli, "_opencode_install_supported_from_this_package", lambda: True)
    monkeypatch.setattr(cli, "_opencode_prerequisite_error", lambda: None)
    monkeypatch.setattr(cli, "_configure_reflexio_setup", lambda host: False)
    monkeypatch.setattr(cli, "_persist_opencode_path", lambda: None)
    monkeypatch.setattr(cli, "_has_extraction_provider", lambda: True)
    monkeypatch.setattr(
        cli, "_bootstrap_opencode_install", lambda read_only: (True, str(package_root))
    )
    patched: list[bool] = []
    monkeypatch.setattr(
        cli,
        "_remove_opencode_legacy_entries",
        lambda global_config: (patched.append(global_config), [])[1],
    )
    monkeypatch.setattr(
        cli,
        "_opencode_v2_loader_conflicts",
        lambda global_config: (loader_path, "It is not a claude-smart loader"),
    )

    rc = cli.cmd_install_opencode(argparse.Namespace(global_config=False))

    # The loader is the only load path: a conflict must fail, not report an
    # installed host.
    assert rc == 1
    captured = capsys.readouterr()
    assert "not a claude-smart loader" in captured.err
    assert "Wrote the OpenCode loader" not in captured.out
    assert "Restart OpenCode" not in captured.out
    # The conflict is found before the config is touched, so nothing needs restoring.
    assert patched == []

    # The same flow with a written loader still succeeds.
    monkeypatch.setattr(cli, "_opencode_v2_loader_conflicts", lambda global_config: None)
    monkeypatch.setattr(
        cli,
        "_write_opencode_v2_plugin",
        lambda package_root, global_config: (loader_path, "written"),
    )
    rc = cli.cmd_install_opencode(argparse.Namespace(global_config=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Wrote the OpenCode loader" in out
    assert "Restart OpenCode" in out


def test_opencode_bootstrap_guard_rejects_unsupported_package(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(cli, "_SCRIPTS_DIR", tmp_path)

    bootstrapped, message = cli._bootstrap_opencode_install(read_only=False)

    assert bootstrapped is False
    assert "npx claude-smart install --host opencode" in message


def test_opencode_install_fails_without_extraction_provider(monkeypatch, tmp_path) -> None:
    env_path = tmp_path / ".claude-smart" / ".env"
    monkeypatch.setattr(cli, "_CLAUDE_SMART_ENV_PATH", env_path)
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)

    rc = cli.cmd_install(argparse.Namespace(host="opencode", global_config=False))

    assert rc == 1


def test_opencode_install_requires_opencode_cli_even_with_other_provider(
    monkeypatch, tmp_path, capsys
) -> None:
    env_path = tmp_path / ".claude-smart" / ".env"
    monkeypatch.setattr(cli, "_CLAUDE_SMART_ENV_PATH", env_path)
    monkeypatch.setenv(cli.env_config.REFLEXIO_API_KEY_ENV, "rk-test")
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/bin/claude" if name == "claude" else None)

    rc = cli.cmd_install(argparse.Namespace(host="opencode", global_config=False))

    assert rc == 1
    assert "OpenCode CLI not found on PATH" in capsys.readouterr().err


def test_opencode_install_requires_git_bash_on_windows(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(cli, "_CLAUDE_SMART_ENV_PATH", tmp_path / ".claude-smart" / ".env")
    monkeypatch.setattr(cli, "_has_opencode_cli", lambda: True)
    monkeypatch.setattr(cli.os, "name", "nt")
    monkeypatch.setattr(cli, "_resolve_bash", lambda: None)

    rc = cli.cmd_install(argparse.Namespace(host="opencode", global_config=False))

    assert rc == 1
    assert "Git Bash is required for claude-smart OpenCode support on Windows" in capsys.readouterr().err


def test_opencode_prerequisite_accepts_git_bash_on_windows(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_has_opencode_cli", lambda: True)
    monkeypatch.setattr(cli.os, "name", "nt")
    monkeypatch.setattr(cli, "_resolve_bash", lambda: r"C:\Program Files\Git\bin\bash.exe")

    assert cli._opencode_prerequisite_error() is None


def test_opencode_extraction_provider_requires_executable_cli_path(
    monkeypatch, tmp_path
) -> None:
    cli_path = tmp_path / "claude-smart-provider"
    cli_path.write_text("#!/bin/sh\nexit 0\n")
    cli_path.chmod(0o644)
    monkeypatch.setattr(cli, "_CLAUDE_SMART_ENV_PATH", tmp_path / ".claude-smart" / ".env")
    monkeypatch.setenv("CLAUDE_SMART_CLI_PATH", str(cli_path))
    monkeypatch.delenv(cli.env_config.REFLEXIO_API_KEY_ENV, raising=False)
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)

    assert cli._has_extraction_provider() is False

    cli_path.chmod(0o755)
    assert cli._has_extraction_provider() is True


def test_opencode_extraction_provider_accepts_opencode_only(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(cli, "_CLAUDE_SMART_ENV_PATH", tmp_path / ".claude-smart" / ".env")
    monkeypatch.delenv("CLAUDE_SMART_CLI_PATH", raising=False)
    monkeypatch.delenv(cli._OPENCODE_PATH_ENV, raising=False)
    monkeypatch.delenv(cli.env_config.REFLEXIO_API_KEY_ENV, raising=False)
    monkeypatch.setattr(
        cli.shutil, "which", lambda name: f"/bin/{name}" if name == "opencode" else None
    )

    assert cli._has_extraction_provider() is True


def test_opencode_extraction_provider_accepts_explicit_opencode_path(
    monkeypatch, tmp_path
) -> None:
    opencode = tmp_path / "opencode"
    opencode.write_text("#!/bin/sh\nexit 0\n")
    opencode.chmod(0o755)
    monkeypatch.setattr(cli, "_CLAUDE_SMART_ENV_PATH", tmp_path / ".claude-smart" / ".env")
    monkeypatch.delenv("CLAUDE_SMART_CLI_PATH", raising=False)
    monkeypatch.delenv(cli.env_config.REFLEXIO_API_KEY_ENV, raising=False)
    monkeypatch.setenv(cli._OPENCODE_PATH_ENV, str(opencode))
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)

    assert cli._has_extraction_provider() is True


def test_opencode_backend_service_uses_bridge_without_opencode_path_probe() -> None:
    service = (REPO_ROOT / "plugin" / "scripts" / "backend-service.sh").read_text()

    assert 'if [ "$learning_provider" = "opencode" ]' in service
    assert 'CLAUDE_SMART_CLI_PATH="$(claude_smart_opencode_compat_path "$PLUGIN_ROOT")"' in service
    assert "command -v opencode" not in service
    assert 'CLAUDE_SMART_CLI_PATH="$PLUGIN_ROOT/scripts/codex-claude-compat"' in service


def test_opencode_server_resolves_windows_bash_before_spawning() -> None:
    server = (REPO_ROOT / "plugin" / "opencode" / "server.mts").read_text()

    assert "function commandPath(names: string[]): string | undefined" in server
    assert "WINDOWS_SYSTEM_BASH_SUFFIXES" in server
    assert "isWindowsSystemBash" in server
    assert '"C:\\\\Program Files\\\\Git\\\\bin\\\\bash.exe"' in server
    assert 'pathCommandCandidates(["bash.exe", "bash"])' in server
    assert "const RESOLVED_BASH = bashPath()" in server
    assert 'spawn(RESOLVED_BASH || "bash"' in server


def test_opencode_cli_bridge_translates_claude_contract(tmp_path: Path) -> None:
    if shutil_which_node() is None:
        pytest.skip("node is not installed")
    bridge = REPO_ROOT / "plugin" / "scripts" / "opencode-claude-compat.js"
    fake_opencode = tmp_path / "opencode"
    call_log = tmp_path / "calls.json"
    fake_opencode.write_text(
        f"""#!/usr/bin/env node
const fs = require("fs");
const path = require("path");
const args = process.argv.slice(2);
const dirIndex = args.indexOf("--dir");
const workDir = dirIndex >= 0 ? args[dirIndex + 1] : "";
const dotConfig = path.join(workDir, ".opencode", "opencode.json");
const rootConfig = path.join(workDir, "opencode.json");
fs.writeFileSync(
  {json.dumps(str(call_log))},
  JSON.stringify({{
    args,
    cwd: process.cwd(),
    dotConfigExists: fs.existsSync(dotConfig),
    rootConfigExists: fs.existsSync(rootConfig),
    stdin: fs.readFileSync(0, "utf8"),
    env: {{
      CLAUDE_SMART_HOST: process.env.CLAUDE_SMART_HOST,
      CLAUDE_SMART_INTERNAL: process.env.CLAUDE_SMART_INTERNAL,
      OPENCODE_PURE: process.env.OPENCODE_PURE,
      PWD: process.env.PWD
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
    env.pop("CLAUDE_SMART_OPENCODE_PATH", None)

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
    assert call["env"]["CLAUDE_SMART_HOST"] == "opencode"
    assert call["env"]["CLAUDE_SMART_INTERNAL"] == "1"
    # Pure mode is set via env (not the rejected --pure flag) so the extractor
    # subprocess skips the user's external plugins on hosts that honor it.
    assert call["env"]["OPENCODE_PURE"] == "1"
    # No `--pure` / `--dir`: `opencode run` rejects both. Isolation comes from
    # cwd (which holds the temp .opencode/opencode.json defining AGENT_NAME),
    # and $PWD is refreshed so opencode resolves the project root correctly.
    assert call["args"][:3] == ["run", "--format", "json"]
    assert "--agent" in call["args"]
    assert "--pure" not in call["args"]
    assert "--dir" not in call["args"]
    assert call["env"]["PWD"] == call["cwd"]
    assert call["dotConfigExists"] is True
    assert call["rootConfigExists"] is False
    assert "--model" not in call["args"]
    assert "system text\n\n## Task\nuser prompt" not in call["args"]
    assert call["stdin"] == "system text\n\n## Task\nuser prompt"


def test_opencode_cli_bridge_strips_outer_json_markdown_fence(tmp_path: Path) -> None:
    if shutil_which_node() is None:
        pytest.skip("node is not installed")
    bridge = REPO_ROOT / "plugin" / "scripts" / "opencode-claude-compat.js"
    fake_opencode = tmp_path / "opencode"
    fake_opencode.write_text(
        """#!/usr/bin/env node
process.stdout.write(JSON.stringify({
  type: "text",
  part: { type: "text", text: "```json\\n{\\\"playbooks\\\":[]}\\n```" }
}) + "\\n");
"""
    )
    fake_opencode.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    env.pop("CLAUDE_SMART_OPENCODE_PATH", None)

    result = subprocess.run(
        [
            shutil_which_node() or "node",
            str(bridge),
            "-p",
            "--output-format",
            "stream-json",
        ],
        input="return json",
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "type": "result",
        "subtype": "success",
        "result": '{"playbooks":[]}',
    }


def test_opencode_cli_bridge_pipes_large_prompt_via_stdin(tmp_path: Path) -> None:
    if shutil_which_node() is None:
        pytest.skip("node is not installed")
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
    env.pop("CLAUDE_SMART_OPENCODE_PATH", None)
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


def test_opencode_cli_bridge_failure_surfaces_error_event(tmp_path: Path) -> None:
    if shutil_which_node() is None:
        pytest.skip("node is not installed")
    bridge = REPO_ROOT / "plugin" / "scripts" / "opencode-claude-compat.js"
    fake_opencode = tmp_path / "opencode"
    # >300 chars of events precede the error event, so the truncated-stdout
    # fallback cannot carry its message.
    padding = json.dumps({"type": "step_start", "noise": "n" * 400})

    def run_bridge() -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}",
        }
        env.pop("CLAUDE_SMART_OPENCODE_PATH", None)
        return subprocess.run(
            [
                shutil_which_node() or "node",
                str(bridge),
                "-p",
                "--output-format",
                "stream-json",
            ],
            input="extract",
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    # 2.x shape: a warning-only stderr must not suppress the error event.
    fake_opencode.write_text(
        "#!/usr/bin/env node\n"
        "process.stderr.write('warning: benign\\n');\n"
        f"process.stdout.write({json.dumps(padding)} + '\\n');\n"
        "process.stdout.write(JSON.stringify({ type: 'error',"
        " error: { type: 'unknown', message: 'Model not found' } }) + '\\n');\n"
        "process.exit(1);\n"
    )
    fake_opencode.chmod(0o755)
    result = run_bridge()
    assert result.returncode == 1
    assert "Model not found" in result.stderr
    assert "warning: benign" not in result.stderr

    # 1.x NamedError shape: {name, data:{message}}, stderr empty.
    fake_opencode.write_text(
        "#!/usr/bin/env node\n"
        f"process.stdout.write({json.dumps(padding)} + '\\n');\n"
        "process.stdout.write(JSON.stringify({ type: 'error',"
        " error: { name: 'ProviderAuthError',"
        " data: { message: 'Invalid API key' } } }) + '\\n');\n"
        "process.exit(1);\n"
    )
    result = run_bridge()
    assert result.returncode == 1
    assert "Invalid API key" in result.stderr

    # No error event: the stdout fallback carries diagnostics, never model text
    # (a complete text event, or one cut off mid-line by the exit).
    fake_opencode.write_text(
        "#!/usr/bin/env node\n"
        "process.stdout.write(JSON.stringify({ type: 'text',"
        " part: { type: 'text', text: 'SECRET-MODEL-TEXT' } }) + '\\n');\n"
        "process.stdout.write('plain diagnostic line\\n');\n"
        "process.stdout.write('{\"type\":\"text\",\"part\":{\"text\":\"SECRET-CUT');\n"
        "process.exit(1);\n"
    )
    result = run_bridge()
    assert result.returncode == 1
    assert "plain diagnostic line" in result.stderr
    assert "SECRET" not in result.stderr

    # A bracketed log prefix is a diagnostic, not cut-off JSON, and a long
    # non-text event ahead of it must not push it past the length limit.
    fake_opencode.write_text(
        "#!/usr/bin/env node\n"
        f"process.stdout.write({json.dumps(padding)} + '\\n');\n"
        "process.stdout.write('[ERROR] Provider unavailable\\n');\n"
        "process.stdout.write('[{\"type\":\"text\",\"part\":{\"text\":\"SECRET-CUT');\n"
        "process.exit(1);\n"
    )
    result = run_bridge()
    assert result.returncode == 1
    assert "[ERROR] Provider unavailable" in result.stderr
    assert "SECRET" not in result.stderr


def test_node_installer_accepts_opencode_only_extraction_provider(tmp_path: Path) -> None:
    if shutil_which_node() is None:
        pytest.skip("node is not installed")
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


def test_node_installer_accepts_explicit_opencode_extraction_provider(
    tmp_path: Path,
) -> None:
    node = shutil_which_node()
    if node is None:
        pytest.skip("node is not installed")
    assert node is not None
    fake_opencode = tmp_path / "opencode"
    fake_opencode.write_text("#!/bin/sh\nexit 0\n")
    fake_opencode.chmod(0o755)
    script = (
        f"process.env.PATH = {json.dumps(str(tmp_path / 'empty'))};"
        "delete process.env.REFLEXIO_API_KEY;"
        "delete process.env.CLAUDE_SMART_CLI_PATH;"
        f"process.env.CLAUDE_SMART_OPENCODE_PATH = {json.dumps(str(fake_opencode))};"
        f"const installer = require({json.dumps(str(REPO_ROOT / 'bin' / 'claude-smart.js'))});"
        "process.stdout.write(String(installer.hasExtractionProvider()));"
    )

    result = subprocess.run(
        [node, "-e", script],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "true"


def test_node_installer_persists_resolved_opencode_path(tmp_path: Path) -> None:
    node = shutil_which_node()
    if node is None:
        pytest.skip("node is not installed")
    assert node is not None
    env = _isolated_installer_env(tmp_path)
    env.pop("CLAUDE_SMART_OPENCODE_PATH", None)
    opencode = Path(env["PATH"].split(os.pathsep)[0]) / "opencode"
    script = (
        f"const installer = require({json.dumps(str(REPO_ROOT / 'bin' / 'claude-smart.js'))});"
        "installer.persistOpenCodePath();"
    )

    result = subprocess.run(
        [node, "-e", script],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    expected = f'CLAUDE_SMART_OPENCODE_PATH="{opencode}"'
    assert expected in (Path(env["HOME"]) / ".claude-smart" / ".env").read_text()
    assert not (Path(env["HOME"]) / ".reflexio" / ".env").exists()


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
    env.pop("CLAUDE_SMART_OPENCODE_PATH", None)
    return env


def _write_minimal_node_package_root(package_root: Path) -> None:
    bin_dir = package_root / "bin"
    scripts = package_root / "plugin" / "scripts"
    bin_dir.mkdir(parents=True)
    scripts.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "bin" / "claude-smart.js", bin_dir / "claude-smart.js")
    shutil.copy2(REPO_ROOT / "bin" / "install-guide.js", bin_dir / "install-guide.js")
    (package_root / "package.json").write_text(
        json.dumps({"name": "claude-smart", "version": "0.0.0"}) + "\n"
    )
    (package_root / "plugin" / "pyproject.toml").write_text(
        "[project]\nname='claude-smart'\n"
    )
    (package_root / "plugin" / "uv.lock").write_text("version = 1\n")
    smart_install = scripts / "smart-install.sh"
    smart_install.write_text("#!/bin/sh\nexit 0\n")
    smart_install.chmod(0o755)


def _seed_private_node_and_uv(env: dict[str, str]) -> None:
    home = Path(env["HOME"])
    private_node = home / ".claude-smart" / "node" / "current" / "bin"
    uv_bin = home / ".local" / "bin"
    private_node.mkdir(parents=True, exist_ok=True)
    uv_bin.mkdir(parents=True, exist_ok=True)
    for path in [private_node / "node", private_node / "npm"]:
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    uv = uv_bin / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        'printf "uv %s\\n" "$*" >> "$HOME/uv.log"\n'
        '/bin/mkdir -p "$PWD/.venv/bin" "$PWD/.venv/Scripts"\n'
        'printf "#!/bin/sh\\nexit 0\\n" > "$PWD/.venv/bin/python"\n'
        'printf "#!/bin/sh\\nexit 0\\n" > "$PWD/.venv/Scripts/python.exe"\n'
        '/bin/chmod +x "$PWD/.venv/bin/python" "$PWD/.venv/Scripts/python.exe"\n'
        "exit 0\n"
    )
    uv.chmod(0o755)


def test_node_opencode_install_from_npx_root_writes_scoped_loader_only(
    tmp_path: Path,
) -> None:
    node = shutil_which_node()
    if node is None:
        pytest.skip("node is not installed")
    assert node is not None
    package_root = tmp_path / "_npx" / "abc" / "node_modules" / "claude-smart"
    _write_minimal_node_package_root(package_root)
    project = tmp_path / "project"
    project.mkdir()
    env = _isolated_installer_env(tmp_path)
    _seed_private_node_and_uv(env)

    result = subprocess.run(
        [
            node,
            str(package_root / "bin" / "claude-smart.js"),
            "install",
            "--host",
            "opencode",
        ],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    # One load path: no config entry is written, and a fresh project's
    # opencode.json is not created at all. The loader carries the package URL.
    assert not (project / "opencode.json").exists()
    v2_loader = project / ".opencode" / "plugins" / "claude-smart.js"
    assert v2_loader.is_file()
    loader_text = v2_loader.read_text()
    assert "claude-smart OpenCode V2 loader" in loader_text
    assert "plugin/opencode/dist/server.mjs" in loader_text
    entry = json.loads(
        loader_text.splitlines()[2]
        .removeprefix("export { default } from ")
        .removesuffix(";")
    )
    assert entry.startswith("file://")
    local_package = Path(
        entry.removeprefix("file://").removesuffix("/plugin/opencode/dist/server.mjs")
    )
    assert local_package == Path(env["HOME"]) / ".claude-smart" / "opencode" / "claude-smart"
    assert (local_package / "plugin" / "scripts" / "smart-install.sh").exists()
    # A project install must not write the user-wide loader: OpenCode loads that
    # directory in every project, which would widen capture beyond this project.
    assert not (Path(env["XDG_CONFIG_HOME"]) / "opencode" / "plugins" / "claude-smart.js").exists()


def test_node_opencode_loader_conflict_restores_the_previous_package(
    tmp_path: Path,
) -> None:
    node = shutil_which_node()
    if node is None:
        pytest.skip("node is not installed")
    assert node is not None
    package_root = tmp_path / "_npx" / "abc" / "node_modules" / "claude-smart"
    _write_minimal_node_package_root(package_root)
    project = tmp_path / "project"
    loader = project / ".opencode" / "plugins" / "claude-smart.js"
    loader.parent.mkdir(parents=True)
    loader.write_text("// hand-written\n")
    # A plain-JSON legacy entry gets no backup, so the config must not be touched at all.
    config = project / "opencode.json"
    config_text = json.dumps({"plugin": ["claude-smart", "other-plugin"]}) + "\n"
    config.write_text(config_text)
    env = _isolated_installer_env(tmp_path)
    _seed_private_node_and_uv(env)
    previous = Path(env["HOME"]) / ".claude-smart" / "opencode" / "claude-smart"
    (previous / "plugin").mkdir(parents=True)
    (previous / "plugin" / "owner").write_text("older\n")

    result = subprocess.run(
        [node, str(package_root / "bin" / "claude-smart.js"), "install", "--host", "opencode"],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1, result.stderr
    assert "not a claude-smart loader" in result.stderr
    # The conflict exit runs before the commit, so the running runtime is restored.
    assert (previous / "plugin" / "owner").read_text() == "older\n"
    assert sorted(p.name for p in previous.parent.iterdir()) == ["claude-smart"]
    assert loader.read_text() == "// hand-written\n"
    assert config.read_text() == config_text


def test_node_opencode_install_requires_opencode_cli_even_with_api_key(
    tmp_path: Path,
) -> None:
    node = shutil_which_node()
    if node is None:
        pytest.skip("node is not installed")
    assert node is not None
    package_root = tmp_path / "_npx" / "abc" / "node_modules" / "claude-smart"
    _write_minimal_node_package_root(package_root)
    project = tmp_path / "project"
    project.mkdir()
    env = _isolated_installer_env(tmp_path)
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    env["PATH"] = f"{empty_bin}{os.pathsep}/bin{os.pathsep}/usr/bin"
    env["REFLEXIO_API_KEY"] = "rk-test"
    _seed_private_node_and_uv(env)

    result = subprocess.run(
        [
            node,
            str(package_root / "bin" / "claude-smart.js"),
            "install",
            "--host",
            "opencode",
        ],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "OpenCode CLI not found on PATH" in result.stderr
    assert not (project / "opencode.json").exists()


def test_node_opencode_install_requires_bash_on_windows(
    tmp_path: Path,
) -> None:
    node = shutil_which_node()
    if node is None:
        pytest.skip("node is not installed")
    assert node is not None
    package_root = tmp_path / "_npx" / "abc" / "node_modules" / "claude-smart"
    _write_minimal_node_package_root(package_root)
    project = tmp_path / "project"
    project.mkdir()
    env = _isolated_installer_env(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    env.update(
        {
            "CLAUDE_SMART_TEST_PLATFORM": "win32",
            "CLAUDE_SMART_TEST_ARCH": "x64",
            "PATH": str(fake_bin),
        }
    )
    _seed_private_node_and_uv(env)

    result = subprocess.run(
        [
            node,
            str(package_root / "bin" / "claude-smart.js"),
            "install",
            "--host",
            "opencode",
        ],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "Git Bash is required for claude-smart OpenCode support on Windows" in result.stderr
    assert not (project / "opencode.json").exists()


def test_node_opencode_prerequisite_rejects_windows_wsl_bash_stub(
    tmp_path: Path,
) -> None:
    if shutil_which_node() is None:
        pytest.skip("node is not installed")
    fake_bin = tmp_path / "fake-bin"
    system_dir = tmp_path / "Windows" / "System32"
    git_bash_dir = tmp_path / "Program Files" / "Git" / "bin"
    fake_bin.mkdir()
    system_dir.mkdir(parents=True)
    git_bash_dir.mkdir(parents=True)
    fake_opencode = _fake_opencode_path(fake_bin)
    for bash in (system_dir / "bash.exe", git_bash_dir / "bash.exe"):
        bash.write_text("#!/bin/sh\nexit 0\n")
        bash.chmod(0o755)
    script = (
        "process.env.CLAUDE_SMART_TEST_PLATFORM = 'win32';"
        f"process.env.CLAUDE_SMART_OPENCODE_PATH = {json.dumps(str(fake_opencode))};"
        f"const systemDir = {json.dumps(str(system_dir))};"
        f"const gitBashDir = {json.dumps(str(git_bash_dir))};"
        f"const installer = require({json.dumps(str(REPO_ROOT / 'bin' / 'claude-smart.js'))});"
        "process.env.PATH = systemDir;"
        "const withSystemBash = installer.opencodePrerequisiteError();"
        "process.env.PATH = `${systemDir};${gitBashDir}`;"
        "const withGitBash = installer.opencodePrerequisiteError();"
        "process.stdout.write(JSON.stringify({ withSystemBash, withGitBash }));"
    )

    result = _run_node_script(script)
    assert result is not None
    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert "Git Bash is required for claude-smart OpenCode support on Windows" in parsed[
        "withSystemBash"
    ]
    assert parsed["withGitBash"] is None


def test_node_opencode_install_preserves_existing_claude_and_codex_state_on_windows(
    tmp_path: Path,
) -> None:
    node = shutil_which_node()
    if node is None:
        pytest.skip("node is not installed")
    assert node is not None
    package_root = tmp_path / "_npx" / "abc" / "node_modules" / "claude-smart"
    _write_minimal_node_package_root(package_root)
    project = tmp_path / "project"
    project.mkdir()
    env = _isolated_installer_env(tmp_path)
    home = Path(env["HOME"])
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir(exist_ok=True)
    opencode_cmd = fake_bin / "opencode.cmd"
    opencode_cmd.write_text("#!/bin/sh\nexit 0\n")
    opencode_cmd.chmod(0o755)
    bash_exe = fake_bin / "bash.exe"
    bash_exe.write_text("#!/bin/sh\nexit 0\n")
    bash_exe.chmod(0o755)
    env.update(
        {
            "CLAUDE_SMART_TEST_PLATFORM": "win32",
            "CLAUDE_SMART_TEST_ARCH": "x64",
            "PATH": str(fake_bin),
            "CLAUDE_SMART_BACKEND_AUTOSTART": "0",
            "CLAUDE_SMART_DASHBOARD_AUTOSTART": "0",
        }
    )
    _seed_private_node_and_uv(env)
    for env_path in [home / ".reflexio" / ".env", home / ".claude-smart" / ".env"]:
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(
            "# existing host state\n"
            "CLAUDE_SMART_HOST=codex\n"
            'CLAUDE_SMART_READ_ONLY="1"\n'
        )
    claude_cache = (
        home / ".claude" / "plugins" / "cache" / "reflexioai" / "claude-smart" / "0.2.46"
    )
    codex_cache = (
        home / ".codex" / "plugins" / "cache" / "reflexioai" / "claude-smart" / "0.2.46"
    )
    for cache in [claude_cache, codex_cache]:
        cache.mkdir(parents=True)
        (cache / "keep.txt").write_text("keep\n")
    codex_config = home / ".codex" / "config.toml"
    codex_config.parent.mkdir(parents=True, exist_ok=True)
    codex_config.write_text(
        '[plugins."claude-smart@reflexioai"]\n'
        'enabled = true\n'
        'source = "reflexioai"\n'
    )
    reflexio_data = home / ".reflexio" / "data" / "reflexio.db"
    reflexio_data.parent.mkdir(parents=True, exist_ok=True)
    reflexio_data.write_text("learned data\n")
    session_file = home / ".claude-smart" / "sessions" / "codex.jsonl"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text("{}\n")

    result = subprocess.run(
        [
            node,
            str(package_root / "bin" / "claude-smart.js"),
            "install",
            "--host",
            "opencode",
        ],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    # One load path: no config entry, and the loader carries the package URL.
    assert not (project / "opencode.json").exists()
    loader_text = (project / ".opencode" / "plugins" / "claude-smart.js").read_text()
    entry = json.loads(
        loader_text.splitlines()[2]
        .removeprefix("export { default } from ")
        .removesuffix(";")
    )
    local_package = Path(
        entry.removeprefix("file://").removesuffix("/plugin/opencode/dist/server.mjs")
    )
    assert local_package == home / ".claude-smart" / "opencode" / "claude-smart"
    assert (local_package / "plugin" / "scripts" / "smart-install.sh").exists()
    runtime_env = (home / ".claude-smart" / ".env").read_text()
    assert "CLAUDE_SMART_HOST=opencode" in runtime_env
    assert "CLAUDE_SMART_HOST=codex" not in runtime_env
    assert 'CLAUDE_SMART_READ_ONLY="1"' in runtime_env
    # ~/.reflexio/.env belongs to other Reflexio tools and is never rewritten.
    assert (home / ".reflexio" / ".env").read_text() == (
        "# existing host state\n"
        "CLAUDE_SMART_HOST=codex\n"
        'CLAUDE_SMART_READ_ONLY="1"\n'
    )
    assert f'CLAUDE_SMART_OPENCODE_PATH="{opencode_cmd}"' in runtime_env
    assert (claude_cache / "keep.txt").read_text() == "keep\n"
    assert (codex_cache / "keep.txt").read_text() == "keep\n"
    assert codex_config.read_text() == (
        '[plugins."claude-smart@reflexioai"]\n'
        'enabled = true\n'
        'source = "reflexioai"\n'
    )
    assert reflexio_data.read_text() == "learned data\n"
    assert session_file.read_text() == "{}\n"


def test_node_opencode_install_stable_root_bootstrap_failure_leaves_config_unchanged(
    tmp_path: Path,
) -> None:
    node = shutil_which_node()
    if node is None:
        pytest.skip("node is not installed")
    assert node is not None
    package_root = tmp_path / "global" / "node_modules" / "claude-smart"
    bin_dir = package_root / "bin"
    bin_dir.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "bin" / "claude-smart.js", bin_dir / "claude-smart.js")
    shutil.copy2(REPO_ROOT / "bin" / "install-guide.js", bin_dir / "install-guide.js")
    (package_root / "package.json").write_text(
        json.dumps({"name": "claude-smart", "version": "0.0.0"}) + "\n"
    )
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
    assert "could not prepare claude-smart OpenCode package" in result.stderr
    assert config.read_text() == original


def test_opencode_install_copy_failure_preserves_existing_local_package(
    monkeypatch, tmp_path: Path
) -> None:
    repo_root = tmp_path / "repo"
    plugin_scripts = repo_root / "plugin" / "scripts"
    plugin_scripts.mkdir(parents=True)
    (repo_root / "package.json").write_text('{"name":"claude-smart"}\n')
    (plugin_scripts / "smart-install.sh").write_text("#!/bin/sh\nexit 0\n")
    local_package = tmp_path / "home" / ".claude-smart" / "opencode" / "claude-smart"
    local_package.mkdir(parents=True)
    marker = local_package / "marker.txt"
    marker.write_text("keep\n")

    def failing_copytree(src: Path, dst: Path, **_kwargs: object) -> Path:
        assert src == repo_root
        dst.mkdir(parents=True, exist_ok=True)
        (dst / "partial.txt").write_text("partial\n")
        raise OSError("copy failed")

    monkeypatch.setattr(cli, "_REPO_ROOT", repo_root)
    monkeypatch.setattr(cli, "_PLUGIN_ROOT", repo_root / "plugin")
    monkeypatch.setattr(cli, "_OPENCODE_LOCAL_PACKAGE_DIR", local_package)
    monkeypatch.setattr(cli.shutil, "copytree", failing_copytree)

    with pytest.raises(OSError, match="copy failed"):
        cli._install_opencode_plugin_package()

    assert marker.read_text() == "keep\n"
    assert not (local_package / "partial.txt").exists()
    assert not (local_package.parent / ".install.lock").exists()
    assert list(local_package.parent.iterdir()) == [local_package]


def test_opencode_install_writes_project_loader_after_bootstrap(
    monkeypatch, tmp_path, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_CLAUDE_SMART_ENV_PATH", tmp_path / ".claude-smart" / ".env")
    monkeypatch.setattr(cli.os, "name", "nt")
    monkeypatch.setattr(cli, "Path", PosixPath)
    monkeypatch.setattr(cli, "_resolve_bash", lambda: r"C:\Program Files\Git\bin\bash.exe")
    monkeypatch.setattr(
        cli.shutil,
        "which",
        lambda name: f"/bin/{name}" if name in {"codex", "opencode"} else None,
    )
    monkeypatch.setattr(cli, "_bootstrap_opencode_install", lambda _read_only: (True, "/plugin"))

    rc = cli.cmd_install(argparse.Namespace(host="opencode", global_config=False))

    assert rc == 0
    # One load path: the loader is written and no config file is created.
    assert not (tmp_path / "opencode.json").exists()
    loader = tmp_path / ".opencode" / "plugins" / "claude-smart.js"
    assert loader.read_text() == cli._opencode_v2_plugin_body(PosixPath("/plugin"))
    assert "CLAUDE_SMART_HOST=opencode" in (
        tmp_path / ".claude-smart" / ".env"
    ).read_text()
    assert "Restart OpenCode" in capsys.readouterr().out


def test_opencode_install_patches_existing_dot_opencode_config(
    monkeypatch, tmp_path
) -> None:
    legacy_config = tmp_path / ".opencode" / "opencode.json"
    legacy_config.parent.mkdir()
    legacy_config.write_text(json.dumps({"plugin": ["other-plugin"]}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_CLAUDE_SMART_ENV_PATH", tmp_path / ".claude-smart" / ".env")
    monkeypatch.setattr(
        cli.shutil,
        "which",
        lambda name: f"/bin/{name}" if name in {"codex", "opencode"} else None,
    )
    monkeypatch.setattr(cli, "_bootstrap_opencode_install", lambda _read_only: (True, "/plugin"))

    rc = cli.cmd_install(argparse.Namespace(host="opencode", global_config=False))

    assert rc == 0
    assert not (tmp_path / "opencode.json").exists()
    # The unrelated legacy entry stays untouched; claude-smart loads through
    # the loader alone.
    assert json.loads(legacy_config.read_text())["plugin"] == ["other-plugin"]
    assert (tmp_path / ".opencode" / "plugins" / "claude-smart.js").is_file()


def test_opencode_install_migrates_existing_plugins_field(
    monkeypatch, tmp_path
) -> None:
    config = tmp_path / "opencode.json"
    config.write_text(json.dumps({"plugins": ["other-plugin"], "theme": "system"}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_CLAUDE_SMART_ENV_PATH", tmp_path / ".claude-smart" / ".env")
    monkeypatch.setattr(
        cli.shutil,
        "which",
        lambda name: f"/bin/{name}" if name in {"codex", "opencode"} else None,
    )
    monkeypatch.setattr(cli, "_bootstrap_opencode_install", lambda _read_only: (True, "/plugin"))

    rc = cli.cmd_install(argparse.Namespace(host="opencode", global_config=False))

    parsed = json.loads(config.read_text())
    assert rc == 0
    # The legacy `plugins` field is still normalized, but claude-smart adds no
    # config entry any more — the loader is the single load path.
    assert parsed["plugin"] == ["other-plugin"]
    assert "plugins" not in parsed
    assert (tmp_path / ".opencode" / "plugins" / "claude-smart.js").is_file()


def test_node_installer_patches_opencode_jsonc(tmp_path: Path) -> None:
    if shutil_which_node() is None:
        pytest.skip("node is not installed")
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
    assert parsed["plugin"][0] == "other"
    assert parsed["plugin"][1].startswith("file://")
    assert parsed["plugin"][1] != "claude-smart"
    assert parsed["theme"] == "system"


def test_node_installer_patches_opencode_to_local_file_spec(tmp_path: Path) -> None:
    if shutil_which_node() is None:
        pytest.skip("node is not installed")
    config = tmp_path / "opencode.json"
    local_package = tmp_path / "local-package"
    local_package.mkdir()
    old_package = tmp_path / "old-cache" / "node_modules" / "claude-smart"
    old_package.mkdir(parents=True)
    (old_package / "package.json").write_text('{"name":"claude-smart"}\n')
    config.write_text(
        json.dumps(
            {
                "plugin": [
                    "other",
                    "claude-smart",
                    old_package.as_uri(),
                ]
            }
        )
    )
    script = (
        f"const installer = require({json.dumps(str(REPO_ROOT / 'bin' / 'claude-smart.js'))});"
        f"const spec = installer.opencodeLocalPluginSpec({json.dumps(str(local_package))});"
        f"installer.patchOpenCodePluginConfig({json.dumps(str(config))}, {{ install: true, pluginSpec: spec }});"
        "process.stdout.write(require('fs').readFileSync(process.argv[1], 'utf8'));"
    )

    result = _run_node_script(script, str(config))
    assert result is not None

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["plugin"] == [
        "other",
        local_package.as_uri(),
    ]


def test_node_installer_preserves_unrelated_file_specs_named_claude_smart(
    tmp_path: Path,
) -> None:
    if shutil_which_node() is None:
        pytest.skip("node is not installed")
    missing_manifest = tmp_path / "vendor" / "claude-smart"
    wrong_manifest = tmp_path / "other" / "claude-smart"
    real_package = tmp_path / "real-package"
    missing_manifest.mkdir(parents=True)
    wrong_manifest.mkdir(parents=True)
    real_package.mkdir()
    (wrong_manifest / "package.json").write_text('{"name":"not-claude-smart"}\n')
    (real_package / "package.json").write_text('{"name":"claude-smart"}\n')
    config = tmp_path / "opencode.json"
    config.write_text(
        json.dumps(
            {
                "plugin": [
                    "other",
                    missing_manifest.as_uri(),
                    wrong_manifest.as_uri(),
                    real_package.as_uri(),
                    "claude-smart@0.2.46",
                ]
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
    assert json.loads(result.stdout)["plugin"] == [
        "other",
        missing_manifest.as_uri(),
        wrong_manifest.as_uri(),
    ]


def test_node_installer_uninstalls_and_preserves_other_plugin_shapes(
    tmp_path: Path,
) -> None:
    if shutil_which_node() is None:
        pytest.skip("node is not installed")
    local_package = tmp_path / "local-package"
    local_package.mkdir()
    (local_package / "package.json").write_text('{"name":"claude-smart"}\n')
    config = tmp_path / "opencode.json"
    config.write_text(
        json.dumps(
            {
                "plugin": [
                    "claude-smart",
                    "other",
                    ["tuple-plugin", {"enabled": True}],
                    local_package.as_uri(),
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


def test_node_opencode_install_copy_failure_preserves_existing_local_package(
    tmp_path: Path,
) -> None:
    node = shutil_which_node()
    if node is None:
        pytest.skip("node is not installed")
    assert node is not None
    package_root = tmp_path / "global" / "node_modules" / "claude-smart"
    _write_minimal_node_package_root(package_root)
    env = _isolated_installer_env(tmp_path)
    local_package = (
        Path(env["HOME"]) / ".claude-smart" / "opencode" / "claude-smart"
    )
    local_package.mkdir(parents=True)
    (local_package / "marker.txt").write_text("keep\n")
    script = (
        "const fs = require('fs');"
        "const path = require('path');"
        "fs.cpSync = function(_src, dst) {"
        "  fs.mkdirSync(dst, { recursive: true });"
        "  fs.writeFileSync(path.join(dst, 'partial.txt'), 'partial\\n');"
        "  throw new Error('copy failed');"
        "};"
        f"const installer = require({json.dumps(str(package_root / 'bin' / 'claude-smart.js'))});"
        "try {"
        "  installer.installOpenCodePluginPackage();"
        "  process.stdout.write('unexpected success');"
        "  process.exit(2);"
        "} catch (err) {"
        "  const target = path.join(process.env.HOME, '.claude-smart', 'opencode', 'claude-smart');"
        "  const parent = path.dirname(target);"
        "  process.stdout.write(JSON.stringify({"
        "    message: err.message,"
        "    marker: fs.existsSync(path.join(target, 'marker.txt')) ? fs.readFileSync(path.join(target, 'marker.txt'), 'utf8') : null,"
        "    partialAtTarget: fs.existsSync(path.join(target, 'partial.txt')),"
        "    entries: fs.readdirSync(parent).sort()"
        "  }));"
        "}"
    )

    result = subprocess.run(
        [node, "-e", script],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed["message"] == "copy failed"
    assert parsed["marker"] == "keep\n"
    assert parsed["partialAtTarget"] is False
    assert parsed["entries"] == ["claude-smart"]


def test_node_opencode_install_replaces_broken_local_package_symlink(
    tmp_path: Path,
) -> None:
    node = shutil_which_node()
    if node is None:
        pytest.skip("node is not installed")
    assert node is not None
    package_root = tmp_path / "global" / "node_modules" / "claude-smart"
    _write_minimal_node_package_root(package_root)
    env = _isolated_installer_env(tmp_path)
    local_package = (
        Path(env["HOME"]) / ".claude-smart" / "opencode" / "claude-smart"
    )
    local_package.parent.mkdir(parents=True)
    local_package.symlink_to(tmp_path / "missing-package")
    script = (
        "const fs = require('fs');"
        "const path = require('path');"
        "const target = path.join(process.env.HOME, '.claude-smart', 'opencode', 'claude-smart');"
        "const realRenameSync = fs.renameSync;"
        "fs.renameSync = function(src, dst) {"
        "  let targetIsSymlink = false;"
        "  try { targetIsSymlink = fs.lstatSync(target).isSymbolicLink(); } catch {}"
        "  if (dst === target && targetIsSymlink) {"
        "    const err = new Error('cannot replace broken symlink');"
        "    err.code = 'ENOTDIR';"
        "    throw err;"
        "  }"
        "  return realRenameSync(src, dst);"
        "};"
        f"const installer = require({json.dumps(str(package_root / 'bin' / 'claude-smart.js'))});"
        "const packageRoot = installer.installOpenCodePluginPackage();"
        "process.stdout.write(JSON.stringify({"
        "  packageRoot,"
        "  isSymlink: fs.lstatSync(target).isSymbolicLink(),"
        "  hasManifest: fs.existsSync(path.join(target, 'package.json'))"
        "}));"
    )

    result = subprocess.run(
        [node, "-e", script],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed == {
        "packageRoot": str(local_package),
        "isSymlink": False,
        "hasManifest": True,
    }


def test_node_installer_migrates_existing_plugins_field(tmp_path: Path) -> None:
    if shutil_which_node() is None:
        pytest.skip("node is not installed")
    config = tmp_path / "opencode.json"
    local_package = tmp_path / "local-package"
    local_package.mkdir()
    config.write_text(json.dumps({"plugins": ["other"], "theme": "system"}))
    script = (
        f"const installer = require({json.dumps(str(REPO_ROOT / 'bin' / 'claude-smart.js'))});"
        f"const spec = installer.opencodeLocalPluginSpec({json.dumps(str(local_package))});"
        f"installer.patchOpenCodePluginConfig({json.dumps(str(config))}, {{ install: true, pluginSpec: spec }});"
        "process.stdout.write(require('fs').readFileSync(process.argv[1], 'utf8'));"
    )

    result = _run_node_script(script, str(config))
    assert result is not None

    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed["plugin"] == ["other", local_package.as_uri()]
    assert "plugins" not in parsed
    assert parsed["theme"] == "system"


def test_node_installer_uninstall_migrates_existing_plugins_field(tmp_path: Path) -> None:
    if shutil_which_node() is None:
        pytest.skip("node is not installed")
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
        pytest.skip("node is not installed")
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
        pytest.skip("node is not installed")
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
        pytest.skip("node is not installed")
    config = tmp_path / "opencode.jsonc"
    local_package = tmp_path / "local-package"
    local_package.mkdir()
    original = '{\n  // keep my notes\n  "plugin": ["other"]\n}\n'
    config.write_text(original)
    script = (
        f"const installer = require({json.dumps(str(REPO_ROOT / 'bin' / 'claude-smart.js'))});"
        f"const spec = installer.opencodeLocalPluginSpec({json.dumps(str(local_package))});"
        f"const result = installer.patchOpenCodePluginConfig({json.dumps(str(config))}, {{ install: true, pluginSpec: spec }});"
        "process.stdout.write(JSON.stringify({ result, text: require('fs').readFileSync(process.argv[1], 'utf8'), backup: require('fs').readFileSync(`${process.argv[1]}.bak`, 'utf8') }));"
    )

    result = _run_node_script(script, str(config))
    assert result is not None

    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed["result"]["backupPath"] == f"{config}.bak"
    assert parsed["backup"] == original
    assert json.loads(parsed["text"])["plugin"] == ["other", local_package.as_uri()]


def test_node_installer_scope_config_paths_match_python(tmp_path: Path) -> None:
    if shutil_which_node() is None:
        pytest.skip("node is not installed")
    script = (
        f"process.env.XDG_CONFIG_HOME = {json.dumps(str(tmp_path / 'xdg'))};"
        f"const installer = require({json.dumps(str(REPO_ROOT / 'bin' / 'claude-smart.js'))});"
        "process.stdout.write(JSON.stringify({"
        " project: installer.opencodeScopeConfigPaths([], process.argv[1]),"
        " global: installer.opencodeScopeConfigPaths(['--global'], process.argv[1]) }));"
    )

    result = _run_node_script(script, str(tmp_path))
    assert result is not None

    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert [Path(p) for p in parsed["project"]] == cli._opencode_scope_config_paths(
        cwd=tmp_path
    )
    assert [Path(p) for p in parsed["global"]] == [
        tmp_path / "xdg" / "opencode" / name for name in cli._OPENCODE_CONFIG_NAMES
    ]


def test_node_installer_removes_legacy_entries_from_every_scope_config(
    tmp_path: Path,
) -> None:
    if shutil_which_node() is None:
        pytest.skip("node is not installed")
    (tmp_path / ".opencode").mkdir()
    configs = [tmp_path / "opencode.json", tmp_path / ".opencode" / "opencode.jsonc"]
    for config in configs:
        config.write_text(json.dumps({"plugin": ["keep", "claude-smart"]}) + "\n")
    bad = tmp_path / "bad"
    (bad / ".opencode").mkdir(parents=True)
    (bad / "opencode.json").write_text('{"plugin": ["claude-smart"]}\n')
    (bad / ".opencode" / "opencode.json").write_text('{"plugin": "nope"}\n')
    script = (
        f"const installer = require({json.dumps(str(REPO_ROOT / 'bin' / 'claude-smart.js'))});"
        "const changed = installer.removeOpenCodeLegacyEntries([], process.argv[1]).map((r) => r.configPath);"
        "let error = null;"
        "try { installer.removeOpenCodeLegacyEntries([], process.argv[2]); } catch (err) { error = err.message; }"
        "process.stdout.write(JSON.stringify({ changed, error }));"
    )

    result = _run_node_script(script, str(tmp_path), str(bad))
    assert result is not None

    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert [Path(p) for p in parsed["changed"]] == configs
    for config in configs:
        assert json.loads(config.read_text())["plugin"] == ["keep"]
    assert "must be a JSON array" in parsed["error"]
    # Validated before writing: the valid config in the failing scope is untouched.
    assert (bad / "opencode.json").read_text() == '{"plugin": ["claude-smart"]}\n'


def test_node_installer_reports_unusable_loader_path_as_conflict(
    tmp_path: Path,
) -> None:
    if shutil_which_node() is None:
        pytest.skip("node is not installed")
    package = tmp_path / "pkg"
    dir_project = tmp_path / "dir"
    (dir_project / ".opencode" / "plugins" / "claude-smart.js").mkdir(parents=True)
    bytes_project = tmp_path / "bytes"
    foreign = bytes_project / ".opencode" / "plugins" / "claude-smart.js"
    foreign.parent.mkdir(parents=True)
    foreign.write_bytes(b"\xff\xfe foreign\n")
    free_project = tmp_path / "free"
    free_project.mkdir()
    script = (
        f"const installer = require({json.dumps(str(REPO_ROOT / 'bin' / 'claude-smart.js'))});"
        "const [dir, bytes, free, pkg] = process.argv.slice(1);"
        "process.stdout.write(JSON.stringify({"
        " dir: installer.openCodeV2LoaderConflicts([], dir),"
        " dirWrite: installer.writeOpenCodeV2Plugin([], pkg, dir).status,"
        " dirRemove: installer.removeOpenCodeV2Plugin([], dir),"
        " bytes: installer.openCodeV2LoaderConflicts([], bytes),"
        " bytesWrite: installer.writeOpenCodeV2Plugin([], pkg, bytes).status,"
        " free: installer.openCodeV2LoaderConflicts([], free) }));"
    )

    result = _run_node_script(
        script, str(dir_project), str(bytes_project), str(free_project), str(package)
    )
    assert result is not None

    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert "cannot be read as a file" in parsed["dir"]
    assert parsed["dirWrite"] == "conflict"
    assert parsed["dirRemove"] is False
    assert parsed["bytes"] == "It is not a claude-smart loader"
    assert parsed["bytesWrite"] == "conflict"
    assert parsed["free"] is None
    assert foreign.read_bytes() == b"\xff\xfe foreign\n"

def test_node_installer_skips_backup_for_plain_json(tmp_path: Path) -> None:
    if shutil_which_node() is None:
        pytest.skip("node is not installed")
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
        pytest.skip("node is not installed")
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
        pytest.skip("node is not installed")
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


def test_node_opencode_v2_setup_registers_prompt_and_keeps_cache_header(
    tmp_path: Path,
) -> None:
    if shutil_which_node() is None:
        pytest.skip("node is not installed")
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
        if (typeof mod.default.setup !== "function") throw new Error("missing setup");
        const hooks = {{}};
        const dispose = await mod.default.setup({{
          location: {{ directory: "/repo" }},
          session: {{
            hook(name, fn) {{
              hooks[name] = fn;
              return {{ dispose() {{}} }};
            }},
          }},
          tool: {{
            hook(name, fn) {{
              hooks[name] = fn;
              return {{ dispose() {{}} }};
            }},
          }},
          event: {{ async *subscribe() {{}} }},
        }});
        const system = [
          {{ type: "text", text: "cache-header" }},
          {{ type: "text", text: "stable system body" }},
        ];
        await hooks.prompt({{ sessionID: "s1", prompt: {{ text: "remember" }} }});
        await hooks.context({{ sessionID: "s1", system }});
        if (typeof dispose === "function") await dispose();
        process.stdout.write(JSON.stringify({{
          registered: Object.keys(hooks).sort(),
          header: system[0].text,
          body: system[1].text,
          injected: system.slice(2).map((part) => part.text),
          injectedLast: system[system.length - 1].text,
        }}));
      }}).catch((err) => {{
        console.error(err);
        process.exit(1);
      }});
    """

    result = _run_node_script(script)
    assert result is not None
    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed["registered"] == ["context", "execute.after", "prompt"]
    assert parsed["header"] == "cache-header"
    assert parsed["body"] == "stable system body"
    assert parsed["injected"] == ["learned context"]
    assert parsed["injectedLast"] == "learned context"


def test_node_opencode_v2_loader_round_trip(tmp_path: Path) -> None:
    node = shutil_which_node()
    if node is None:
        pytest.skip("node is not installed")
    assert node is not None
    package = tmp_path / "pkg"
    package.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    script = (
        f"process.env.XDG_CONFIG_HOME = {json.dumps(str(tmp_path / 'xdg'))};"
        f"const installer = require({json.dumps(str(REPO_ROOT / 'bin' / 'claude-smart.js'))});"
        f"const project = {json.dumps(str(project))};"
        f"const written = installer.writeOpenCodeV2Plugin([], {json.dumps(str(package))}, project);"
        f"const globalWritten = installer.writeOpenCodeV2Plugin(['--global'], {json.dumps(str(package))}, project);"
        "const kept = installer.removeOpenCodeV2Plugin([], project);"
        "const keptGlobal = installer.removeOpenCodeV2Plugin(['--global'], project);"
        "const again = installer.writeOpenCodeV2Plugin([], " + json.dumps(str(package)) + ", project);"
        "require('fs').writeFileSync(written.path, '// claude-smart OpenCode V2 loader\\nuser edit\\n');"
        "const spared = installer.removeOpenCodeV2Plugin([], project);"
        "require('fs').writeFileSync(written.path, 'other plugin\\n');"
        "const conflict = installer.writeOpenCodeV2Plugin([], " + json.dumps(str(package)) + ", project);"
        "process.stdout.write(JSON.stringify({ written: written.path, globalWritten: globalWritten.path, kept, keptGlobal, spared, conflict: conflict.status, still: require('fs').readFileSync(written.path, 'utf8') }));"
    )
    result = subprocess.run([node, "-e", script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    # A project install stays inside the project; only --global writes the user-wide
    # loader that OpenCode loads in every project.
    assert Path(parsed["written"]) == project / ".opencode" / "plugins" / "claude-smart.js"
    assert Path(parsed["globalWritten"]) == tmp_path / "xdg" / "opencode" / "plugins" / "claude-smart.js"
    assert parsed["kept"] is True
    assert parsed["keptGlobal"] is True
    assert parsed["spared"] is False
    assert parsed["conflict"] == "conflict"
    assert parsed["still"] == "other plugin\n"


def test_node_opencode_server_injects_cached_context_for_session_ids(
    tmp_path: Path,
) -> None:
    if shutil_which_node() is None:
        pytest.skip("node is not installed")
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
        pytest.skip("node is not installed")
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


def test_node_opencode_server_prefers_current_plugin_root_when_valid(
    tmp_path: Path,
) -> None:
    if shutil_which_node() is None:
        pytest.skip("node is not installed")
    stale_root = tmp_path / "stale-plugin"
    _write_minimal_plugin_root(stale_root, name="stale")
    home = tmp_path / "home"
    (home / ".reflexio").mkdir(parents=True)
    (home / ".reflexio" / "plugin-root").symlink_to(stale_root)

    calls = _run_opencode_server_and_read_calls(
        tmp_path=tmp_path,
        import_path=REPO_ROOT / "plugin" / "opencode" / "dist" / "server.mjs",
        home=home,
    )

    assert Path(calls[0]["script"]) == (
        REPO_ROOT / "plugin" / "scripts" / "hook_entry.sh"
    )


def test_node_opencode_server_uses_stable_root_when_current_is_dist_only(
    tmp_path: Path,
) -> None:
    if shutil_which_node() is None:
        pytest.skip("node is not installed")
    stable_root = tmp_path / "stable-plugin"
    _write_minimal_plugin_root(stable_root)
    dist_only_root = tmp_path / "dist-only-plugin"
    import_path = _copy_opencode_dist(dist_only_root)
    home = tmp_path / "home"
    (home / ".reflexio").mkdir(parents=True)
    (home / ".reflexio" / "plugin-root").symlink_to(stable_root)

    calls = _run_opencode_server_and_read_calls(
        tmp_path=tmp_path,
        import_path=import_path,
        home=home,
    )

    assert Path(calls[0]["script"]) == stable_root / "scripts" / "hook_entry.sh"


def test_node_opencode_server_explicit_plugin_root_overrides_current_and_stable(
    tmp_path: Path,
) -> None:
    if shutil_which_node() is None:
        pytest.skip("node is not installed")
    explicit_root = tmp_path / "explicit-plugin"
    stable_root = tmp_path / "stable-plugin"
    _write_minimal_plugin_root(explicit_root, name="explicit")
    _write_minimal_plugin_root(stable_root, name="stable")
    home = tmp_path / "home"
    (home / ".reflexio").mkdir(parents=True)
    (home / ".reflexio" / "plugin-root").symlink_to(stable_root)

    calls = _run_opencode_server_and_read_calls(
        tmp_path=tmp_path,
        import_path=REPO_ROOT / "plugin" / "opencode" / "dist" / "server.mjs",
        home=home,
        extra_env={"CLAUDE_SMART_PLUGIN_ROOT": str(explicit_root)},
    )

    assert Path(calls[0]["script"]) == explicit_root / "scripts" / "hook_entry.sh"


def test_node_opencode_server_uses_text_root_when_symlink_is_unavailable(
    tmp_path: Path,
) -> None:
    if shutil_which_node() is None:
        pytest.skip("node is not installed")
    stable_root = tmp_path / "stable-plugin"
    _write_minimal_plugin_root(stable_root)
    dist_only_root = tmp_path / "dist-only-plugin"
    import_path = _copy_opencode_dist(dist_only_root)
    home = tmp_path / "home"
    (home / ".reflexio").mkdir(parents=True)
    (home / ".reflexio" / "plugin-root.txt").write_text(f"{stable_root}\n")

    calls = _run_opencode_server_and_read_calls(
        tmp_path=tmp_path,
        import_path=import_path,
        home=home,
    )

    assert Path(calls[0]["script"]) == stable_root / "scripts" / "hook_entry.sh"


def test_node_opencode_server_treats_npx_current_root_as_transient(
    tmp_path: Path,
) -> None:
    if shutil_which_node() is None:
        pytest.skip("node is not installed")
    stable_root = tmp_path / "stable-plugin"
    _write_minimal_plugin_root(stable_root)
    npx_root = tmp_path / "_npx" / "12345" / "node_modules" / "claude-smart" / "plugin"
    _write_minimal_plugin_root(npx_root, name="npx")
    import_path = _copy_opencode_dist(npx_root)
    home = tmp_path / "home"
    (home / ".reflexio").mkdir(parents=True)
    (home / ".reflexio" / "plugin-root").symlink_to(stable_root)

    calls = _run_opencode_server_and_read_calls(
        tmp_path=tmp_path,
        import_path=import_path,
        home=home,
    )

    assert Path(calls[0]["script"]) == stable_root / "scripts" / "hook_entry.sh"


def test_node_opencode_server_falls_back_to_transient_root_without_stable_candidate(
    tmp_path: Path,
) -> None:
    if shutil_which_node() is None:
        pytest.skip("node is not installed")
    npx_root = tmp_path / "_npx" / "12345" / "node_modules" / "claude-smart" / "plugin"
    _write_minimal_plugin_root(npx_root, name="npx")
    import_path = _copy_opencode_dist(npx_root)
    home = tmp_path / "home"
    home.mkdir()

    calls = _run_opencode_server_and_read_calls(
        tmp_path=tmp_path,
        import_path=import_path,
        home=home,
    )

    assert Path(calls[0]["script"]) == npx_root / "scripts" / "hook_entry.sh"


def test_node_opencode_server_dispatches_lifecycle_tool_idle_and_dispose(
    tmp_path: Path,
) -> None:
    if shutil_which_node() is None:
        pytest.skip("node is not installed")
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
        pytest.skip("node is not installed")
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
        pytest.skip("node is not installed")
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
        pytest.skip("node is not installed")
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


def test_node_opencode_v2_logs_when_event_stream_ends(tmp_path: Path) -> None:
    if shutil_which_node() is None:
        pytest.skip("node is not installed")
    _write_fake_bash_recorder(tmp_path)
    script = f"""
      process.env.PATH = {json.dumps(str(tmp_path))} + ":" + process.env.PATH;
      const warns = [];
      console.warn = (...args) => {{ warns.push(args.join(" ")); }};
      import({json.dumps(str(REPO_ROOT / "plugin" / "opencode" / "dist" / "server.mjs"))}).then(async (mod) => {{
        const ctx = {{
          location: {{ directory: "/repo" }},
          sessionsOutliveDispose: true,
          session: {{ hook: async () => ({{ dispose() {{}} }}) }},
          tool: {{ hook: async () => ({{ dispose() {{}} }}) }},
          event: {{ subscribe: () => (async function* () {{}}()) }},
        }};
        await mod.default.setup(ctx);
        // The subscription ended without an abort; give the loop a tick to notice.
        await new Promise((resolve) => setTimeout(resolve, 100));
        process.stdout.write(JSON.stringify(warns));
      }}).catch((err) => {{
        console.error(err);
        process.exit(1);
      }});
    """

    result = _run_node_script(script)
    assert result is not None

    assert result.returncode == 0, result.stderr
    # A dead stream stops capture for the life of the instance; it must be
    # reported, not swallowed.
    ended = [warn for warn in json.loads(result.stdout) if "event stream ended" in warn]
    assert len(ended) == 1


def test_node_opencode_v2_setup_merges_text_events_into_stop(
    tmp_path: Path,
) -> None:
    if shutil_which_node() is None:
        pytest.skip("node is not installed")
    log_path = tmp_path / "calls.jsonl"
    _write_fake_bash_recorder(tmp_path)
    script = f"""
      process.env.PATH = {json.dumps(str(tmp_path))} + ":" + process.env.PATH;
      process.env.CALL_LOG = {json.dumps(str(log_path))};
      const fs = require("fs");
      const readCalls = () =>
        fs.readFileSync(process.env.CALL_LOG, "utf8").trim()
          .split("\\n").filter(Boolean).map((line) => JSON.parse(line));
      const stopPayloads = () =>
        readCalls()
          .filter((call) => Array.isArray(call.args) && call.args.includes("stop"))
          .map((call) => JSON.parse(call.payload));
      const waitForStops = async (n) => {{
        for (let i = 0; i < 200 && stopPayloads().length < n; i += 1) {{
          await new Promise((resolve) => setTimeout(resolve, 25));
        }}
      }};
      import({json.dumps(str(REPO_ROOT / "plugin" / "opencode" / "dist" / "server.mjs"))}).then(async (mod) => {{
        const queue = [];
        let wake = null;
        const push = (event) => {{
          queue.push(event);
          if (wake) {{ const w = wake; wake = null; w(); }}
        }};
        const hooks = {{}};
        const ctx = {{
          location: {{ directory: "/repo" }},
          sessionsOutliveDispose: true,
          session: {{ hook: async (name, fn) => {{ hooks[name] = fn; return {{ dispose() {{}} }}; }} }},
          tool: {{ hook: async () => ({{ dispose() {{}} }}) }},
          event: {{
            subscribe: () => (async function* () {{
              for (;;) {{
                if (!queue.length) await new Promise((resolve) => {{ wake = resolve; }});
                while (queue.length) yield queue.shift();
              }}
            }})(),
          }},
        }};
        const dispose = await mod.default.setup(ctx);
        await hooks.prompt({{ sessionID: "s-v2", prompt: {{ text: "first prompt" }} }});
        push({{ type: "session.text.ended", data: {{ sessionID: "s-v2", assistantMessageID: "m1", ordinal: 0, text: "First part" }} }});
        push({{ type: "session.text.ended", data: {{ sessionID: "s-v2", assistantMessageID: "m1", ordinal: 1, text: "Second part" }} }});
        push({{ type: "session.execution.succeeded", data: {{ sessionID: "s-v2" }} }});
        await waitForStops(1);
        await hooks.prompt({{ sessionID: "s-v2", prompt: {{ text: "second prompt" }} }});
        push({{ type: "session.text.ended", data: {{ sessionID: "s-v2", assistantMessageID: "m2", ordinal: 0, text: "Final answer" }} }});
        push({{ type: "session.execution.succeeded", data: {{ sessionID: "s-v2" }} }});
        await waitForStops(2);
        await dispose();
        process.stdout.write(JSON.stringify(stopPayloads()));
      }}).catch((err) => {{
        console.error(err);
        process.exit(1);
      }});
    """

    result = _run_node_script(script)
    assert result is not None

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        {
            "session_id": "s-v2",
            "cwd": "/repo",
            "last_assistant_message": "First part\n\nSecond part",
        },
        {
            "session_id": "s-v2",
            "cwd": "/repo",
            "last_assistant_message": "Final answer",
        },
    ]


def test_node_opencode_v2_failed_and_interrupted_turns_flush_once(
    tmp_path: Path,
) -> None:
    if shutil_which_node() is None:
        pytest.skip("node is not installed")
    log_path = tmp_path / "calls.jsonl"
    _write_fake_bash_recorder(tmp_path)
    script = f"""
      process.env.PATH = {json.dumps(str(tmp_path))} + ":" + process.env.PATH;
      process.env.CALL_LOG = {json.dumps(str(log_path))};
      const fs = require("fs");
      const readCalls = () =>
        fs.readFileSync(process.env.CALL_LOG, "utf8").trim()
          .split("\\n").filter(Boolean).map((line) => JSON.parse(line));
      const stopPayloads = () =>
        readCalls()
          .filter((call) => Array.isArray(call.args) && call.args.includes("stop"))
          .map((call) => JSON.parse(call.payload));
      const waitForStops = async (n) => {{
        for (let i = 0; i < 200 && stopPayloads().length < n; i += 1) {{
          await new Promise((resolve) => setTimeout(resolve, 25));
        }}
      }};
      import({json.dumps(str(REPO_ROOT / "plugin" / "opencode" / "dist" / "server.mjs"))}).then(async (mod) => {{
        const queue = [];
        let wake = null;
        const push = (event) => {{
          queue.push(event);
          if (wake) {{ const w = wake; wake = null; w(); }}
        }};
        const hooks = {{}};
        const ctx = {{
          location: {{ directory: "/repo" }},
          sessionsOutliveDispose: true,
          session: {{ hook: async (name, fn) => {{ hooks[name] = fn; return {{ dispose() {{}} }}; }} }},
          tool: {{ hook: async () => ({{ dispose() {{}} }}) }},
          event: {{
            subscribe: () => (async function* () {{
              for (;;) {{
                if (!queue.length) await new Promise((resolve) => {{ wake = resolve; }});
                while (queue.length) yield queue.shift();
              }}
            }})(),
          }},
        }};
        const dispose = await mod.default.setup(ctx);
        // Same assistantMessageID across turns: text left in the buffer by
        // the first turn would be merged into the second.
        await hooks.prompt({{ sessionID: "s-end", prompt: {{ text: "fails" }} }});
        push({{ type: "session.text.ended", data: {{ sessionID: "s-end", assistantMessageID: "m1", ordinal: 0, text: "Partial" }} }});
        push({{ type: "session.execution.failed", data: {{ sessionID: "s-end" }} }});
        await waitForStops(1);
        await hooks.prompt({{ sessionID: "s-end", prompt: {{ text: "cancelled" }} }});
        push({{ type: "session.text.ended", data: {{ sessionID: "s-end", assistantMessageID: "m1", ordinal: 0, text: "Cut short" }} }});
        push({{ type: "session.execution.interrupted", data: {{ sessionID: "s-end" }} }});
        await waitForStops(2);
        await new Promise((resolve) => setTimeout(resolve, 200));
        const stops = stopPayloads();
        await dispose();
        process.stdout.write(JSON.stringify(stops));
      }}).catch((err) => {{
        console.error(err);
        process.exit(1);
      }});
    """

    result = _run_node_script(script)
    assert result is not None

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        {"session_id": "s-end", "cwd": "/repo", "last_assistant_message": "Partial"},
        {"session_id": "s-end", "cwd": "/repo", "last_assistant_message": "Cut short"},
    ]


def test_node_opencode_v2_reload_flushes_only_prompted_instance(
    tmp_path: Path,
) -> None:
    if shutil_which_node() is None:
        pytest.skip("node is not installed")
    log_path = tmp_path / "calls.jsonl"
    _write_fake_bash_recorder(tmp_path)
    script = f"""
      process.env.PATH = {json.dumps(str(tmp_path))} + ":" + process.env.PATH;
      process.env.CALL_LOG = {json.dumps(str(log_path))};
      const fs = require("fs");
      const readCalls = () =>
        fs.readFileSync(process.env.CALL_LOG, "utf8").trim()
          .split("\\n").filter(Boolean).map((line) => JSON.parse(line));
      const stopPayloads = () =>
        readCalls()
          .filter((call) => Array.isArray(call.args) && call.args.includes("stop"))
          .map((call) => JSON.parse(call.payload));
      const waitForStops = async (n) => {{
        for (let i = 0; i < 200 && stopPayloads().length < n; i += 1) {{
          await new Promise((resolve) => setTimeout(resolve, 25));
        }}
      }};
      import({json.dumps(str(REPO_ROOT / "plugin" / "opencode" / "dist" / "server.mjs"))}).then(async (mod) => {{
        const subscribers = [];
        const push = (event) => {{ for (const deliver of subscribers) deliver(event); }};
        const makeSubscribe = () => {{
          const queue = [];
          let wake = null;
          subscribers.push((event) => {{
            queue.push(event);
            if (wake) {{ const w = wake; wake = null; w(); }}
          }});
          return () => (async function* () {{
            for (;;) {{
              if (!queue.length) await new Promise((resolve) => {{ wake = resolve; }});
              while (queue.length) yield queue.shift();
            }}
          }})();
        }};
        let releaseDispose = null;
        const disposeGate = new Promise((resolve) => {{ releaseDispose = resolve; }});
        const makeCtx = (holdDispose) => {{
          const hooks = {{}};
          return {{
            hooks,
            ctx: {{
              location: {{ directory: "/repo" }},
              sessionsOutliveDispose: true,
              session: {{ hook: async (name, fn) => {{ hooks[name] = fn; return {{ dispose: () => (holdDispose ? disposeGate : undefined) }}; }} }},
              tool: {{ hook: async () => ({{ dispose() {{}} }}) }},
              event: {{ subscribe: makeSubscribe() }},
            }},
          }};
        }};
        const first = makeCtx(true);
        const second = makeCtx(false);
        const cleanupFirst = await mod.default.setup(first.ctx);
        await first.hooks.prompt({{ sessionID: "s-reload", prompt: {{ text: "only this instance got the prompt" }} }});
        // Real reload order: cleanup frees the registration slot synchronously,
        // then awaits the hook disposers. Hold one disposer open so the old
        // instance's event loop is still alive while the new instance sets up —
        // two live instances, only one of which received the prompt.
        const cleanupDone = cleanupFirst();
        await mod.default.setup(second.ctx);
        push({{ type: "session.text.ended", data: {{ sessionID: "s-reload", assistantMessageID: "m1", ordinal: 0, text: "Only once" }} }});
        push({{ type: "session.execution.succeeded", data: {{ sessionID: "s-reload" }} }});
        await waitForStops(1);
        push({{ type: "session.created", data: {{ sessionID: "s-created" }} }});
        await new Promise((resolve) => setTimeout(resolve, 300));
        const stops = stopPayloads();
        const prompts = readCalls().filter(
          (call) => Array.isArray(call.args) && call.args.includes("user-prompt"),
        );
        const sessionStarts = readCalls().filter(
          (call) => Array.isArray(call.args) && call.args.includes("session-start"),
        );
        const secondHookNames = Object.keys(second.hooks);
        releaseDispose();
        await cleanupDone;
        process.stdout.write(JSON.stringify({{ stops, promptCount: prompts.length, sessionStartCount: sessionStarts.length, secondHookNames }}));
      }}).catch((err) => {{
        console.error(err);
        process.exit(1);
      }});
    """

    result = _run_node_script(script)
    assert result is not None

    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    # Events are broadcast to every live instance, but only the instance that
    # received the prompt registered the session with V1 and may flush it, so
    # the second (unprompted) instance must stay silent.
    assert parsed["stops"] == [
        {
            "session_id": "s-reload",
            "cwd": "/repo",
            "last_assistant_message": "Only once",
        },
    ]
    assert parsed["promptCount"] == 1
    # session.created is broadcast too; only the newest instance may run its
    # session-start hook and service starts.
    assert parsed["sessionStartCount"] == 1
    # The overlap is real: the new instance registered while the old one was
    # still disposing.
    assert parsed["secondHookNames"]


def test_node_opencode_v2_second_setup_for_same_directory_registers_nothing(
    tmp_path: Path,
) -> None:
    if shutil_which_node() is None:
        pytest.skip("node is not installed")
    log_path = tmp_path / "calls.jsonl"
    _write_fake_bash_recorder(tmp_path)
    script = f"""
      process.env.PATH = {json.dumps(str(tmp_path))} + ":" + process.env.PATH;
      process.env.CALL_LOG = {json.dumps(str(log_path))};
      const fs = require("fs");
      const readCalls = () =>
        fs.readFileSync(process.env.CALL_LOG, "utf8").trim()
          .split("\\n").filter(Boolean).map((line) => JSON.parse(line));
      const calls = (name) => readCalls().filter((call) => Array.isArray(call.args) && call.args.includes(name));
      const waitFor = async (name, n) => {{
        for (let i = 0; i < 200 && calls(name).length < n; i += 1) {{
          await new Promise((resolve) => setTimeout(resolve, 25));
        }}
      }};
      import({json.dumps(str(REPO_ROOT / "plugin" / "opencode" / "dist" / "server.mjs"))}).then(async (mod) => {{
        const subscribers = [];
        const push = (event) => {{ for (const deliver of subscribers) deliver(event); }};
        const makeSubscribe = () => {{
          const queue = [];
          let wake = null;
          subscribers.push((event) => {{
            queue.push(event);
            if (wake) {{ const w = wake; wake = null; w(); }}
          }});
          return () => (async function* () {{
            for (;;) {{
              if (!queue.length) await new Promise((resolve) => {{ wake = resolve; }});
              while (queue.length) yield queue.shift();
            }}
          }})();
        }};
        const makeCtx = () => {{
          const hooks = {{}};
          return {{
            hooks,
            ctx: {{
              location: {{ directory: "/repo" }},
              sessionsOutliveDispose: true,
              session: {{ hook: async (name, fn) => {{ hooks[name] = fn; return {{ dispose() {{}} }}; }} }},
              tool: {{ hook: async () => ({{ dispose() {{}} }}) }},
              event: {{ subscribe: makeSubscribe() }},
            }},
          }};
        }};
        const first = makeCtx();
        const second = makeCtx();
        const firstCleanup = await mod.default.setup(first.ctx);
        const secondCleanup = await mod.default.setup(second.ctx);
        const secondHookNames = Object.keys(second.hooks);
        await first.hooks.prompt({{ sessionID: "s-twice", prompt: {{ text: "loaded twice" }} }});
        push({{ type: "session.text.ended", data: {{ sessionID: "s-twice", assistantMessageID: "m1", ordinal: 0, text: "Once" }} }});
        push({{ type: "session.execution.succeeded", data: {{ sessionID: "s-twice" }} }});
        await waitFor("stop", 1);
        // Disposing the duplicate must be a no-op: handing out the survivor's
        // cleanup would tear down the surviving hooks and stop capture.
        await secondCleanup();
        await first.hooks.prompt({{ sessionID: "s-twice", prompt: {{ text: "still capturing" }} }});
        push({{ type: "session.text.ended", data: {{ sessionID: "s-twice", assistantMessageID: "m2", ordinal: 0, text: "Twice" }} }});
        push({{ type: "session.execution.succeeded", data: {{ sessionID: "s-twice" }} }});
        await waitFor("stop", 2);
        process.stdout.write(JSON.stringify({{
          secondHookNames,
          sameCleanup: firstCleanup === secondCleanup,
          promptCount: calls("user-prompt").length,
          stopCount: calls("stop").length,
        }}));
      }}).catch((err) => {{
        console.error(err);
        process.exit(1);
      }});
    """

    result = _run_node_script(script)
    assert result is not None

    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    # The opencode.json entry and the plugins-directory loader resolve to this
    # same module, so OpenCode sets it up twice for one project; the second call
    # must reuse the first registration instead of publishing every turn twice,
    # and its cleanup must be a no-op that cannot tear the survivor down.
    assert parsed["secondHookNames"] == []
    assert parsed["sameCleanup"] is False
    assert parsed["promptCount"] == 2
    assert parsed["stopCount"] == 2


def test_node_opencode_v2_double_module_load_registers_once(
    tmp_path: Path,
) -> None:
    if shutil_which_node() is None:
        pytest.skip("node is not installed")
    log_path = tmp_path / "calls.jsonl"
    _write_fake_bash_recorder(tmp_path)
    copy_root = tmp_path / "copy-root"
    _write_minimal_plugin_root(copy_root)
    copy_server = _copy_opencode_dist(copy_root)
    script = f"""
      process.env.PATH = {json.dumps(str(tmp_path))} + ":" + process.env.PATH;
      process.env.CALL_LOG = {json.dumps(str(log_path))};
      delete process.env.CLAUDE_SMART_PLUGIN_ROOT;
      const fs = require("fs");
      const readCalls = () =>
        fs.readFileSync(process.env.CALL_LOG, "utf8").trim()
          .split("\\n").filter(Boolean).map((line) => JSON.parse(line));
      const calls = (name) => readCalls().filter((call) => Array.isArray(call.args) && call.args.includes(name));
      const waitFor = async (name, n) => {{
        for (let i = 0; i < 200 && calls(name).length < n; i += 1) {{
          await new Promise((resolve) => setTimeout(resolve, 25));
        }}
      }};
      Promise.all([
        import({json.dumps(str(REPO_ROOT / "plugin" / "opencode" / "dist" / "server.mjs"))}),
        import({json.dumps(str(copy_server))}),
      ]).then(async ([modA, modB]) => {{
        const subscribers = [];
        const push = (event) => {{ for (const deliver of subscribers) deliver(event); }};
        const makeSubscribe = () => {{
          const queue = [];
          let wake = null;
          subscribers.push((event) => {{
            queue.push(event);
            if (wake) {{ const w = wake; wake = null; w(); }}
          }});
          return () => (async function* () {{
            for (;;) {{
              if (!queue.length) await new Promise((resolve) => {{ wake = resolve; }});
              while (queue.length) yield queue.shift();
            }}
          }})();
        }};
        const makeCtx = () => {{
          const hooks = {{}};
          return {{
            hooks,
            ctx: {{
              location: {{ directory: "/repo" }},
              sessionsOutliveDispose: true,
              session: {{ hook: async (name, fn) => {{ hooks[name] = fn; return {{ dispose() {{}} }}; }} }},
              tool: {{ hook: async () => ({{ dispose() {{}} }}) }},
              event: {{ subscribe: makeSubscribe() }},
            }},
          }};
        }};
        const a = makeCtx();
        const b = makeCtx();
        const cleanupA = await modA.default.setup(a.ctx);
        const cleanupB = await modB.default.setup(b.ctx);
        await a.hooks.prompt({{ sessionID: "s-dup", prompt: {{ text: "loaded through two specs" }} }});
        push({{ type: "session.text.ended", data: {{ sessionID: "s-dup", assistantMessageID: "m1", ordinal: 0, text: "Once" }} }});
        push({{ type: "session.execution.succeeded", data: {{ sessionID: "s-dup" }} }});
        await waitFor("stop", 1);
        // Disposing the duplicate must not remove the survivor's hooks.
        await cleanupB();
        await a.hooks.prompt({{ sessionID: "s-dup", prompt: {{ text: "still live" }} }});
        push({{ type: "session.text.ended", data: {{ sessionID: "s-dup", assistantMessageID: "m2", ordinal: 0, text: "Twice" }} }});
        push({{ type: "session.execution.succeeded", data: {{ sessionID: "s-dup" }} }});
        await waitFor("stop", 2);
        process.stdout.write(JSON.stringify({{
          secondHookNames: Object.keys(b.hooks),
          sameCleanup: cleanupA === cleanupB,
          promptCount: calls("user-prompt").length,
          stopCount: calls("stop").length,
        }}));
      }}).catch((err) => {{
        console.error(err);
        process.exit(1);
      }});
    """

    result = _run_node_script(script)
    assert result is not None

    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    # Bun loads each entry spec as a separate module instance, so the
    # opencode.json entry and the plugins-directory loader each get their own
    # module state; the dedupe has to live on globalThis to span both, and the
    # duplicate's cleanup must be a no-op.
    assert parsed["secondHookNames"] == []
    assert parsed["sameCleanup"] is False
    assert parsed["promptCount"] == 2
    assert parsed["stopCount"] == 2


def test_node_opencode_v1_double_server_publishes_once(
    tmp_path: Path,
) -> None:
    if shutil_which_node() is None:
        pytest.skip("node is not installed")
    log_path = tmp_path / "calls.jsonl"
    _write_fake_bash_recorder(tmp_path)
    copy_root = tmp_path / "copy-root"
    _write_minimal_plugin_root(copy_root)
    copy_server = _copy_opencode_dist(copy_root)
    script = f"""
      process.env.PATH = {json.dumps(str(tmp_path))} + ":" + process.env.PATH;
      process.env.CALL_LOG = {json.dumps(str(log_path))};
      delete process.env.CLAUDE_SMART_PLUGIN_ROOT;
      const fs = require("fs");
      const readCalls = () =>
        fs.readFileSync(process.env.CALL_LOG, "utf8").trim()
          .split("\\n").filter(Boolean).map((line) => JSON.parse(line));
      const calls = (name) => readCalls().filter((call) => Array.isArray(call.args) && call.args.includes(name));
      Promise.all([
        import({json.dumps(str(REPO_ROOT / "plugin" / "opencode" / "dist" / "server.mjs"))}),
        import({json.dumps(str(copy_server))}),
      ]).then(async ([modA, modB]) => {{
        // OpenCode 1.x auto-loads the plugins directory too, and its
        // deduplicatePluginOrigins only drops exact spec matches, so the
        // config entry and the loader yield two server() instances whose
        // hooks all run and whose events are all delivered.
        const pluginA = await modA.default.server({{ directory: "/repo" }});
        const pluginB = await modB.default.server({{ directory: "/repo" }});
        const created = {{ type: "session.created", properties: {{ sessionID: "s-v1", info: {{ directory: "/repo" }} }} }};
        await pluginA.event({{ event: created }});
        await pluginB.event({{ event: created }});
        await pluginA["chat.message"]({{ sessionID: "s-v1" }}, {{ message: {{ content: "double load" }} }});
        await pluginB["chat.message"]({{ sessionID: "s-v1" }}, {{ message: {{ content: "double load" }} }});
        const idle = {{ type: "session.idle", properties: {{ sessionID: "s-v1" }} }};
        await pluginA.event({{ event: idle }});
        await pluginB.event({{ event: idle }});
        // The superseded instance's dispose must not stop services the
        // surviving instance still needs; the survivor's dispose runs in full.
        await pluginA.dispose();
        await pluginB.dispose();
        process.stdout.write(JSON.stringify({{
          sessionStarts: calls("session-start").length,
          prompts: calls("user-prompt").length,
          stops: calls("stop").length,
          sessionEnds: calls("session-end").length,
        }}));
      }}).catch((err) => {{
        console.error(err);
        process.exit(1);
      }});
    """

    result = _run_node_script(script)
    assert result is not None

    assert result.returncode == 0, result.stderr
    # One publish per turn: the newest server() instance per directory wins
    # and earlier instances go inert, including on dispose.
    assert json.loads(result.stdout) == {
        "sessionStarts": 1,
        "prompts": 1,
        "stops": 1,
        "sessionEnds": 2,
    }


def test_node_opencode_v1_superseded_server_buffers_nothing(
    tmp_path: Path,
) -> None:
    if shutil_which_node() is None:
        pytest.skip("node is not installed")
    log_path = tmp_path / "calls.jsonl"
    _write_fake_bash_recorder(tmp_path)
    script = f"""
      process.env.PATH = {json.dumps(str(tmp_path))} + ":" + process.env.PATH;
      process.env.CALL_LOG = {json.dumps(str(log_path))};
      import({json.dumps(str(REPO_ROOT / "plugin" / "opencode" / "dist" / "server.mjs"))}).then(async (mod) => {{
        const first = await mod.default.server({{ directory: "/repo" }});
        const second = await mod.default.server({{ directory: "/repo" }});
        // The buffers are private, so count every Map write keyed by the
        // session while only the superseded instance handles a full turn.
        const realSet = Map.prototype.set;
        let writes = 0;
        Map.prototype.set = function (key, value) {{
          if (key === "s-aside") writes += 1;
          return realSet.call(this, key, value);
        }};
        await first["chat.message"]({{ sessionID: "s-aside" }}, {{ message: {{ content: "not mine" }} }});
        await first.event({{ event: {{ type: "message.updated", properties: {{ sessionID: "s-aside", info: {{ id: "m1", role: "assistant" }} }} }} }});
        await first.event({{ event: {{ type: "message.part.updated", properties: {{ sessionID: "s-aside", part: {{ id: "p1", messageID: "m1", type: "text", text: "leak" }} }} }} }});
        await first["experimental.text.complete"]({{ sessionID: "s-aside" }}, {{ text: "leak" }});
        await first.event({{ event: {{ type: "session.idle", properties: {{ sessionID: "s-aside" }} }} }});
        Map.prototype.set = realSet;
        await first.dispose();
        await second.dispose();
        process.stdout.write(JSON.stringify({{ writes }}));
      }}).catch((err) => {{
        console.error(err);
        process.exit(1);
      }});
    """

    result = _run_node_script(script)
    assert result is not None

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"writes": 0}


def test_node_opencode_v2_concurrent_setups_register_once(
    tmp_path: Path,
) -> None:
    if shutil_which_node() is None:
        pytest.skip("node is not installed")
    log_path = tmp_path / "calls.jsonl"
    _write_fake_bash_recorder(tmp_path)
    script = f"""
      process.env.PATH = {json.dumps(str(tmp_path))} + ":" + process.env.PATH;
      process.env.CALL_LOG = {json.dumps(str(log_path))};
      const fs = require("fs");
      const readCalls = () =>
        fs.readFileSync(process.env.CALL_LOG, "utf8").trim()
          .split("\\n").filter(Boolean).map((line) => JSON.parse(line));
      const calls = (name) => readCalls().filter((call) => Array.isArray(call.args) && call.args.includes(name));
      const waitFor = async (name, n) => {{
        for (let i = 0; i < 200 && calls(name).length < n; i += 1) {{
          await new Promise((resolve) => setTimeout(resolve, 25));
        }}
      }};
      import({json.dumps(str(REPO_ROOT / "plugin" / "opencode" / "dist" / "server.mjs"))}).then(async (mod) => {{
        const subscribers = [];
        const push = (event) => {{ for (const deliver of subscribers) deliver(event); }};
        const makeSubscribe = () => {{
          const queue = [];
          let wake = null;
          subscribers.push((event) => {{
            queue.push(event);
            if (wake) {{ const w = wake; wake = null; w(); }}
          }});
          return () => (async function* () {{
            for (;;) {{
              if (!queue.length) await new Promise((resolve) => {{ wake = resolve; }});
              while (queue.length) yield queue.shift();
            }}
          }})();
        }};
        const makeCtx = () => {{
          const hooks = {{}};
          return {{
            hooks,
            ctx: {{
              location: {{ directory: "/repo" }},
              sessionsOutliveDispose: true,
              session: {{ hook: async (name, fn) => {{ hooks[name] = fn; return {{ dispose() {{}} }}; }} }},
              tool: {{ hook: async () => ({{ dispose() {{}} }}) }},
              event: {{ subscribe: makeSubscribe() }},
            }},
          }};
        }};
        const a = makeCtx();
        const b = makeCtx();
        // Two setups racing before the first one finishes must not both
        // register: the slot is claimed synchronously, before any await.
        await Promise.all([mod.default.setup(a.ctx), mod.default.setup(b.ctx)]);
        const registered = [a, b].filter((inst) => Object.keys(inst.hooks).length > 0);
        await registered[0].hooks.prompt({{ sessionID: "s-race", prompt: {{ text: "raced" }} }});
        push({{ type: "session.text.ended", data: {{ sessionID: "s-race", assistantMessageID: "m1", ordinal: 0, text: "Raced" }} }});
        push({{ type: "session.execution.succeeded", data: {{ sessionID: "s-race" }} }});
        await waitFor("stop", 1);
        process.stdout.write(JSON.stringify({{
          registeredCount: registered.length,
          promptCount: calls("user-prompt").length,
          stopCount: calls("stop").length,
        }}));
      }}).catch((err) => {{
        console.error(err);
        process.exit(1);
      }});
    """

    result = _run_node_script(script)
    assert result is not None

    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed["registeredCount"] == 1
    assert parsed["promptCount"] == 1
    assert parsed["stopCount"] == 1


def test_opencode_dist_files_are_packaged() -> None:
    package = _read_json("package.json")

    assert "plugin" in package["files"]
    assert package["exports"]["./server"]["import"] == "./plugin/opencode/dist/server.mjs"


def test_opencode_dist_matches_typescript_sources(tmp_path: Path) -> None:
    import filecmp
    import shutil

    if shutil.which("npx") is None:
        pytest.skip("npx is required to recompile the opencode TypeScript sources")
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
    # tsc writes diagnostics to stdout, not stderr, so reporting stderr alone
    # produces an empty message. Surface both, and name the usual cause: the
    # devDependencies (@types/node, typescript) were never installed.
    assert result.returncode == 0, (
        f"npx tsc failed (exit {result.returncode}); "
        f"run `npm ci` if @types/node is missing.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    for filename in ("assistant-buffer.js", "internal.js", "payload.js", "server.mjs", "v2.js"):
        expected = REPO_ROOT / "plugin" / "opencode" / "dist" / filename
        generated = out_dir / filename
        assert filecmp.cmp(expected, generated, shallow=False), filename


def test_node_parse_host_accepts_supported_hosts() -> None:
    if shutil_which_node() is None:
        pytest.skip("node is not installed")
    script = (
        f"const installer = require({json.dumps(str(REPO_ROOT / 'bin' / 'claude-smart.js'))});"
        "const originalExit = process.exit;"
        "process.exit = (code) => { throw new Error(`exit ${code}`); };"
        "let rejectsInvalid = false;"
        "try { installer.parseHost(['--host', 'bad-host']); }"
        "catch (err) { rejectsInvalid = err.message === 'exit 1'; }"
        "process.exit = originalExit;"
        "process.stdout.write(JSON.stringify({"
        "  defaults: installer.parseHost([]),"
        "  codex: installer.parseHost(['--host', 'codex']),"
        "  opencode: installer.parseHost(['--host', 'opencode']),"
        "  rejectsInvalid,"
        "}));"
    )

    result = _run_node_script(script)

    assert result is not None
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "defaults": "claude-code",
        "codex": "codex",
        "opencode": "opencode",
        "rejectsInvalid": True,
    }


def shutil_which_node() -> str | None:
    import shutil

    return shutil.which("node")
