"""Tests for OpenCode host support."""

# Contract subprocesses execute repository code or fixture scripts in owned homes.
# ruff: noqa: S603, S607

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import textwrap
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest  # type: ignore[reportMissingImports]
from claude_smart import hook, runtime, state
from claude_smart.events import post_tool, stop, user_prompt
from dotenv import dotenv_values

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
        'script="$1"\n'
        "shift\n"
        "payload=$(cat || true)\n"
        'PAYLOAD="$payload" node -e \'require("fs").appendFileSync(process.env.CALL_LOG, JSON.stringify({ script: process.argv[1], args: process.argv.slice(2), payload: process.env.PAYLOAD, learningHost: process.env.CLAUDE_SMART_HOST }) + "\\n")\' "$script" "$@"\n'
        'if [ "${2:-}" = "user-prompt" ]; then\n'
        f"  printf '%s\\n' '{response}'\n"
        "else\n"
        "  printf '%s\\n' '{}'\n"
        "fi\n",
    )
    fake_bash.chmod(0o755)
    return fake_bash


def _write_minimal_plugin_root(root: Path, *, name: str = "claude-smart") -> None:
    (root / "scripts").mkdir(parents=True)
    (root / "pyproject.toml").write_text(f'[project]\nname = "{name}"\n')
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

    assert (
        package["exports"]["./server"]["import"] == "./plugin/opencode/dist/server.mjs"
    )
    assert "build:opencode" in package["scripts"]
    assert "opencode" in package["keywords"]
    assert "opencode-plugin" in package["keywords"]
    assert package["engines"]["opencode"] == ">=1.17.0"
    assert package["description"].startswith(
        "Self-improving Claude Code, Codex, and OpenCode"
    )
    assert package["oc-plugin"] == ["server"]


def test_opencode_package_includes_local_cli_bridge() -> None:
    for name in (
        "opencode-claude-compat",
        "opencode-claude-compat.cmd",
        "opencode-claude-compat.js",
    ):
        assert (REPO_ROOT / "plugin" / "scripts" / name).is_file()


def test_opencode_hook_bootstrap_lockfile_matches_pyproject() -> None:
    for prerequisite in ("uv", "bash", "rsync"):
        if shutil.which(prerequisite) is None:
            pytest.skip(f"{prerequisite} is not installed")

    # Check the customer project outside any enclosing enterprise workspace.
    result = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts/standalone-lock.sh"), "--check"],
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
        lambda deadline, **kw: published.append(kw) or ("ok", 3),  # noqa: ARG005 -- Required named callback argument.
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


def test_opencode_backend_service_uses_bridge_without_opencode_path_probe() -> None:
    service = (REPO_ROOT / "plugin" / "scripts" / "backend-service.sh").read_text()

    assert 'if [ "$learning_provider" = "opencode" ]' in service
    assert (
        'CLAUDE_SMART_CLI_PATH="$(claude_smart_opencode_compat_path "$PLUGIN_ROOT")"'
        in service
    )
    assert "command -v opencode" not in service
    assert 'export CLAUDE_SMART_HOST="$learning_provider"' in service


def test_opencode_server_resolves_windows_bash_before_spawning() -> None:
    server = (REPO_ROOT / "plugin" / "opencode" / "server.mts").read_text()

    assert "function commandPath(names: string[]): string | undefined" in server
    assert "WINDOWS_SYSTEM_BASH_SUFFIXES" in server
    assert "isWindowsSystemBash" in server
    assert '"C:\\\\Program Files\\\\Git\\\\bin\\\\bash.exe"' in server
    assert 'pathCommandCandidates(["bash.exe", "bash"])' in server
    assert "const RESOLVED_BASH = bashPath()" in server
    assert 'spawn(RESOLVED_BASH || "bash"' in server


@pytest.mark.parametrize(
    (
        "requested_model",
        "saved_model",
        "retired_name",
        "expected_model",
        "expected_variant",
        "modern",
    ),
    [
        ("claude-sonnet-4-6", None, None, None, None, False),
        ("provider/explicit", None, None, "provider/explicit", None, False),
        ("provider/explicit", "provider/saved", None, "provider/saved", None, False),
        (
            "provider/explicit",
            "provider/saved#high",
            None,
            "provider/saved",
            "high",
            False,
        ),
        ("provider/explicit", "#low", None, None, "low", False),
        ("#medium", None, None, None, "medium", False),
        ("provider/explicit", None, "CLAUDE_SMART_OPENCODE_MODEL", None, None, False),
        (
            "provider/explicit",
            "#high",
            "CLAUDE_SMART_OPENCODE_VARIANT",
            None,
            None,
            False,
        ),
        (
            "provider/explicit",
            "provider/saved#high",
            None,
            "provider/saved#high",
            None,
            True,
        ),
        ("provider/explicit", "#high", None, None, None, True),
    ],
)
def test_opencode_cli_bridge_translates_claude_contract(
    tmp_path: Path,
    requested_model: str,
    saved_model: str | None,
    retired_name: str | None,
    expected_model: str | None,
    expected_variant: str | None,
    modern: bool,
) -> None:
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
if (args[1] === "--help") {{
  process.stderr.write({json.dumps("--standalone" if modern else "--variant")});
  process.exit(0);
}}
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
    for key, value in (
        ("CLAUDE_SMART_LEARNING_MODEL", saved_model),
        (
            "CLAUDE_SMART_OPENCODE_MODEL",
            "private-retired" if retired_name == "CLAUDE_SMART_OPENCODE_MODEL" else "",
        ),
        (
            "CLAUDE_SMART_OPENCODE_VARIANT",
            "private-retired"
            if retired_name == "CLAUDE_SMART_OPENCODE_VARIANT"
            else "",
        ),
    ):
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value

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
            requested_model,
        ],
        input="user prompt",
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    if retired_name:
        assert result.returncode != 0
        assert f"{retired_name} has been retired" in result.stderr
        assert "private-retired" not in result.stderr
        assert not call_log.exists()
        return

    if modern and saved_model == "#high":
        assert result.returncode != 0
        assert "compatible variant option" in result.stderr
        assert not call_log.exists()
        return

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
    subprocess_pwd = Path(call["env"]["PWD"])
    assert subprocess_pwd.is_absolute()
    assert subprocess_pwd.resolve() == Path(call["cwd"]).resolve()
    assert call["dotConfigExists"] is True
    assert call["rootConfigExists"] is False
    if expected_model is None:
        assert "--model" not in call["args"]
    else:
        assert call["args"][call["args"].index("--model") + 1] == expected_model
    variant = [arg for arg in call["args"] if arg.startswith("--variant")]
    assert variant == ([f"--variant={expected_variant}"] if expected_variant else [])
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
        'process.stdout.write(\'{"type":"text","part":{"text":"SECRET-CUT\');\n'
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
        'process.stdout.write(\'[{"type":"text","part":{"text":"SECRET-CUT\');\n'
        "process.exit(1);\n"
    )
    result = run_bridge()
    assert result.returncode == 1
    assert "[ERROR] Provider unavailable" in result.stderr
    assert "SECRET" not in result.stderr


def test_node_installer_accepts_opencode_only_extraction_provider(
    tmp_path: Path,
) -> None:
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


@pytest.mark.parametrize("saved", [False, True])
def test_public_confirmation_preserves_or_discovers_opencode_path(
    tmp_path: Path, saved: bool
) -> None:
    from tests.test_install_scripts import _run_public_setup

    executable = _fake_opencode_path(tmp_path)
    selected = tmp_path / ".claude-smart/.env"
    if saved:
        selected.parent.mkdir()
        selected.write_text(
            'REFLEXIO_URL="http://localhost:8071/"\nREFLEXIO_API_KEY=""\n'
            + f'CLAUDE_SMART_OPENCODE_PATH="{executable}"\nCLAUDE_SMART_OPENCODE_PATH="/stale/other"\n'
        )
    result = _run_public_setup(
        tmp_path,
        "--backend",
        "local",
        "--provider",
        "opencode",
        env_extra={} if saved else {"CLAUDE_SMART_OPENCODE_PATH": str(executable)},
    )
    assert result.returncode == 0, result.stderr
    if saved:
        # Existing caller-first duplicate rows retain their exact bytes/order.
        assert (
            f'CLAUDE_SMART_OPENCODE_PATH="{executable}"\n'
            'CLAUDE_SMART_OPENCODE_PATH="/stale/other"\n'
        ) in selected.read_text()
    else:
        assert dotenv_values(selected, interpolate=False)[
            "CLAUDE_SMART_OPENCODE_PATH"
        ] == str(executable)
    assert not (tmp_path / ".reflexio/.env").exists()


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
    from tests.test_install_scripts import _seed_prepared_policy

    _seed_prepared_policy(Path(env["HOME"]), "opencode")
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
    from tests.test_install_scripts import _copy_prepared_policy_owner

    _copy_prepared_policy_owner(package_root / "plugin")
    smart_install = scripts / "smart-install.sh"
    smart_install.write_text("#!/bin/sh\nexit 0\n")
    smart_install.chmod(0o755)


def _seed_private_node_and_uv(env: dict[str, str]) -> None:
    home = Path(env["HOME"])
    private_node = home / ".claude-smart" / "node" / "current" / "bin"
    uv_bin = home / ".local" / "bin"
    private_node.mkdir(parents=True, exist_ok=True)
    uv_bin.mkdir(parents=True, exist_ok=True)
    npm = "npm.cmd" if env.get("CLAUDE_SMART_TEST_PLATFORM") == "win32" else "npm"
    for path in [private_node / "node", private_node / npm]:
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    from tests.test_install_scripts import _write_bootstrap_uv

    _write_bootstrap_uv(uv_bin / "uv", mode="fresh")


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
    assert (
        local_package
        == Path(env["HOME"]) / ".claude-smart" / "opencode" / "claude-smart"
    )
    assert (local_package / "plugin" / "scripts" / "smart-install.sh").exists()
    # A project install must not write the user-wide loader: OpenCode loads that
    # directory in every project, which would widen capture beyond this project.
    assert not (
        Path(env["XDG_CONFIG_HOME"]) / "opencode" / "plugins" / "claude-smart.js"
    ).exists()


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
    assert (
        "Git Bash is required for claude-smart OpenCode support on Windows"
        in result.stderr
    )
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
    assert (
        "Git Bash is required for claude-smart OpenCode support on Windows"
        in parsed["withSystemBash"]
    )
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
    # Keep host-tool discovery isolated while the real-owner UV fixture uses
    # these ordinary shell utilities to prepare its interpreter wrapper.
    for command in ("mkdir", "cat", "cp", "chmod"):
        executable = shutil.which(command)
        assert executable is not None
        (fake_bin / command).symlink_to(executable)
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
    # The simulated installer publishes Windows ';' PATH entries, while this
    # owned UV stand-in is a POSIX shell. Give only that stand-in its native
    # utility path; production platform selection and tool discovery stay tested.
    uv = home / ".local" / "bin" / "uv"
    uv.write_text(
        uv.read_text().replace(
            "#!/bin/sh\n",
            f"#!/bin/sh\nPATH={shlex.quote(str(fake_bin))}\nexport PATH\n",
            1,
        )
    )
    for env_path in [home / ".reflexio" / ".env", home / ".claude-smart" / ".env"]:
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(
            "# existing host state\n"
            'REFLEXIO_URL="http://localhost:8071/"\n'
            'REFLEXIO_API_KEY=""\n'
            "CLAUDE_SMART_HOST=codex\n"
            'CLAUDE_SMART_READ_ONLY="1"\n'
        )
    claude_cache = (
        home
        / ".claude"
        / "plugins"
        / "cache"
        / "reflexioai"
        / "claude-smart"
        / "0.2.46"
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
        '[plugins."claude-smart@reflexioai"]\nenabled = true\nsource = "reflexioai"\n'
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
    original = (
        "# existing host state\n"
        'REFLEXIO_URL="http://localhost:8071/"\n'
        'REFLEXIO_API_KEY=""\n'
        "CLAUDE_SMART_HOST=codex\n"
        'CLAUDE_SMART_READ_ONLY="1"\n'
    )
    # Private preparation can append missing defaults, never choose a new
    # learning target or publish an executable path without confirmation.
    assert runtime_env.startswith(original)
    assert "CLAUDE_SMART_HOST=opencode" not in runtime_env
    assert "CLAUDE_SMART_OPENCODE_PATH=" not in runtime_env
    assert "CLAUDE_SMART_USE_LOCAL_CLI=1" in runtime_env
    assert "REFLEXIO_ROW_LIMIT_INTERACTIONS=500" in runtime_env
    # ~/.reflexio/.env belongs to other Reflexio tools and is never rewritten.
    assert (home / ".reflexio" / ".env").read_text() == original
    assert (claude_cache / "keep.txt").read_text() == "keep\n"
    assert (codex_cache / "keep.txt").read_text() == "keep\n"
    assert codex_config.read_text() == (
        '[plugins."claude-smart@reflexioai"]\nenabled = true\nsource = "reflexioai"\n'
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
    config.write_text(
        json.dumps(
            {
                "plugin": [
                    "other",
                    "claude-smart",
                ]
            }
        )
    )
    script = (
        f"const installer = require({json.dumps(str(REPO_ROOT / 'bin' / 'claude-smart.js'))});"
        "const fs = require('node:fs');"
        "const current = JSON.parse(fs.readFileSync(process.argv[1], 'utf8'));"
        "current.plugin.push(installer.opencodeLocalPluginSpec());"
        "fs.writeFileSync(process.argv[1], JSON.stringify(current));"
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
        real_package.as_uri(),
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
    assert parsed["plugin"] == [
        "other",
        ["tuple-plugin", {"enabled": True}],
        local_package.as_uri(),
    ]
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
    local_package = Path(env["HOME"]) / ".claude-smart" / "opencode" / "claude-smart"
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
    local_package = Path(env["HOME"]) / ".claude-smart" / "opencode" / "claude-smart"
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


def test_node_installer_uninstall_migrates_existing_plugins_field(
    tmp_path: Path,
) -> None:
    if shutil_which_node() is None:
        pytest.skip("node is not installed")
    config = tmp_path / "opencode.json"
    config.write_text(
        json.dumps({"plugins": ["claude-smart", "other", "claude-smart"]})
    )
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
        'process.stdout.write(installer.stripJsonc(\'{"prompt":"keep,}","plugin":["claude-smart",],}\\n\'));'
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
        'process.stdout.write(installer.stripJsonc(\'{"plugin":["other", // note\\n], /* end */}\\n\'));'
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
    assert parsed["patch"]["tool_response"] == {
        "output": "patched",
        "stdout": "patched",
    }
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
        'if [ "${3:-}" = "user-prompt" ]; then\n'
        '  printf \'%s\\n\' \'{"hookSpecificOutput":{"additionalContext":"learned context"}}\'\n'
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
        "const again = installer.writeOpenCodeV2Plugin([], "
        + json.dumps(str(package))
        + ", project);"
        "require('fs').writeFileSync(written.path, '// claude-smart OpenCode V2 loader\\nuser edit\\n');"
        "const spared = installer.removeOpenCodeV2Plugin([], project);"
        "require('fs').writeFileSync(written.path, 'other plugin\\n');"
        "const conflict = installer.writeOpenCodeV2Plugin([], "
        + json.dumps(str(package))
        + ", project);"
        "process.stdout.write(JSON.stringify({ written: written.path, globalWritten: globalWritten.path, kept, keptGlobal, spared, conflict: conflict.status, still: require('fs').readFileSync(written.path, 'utf8') }));"
    )
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    # A project install stays inside the project; only --global writes the user-wide
    # loader that OpenCode loads in every project.
    assert (
        Path(parsed["written"]) == project / ".opencode" / "plugins" / "claude-smart.js"
    )
    assert (
        Path(parsed["globalWritten"])
        == tmp_path / "xdg" / "opencode" / "plugins" / "claude-smart.js"
    )
    assert parsed["kept"] is True
    assert parsed["keptGlobal"] is True
    assert parsed["spared"] is False
    assert parsed["conflict"] == "conflict"
    assert parsed["still"] == "other plugin\n"


def test_opencode_loader_preserves_file_created_during_preparation(tmp_path: Path):
    script = f"""
        const fs = require("node:fs");
        const assert = require("node:assert/strict");
        const {{ join }} = require("node:path");
        const installer = require({json.dumps(str(REPO_ROOT / "bin/claude-smart.js"))});
        const project = {json.dumps(str(tmp_path))};
        const directory = join(project, ".opencode", "plugins");
        const target = join(directory, "claude-smart.js");
        const installed = installer.writeOpenCodeV2Plugin([], project, project, () => {{
            assert.ok(!fs.existsSync(target));
            fs.writeFileSync(target, "foreign loader", {{flag: "wx"}});
        }});
        assert.equal(installed.status, "conflict");
        assert.equal(fs.readFileSync(target, "utf8"), "foreign loader");
        assert.deepEqual(fs.readdirSync(directory), ["claude-smart.js"]);
    """
    result = _run_node_script(script)
    if result is None:
        pytest.skip("node is not installed")
    assert result.returncode == 0, result.stderr


def test_opencode_dangling_loader_blocks_configuration_changes(tmp_path: Path):
    directory = tmp_path / ".opencode" / "plugins"
    directory.mkdir(parents=True)
    target = directory / "claude-smart.js"
    missing = tmp_path / "missing.js"
    try:
        target.symlink_to(missing)
    except OSError as err:
        if os.name == "nt" and getattr(err, "winerror", None) == 1314:
            pytest.skip("Windows runner lacks permission to create symlinks")
        raise
    script = f"""
        const assert = require("node:assert/strict");
        const installer = require({json.dumps(str(REPO_ROOT / "bin/claude-smart.js"))});
        const project = {json.dumps(str(tmp_path))};
        assert.match(installer.openCodeV2LoaderConflicts([], project), /cannot be read/);
        const result = installer.writeOpenCodeV2Plugin([], project, project, () => {{
            assert.fail("blocked loader must be detected before config changes");
        }});
        assert.equal(result.status, "conflict");
    """
    result = _run_node_script(script)
    if result is None:
        pytest.skip("node is not installed")
    assert result.returncode == 0, result.stderr
    assert list(directory.iterdir()) == [target]
    assert target.readlink() == missing
    assert not missing.exists()


def test_opencode_unreadable_loader_bytes_block_config_changes(tmp_path: Path):
    script = f"""
        const fs = require("node:fs");
        const assert = require("node:assert/strict");
        const {{ join }} = require("node:path");
        const installer = require({json.dumps(str(REPO_ROOT / "bin/claude-smart.js"))});
        const project = {json.dumps(str(tmp_path))};
        const original = installer.writeOpenCodeV2Plugin([], project, project);
        const corrupt = fs.readFileSync(original.path);
        const leaf = corrupt.indexOf("server.mjs");
        assert.ok(leaf > 0);
        corrupt[leaf] = 0xff;
        fs.writeFileSync(original.path, corrupt);
        const result = installer.writeOpenCodeV2Plugin([], join(project, "pkg"), project, () => {{
            assert.fail("invalid loader bytes must be detected before config changes");
        }});
        assert.equal(result.status, "conflict");
        assert.deepEqual(fs.readFileSync(original.path), corrupt);
    """
    result = _run_node_script(script)
    if result is None:
        pytest.skip("node is not installed")
    assert result.returncode == 0, result.stderr


def test_opencode_loader_refresh_updates_existing_file_in_place(tmp_path: Path):
    script = f"""
        const fs = require("node:fs");
        const assert = require("node:assert/strict");
        const {{ join }} = require("node:path");
        const {{ pathToFileURL }} = require("node:url");
        const installer = require({json.dumps(str(REPO_ROOT / "bin/claude-smart.js"))});
        const project = {json.dumps(str(tmp_path))};
        const oldRoot = join(project, "much-longer-original-package-path");
        const newRoot = join(project, "pkg");
        const original = installer.writeOpenCodeV2Plugin([], oldRoot, project);
        assert.equal(original.status, "wrote");
        const identity = fs.statSync(original.path).ino;
        const refreshed = installer.writeOpenCodeV2Plugin([], newRoot, project);
        assert.equal(refreshed.status, "wrote");
        assert.equal(fs.statSync(original.path).ino, identity);
        const body = fs.readFileSync(original.path, "utf8");
        const entry = pathToFileURL(join(newRoot, "plugin", "opencode", "dist", "server.mjs")).href;
        assert.equal(body.split("\\n").at(-2), `export {{ default }} from ${{JSON.stringify(entry)}};`);
    """
    result = _run_node_script(script)
    if result is None:
        pytest.skip("node is not installed")
    assert result.returncode == 0, result.stderr


def test_node_opencode_server_injects_cached_context_for_session_ids(
    tmp_path: Path,
) -> None:
    if shutil_which_node() is None:
        pytest.skip("node is not installed")
    fake_bash = tmp_path / "bash"
    fake_bash.write_text(
        "#!/bin/sh\n"
        "cat >/dev/null || true\n"
        'if [ "${3:-}" = "user-prompt" ]; then\n'
        '  printf \'%s\\n\' \'{"hookSpecificOutput":{"additionalContext":"learned context"}}\'\n'
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
      process.env.CLAUDE_SMART_HOST = "codex";
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
    command_pairs = [
        (Path(call["script"]).name, call["args"]) for call in parsed["calls"]
    ]
    assert ("backend-service.sh", ["start"]) in command_pairs
    assert ("dashboard-service.sh", ["start"]) in command_pairs
    assert ("hook_entry.sh", ["opencode", "session-start"]) in command_pairs
    assert ("hook_entry.sh", ["opencode", "user-prompt"]) in command_pairs
    assert ("hook_entry.sh", ["opencode", "post-tool"]) in command_pairs
    assert ("hook_entry.sh", ["opencode", "stop"]) in command_pairs
    assert ("dashboard-service.sh", ["session-end"]) in command_pairs
    assert ("backend-service.sh", ["session-end"]) in command_pairs
    for call in parsed["calls"]:
        is_hook = Path(call["script"]).name == "hook_entry.sh"
        assert call["learningHost"] == ("codex" if is_hook else "opencode")
    stop_calls = [
        json.loads(call["payload"])
        for call in parsed["calls"]
        if Path(call["script"]).name == "hook_entry.sh"
        and call["args"] == ["opencode", "stop"]
    ]
    assert stop_calls == [
        {
            "session_id": "s-life",
            "cwd": "/repo",
            "last_assistant_message": "final answer",
        }
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
        if Path(call["script"]).name == "hook_entry.sh"
        and call["args"] == ["opencode", "stop"]
    ]
    assert stop_calls == [
        {
            "session_id": "s-dispose",
            "cwd": "/repo",
            "last_assistant_message": "remembered",
        }
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
        if Path(call["script"]).name == "hook_entry.sh"
        and call["args"] == ["opencode", "stop"]
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
        if Path(call["script"]).name == "hook_entry.sh"
        and call["args"] == ["opencode", "stop"]
    ]
    assert stop_calls == [
        {
            "session_id": "s-multi",
            "cwd": "/repo",
            "last_assistant_message": "first\n\nsecond",
        }
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
              while (queue.length) {{
                const event = queue.shift();
                if (typeof event === "function") event();
                else yield event;
              }}
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
        // Cleanup frees the slot and parks the old turn before awaiting
        // hook disposal. Hold a disposer open while the new instance adopts
        // the turn and handles its terminal event.
        const cleanupDone = cleanupFirst();
        await mod.default.setup(second.ctx);
        push({{ type: "session.text.ended", data: {{ sessionID: "s-reload", assistantMessageID: "m1", ordinal: 0, text: "Only once" }} }});
        push({{ type: "session.execution.succeeded", data: {{ sessionID: "s-reload" }} }});
        await waitForStops(1);
        push({{ type: "session.created", data: {{ sessionID: "s-created" }} }});
        // The active stream must finish handling the broadcast before counting hooks.
        await new Promise(subscribers.at(-1));
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
    # The adopted turn publishes through the original prompt's V1 hooks once.
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


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        # The turn is open across the reload: the reloaded instance adopts it
        # and publishes it once, complete, at its terminal event.
        ("mid_turn", ["Before reload\n\nAfter reload"]),
        # Two reloads inside one turn: the adopter parks the turn again.
        ("mid_turn_twice", ["Before reload\n\nAfter reload"]),
        # A prompt reaching the new instance for an adopted session publishes
        # the adopted turn first; the new turn publishes at its own end.
        ("prompt_after_reload", ["Before reload", "After reload"]),
        # No turn open at the reload: nothing is parked or republished.
        ("idle", ["Before reload"]),
        # Control: the same events with no reload at all.
        ("no_reload", ["Before reload\n\nAfter reload"]),
    ],
)
def test_node_opencode_v2_reload_mid_turn_publishes_once(
    tmp_path: Path, scenario: str, expected: list[str]
) -> None:
    if shutil_which_node() is None:
        pytest.skip("node is not installed")
    log_path = tmp_path / "calls.jsonl"
    _write_fake_bash_recorder(tmp_path)
    script = f"""
      process.env.PATH = {json.dumps(str(tmp_path))} + ":" + process.env.PATH;
      process.env.CALL_LOG = {json.dumps(str(log_path))};
      const scenario = {json.dumps(scenario)};
      const fs = require("fs");
      const readCalls = () =>
        fs.existsSync(process.env.CALL_LOG)
          ? fs.readFileSync(process.env.CALL_LOG, "utf8").trim()
              .split("\\n").filter(Boolean).map((line) => JSON.parse(line))
          : [];
      const stopPayloads = () =>
        readCalls()
          .filter((call) => Array.isArray(call.args) && call.args.includes("stop"))
          .map((call) => JSON.parse(call.payload));
      const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
      const waitForStops = async (n) => {{
        for (let i = 0; i < 200 && stopPayloads().length < n; i += 1) await sleep(25);
      }};
      import({json.dumps(str(REPO_ROOT / "plugin" / "opencode" / "dist" / "server.mjs"))}).then(async (mod) => {{
        const subscribers = new Set();
        const push = (event) => {{ for (const deliver of subscribers) deliver(event); }};
        // OpenCode closes a plugin's event iterators when its scope closes,
        // right after awaiting the cleanup, and loads the new instance only
        // then: the old stream never sees an event published after cleanup.
        const subscribe = ({{ signal }}) => (async function* () {{
          const queue = [];
          let wake = null;
          const deliver = (event) => {{
            queue.push(event);
            if (wake) {{ const w = wake; wake = null; w(); }}
          }};
          subscribers.add(deliver);
          const closed = new Promise((resolve) => signal.addEventListener("abort", () => resolve("closed"), {{ once: true }}));
          try {{
            for (;;) {{
              if (!queue.length && (await Promise.race([new Promise((resolve) => {{ wake = resolve; }}), closed])) === "closed") return;
              while (queue.length) yield queue.shift();
            }}
          }} finally {{
            subscribers.delete(deliver);
          }}
        }})();
        const makeCtx = () => {{
          const hooks = {{}};
          return {{
            hooks,
            ctx: {{
              location: {{ directory: "/repo" }},
              session: {{ hook: async (name, fn) => {{ hooks[name] = fn; return {{ dispose() {{}} }}; }} }},
              tool: {{ hook: async () => ({{ dispose() {{}} }}) }},
              event: {{ subscribe }},
            }},
          }};
        }};
        let current = makeCtx();
        let cleanup = await mod.default.setup(current.ctx);
        const reload = async () => {{
          await cleanup();
          current = makeCtx();
          cleanup = await mod.default.setup(current.ctx);
        }};
        const text = (ordinal, value) =>
          push({{ type: "session.text.ended", data: {{ sessionID: "s-r", assistantMessageID: "m1", ordinal, text: value }} }});
        const end = () => push({{ type: "session.execution.succeeded", data: {{ sessionID: "s-r" }} }});
        await current.hooks.prompt({{ sessionID: "s-r", prompt: {{ text: "first" }} }});
        text(0, "Before reload");
        if (scenario === "idle") {{
          end();
          await waitForStops(1);
        }}
        await sleep(150);
        if (scenario !== "no_reload") await reload();
        if (scenario === "mid_turn_twice") await reload();
        if (scenario === "prompt_after_reload") {{
          await current.hooks.prompt({{ sessionID: "s-r", prompt: {{ text: "second" }} }});
          push({{ type: "session.text.ended", data: {{ sessionID: "s-r", assistantMessageID: "m2", ordinal: 0, text: "After reload" }} }});
        }} else if (scenario !== "idle") {{
          text(1, "After reload");
        }}
        // A stray terminal event in the idle case must not publish anything.
        end();
        await waitForStops({len(expected)});
        await sleep(300);
        await cleanup();
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
        {"session_id": "s-r", "cwd": "/repo", "last_assistant_message": text}
        for text in expected
    ]


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
    assert (
        package["exports"]["./server"]["import"] == "./plugin/opencode/dist/server.mjs"
    )


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
    for filename in (
        "assistant-buffer.js",
        "internal.js",
        "payload.js",
        "server.mjs",
        "v2.js",
    ):
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
