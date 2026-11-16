"""Model selection must reach real CLI subprocesses, not just installer output."""

# ruff: noqa: S101 -- pytest assertions are the test interface.

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from claude_smart import optimizer_assistant, runtime
from dotenv import dotenv_values

from tests.test_install_guide import fake_guide

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name == "nt", reason="Executable POSIX fake CLI fixture")
@pytest.mark.parametrize("provider,model", [("opencode", "openai/gpt-6.1-sol")])
def test_extraction_bridge_passes_selected_model_to_agent(
    tmp_path: Path, provider: str, model: str
) -> None:
    executable = tmp_path / provider
    recorded = tmp_path / "args.json"
    node = shutil.which("node")
    assert node is not None, "Node is required for CLI integration tests"
    executable.write_text(f"""#!{node}
const fs = require('fs');
const args = process.argv.slice(2);
fs.writeFileSync({json.dumps(str(recorded))}, JSON.stringify(args));
const position = args.indexOf('--output-last-message');
if (position >= 0) fs.writeFileSync(args[position+1], 'reply');
else console.log(JSON.stringify({{type:'result',result:'reply'}}));
""")
    executable.chmod(0o755)
    env = dict(os.environ)
    env["CLAUDE_SMART_LEARNING_MODEL"] = model
    env[f"CLAUDE_SMART_{provider.upper()}_PATH"] = str(executable)
    result = subprocess.run(  # noqa: S603 -- owned Node fixture and explicit executable.
        [
            node,
            str(ROOT / f"plugin/scripts/{provider}-claude-compat.js"),
            "-p",
            "--output-format",
            "json",
            "--model",
            "claude-provider-placeholder",
        ],
        input="test prompt",
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    args = json.loads(recorded.read_text())
    assert args[args.index("--model") + 1] == model
    assert "claude-provider-placeholder" not in args


@pytest.mark.parametrize(
    "provider,model",
    [
        ("claude-code", "claude-sonnet-5"),
        ("codex", "gpt-6.1-sol"),
        ("opencode", "openai/gpt-6.1-sol"),
    ],
)
def test_optimizer_passes_same_model_to_selected_provider(
    monkeypatch: pytest.MonkeyPatch, provider: str, model: str
) -> None:
    runtime.set_host(runtime.HOST_CODEX)
    monkeypatch.setenv("CLAUDE_SMART_LEARNING_PROVIDER", provider)
    monkeypatch.setenv("CLAUDE_SMART_LEARNING_MODEL", model)
    cli = (
        "/fake/claude"
        if provider == "claude-code"
        else f"/fake/{provider}-claude-compat"
    )
    if provider == "codex":
        monkeypatch.delenv("CLAUDE_SMART_CLI_PATH", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_SMART_CLI_PATH", cli)
    monkeypatch.setattr(optimizer_assistant.shutil, "which", lambda _: "/fake/codex")
    calls = []

    def fake_run(cmd: list[str], **_kwargs: Any) -> SimpleNamespace:
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout='{"result":"reply"}', stderr="")

    monkeypatch.setattr(optimizer_assistant.subprocess, "run", fake_run)
    result = optimizer_assistant._prepare_result(
        prompt="prompt", system_prompt="", deadline=time.monotonic() + 30
    )
    if provider == "opencode":
        assert result == {"content": "reply"}
        assert calls[0][calls[0].index("--model") + 1] == model
    else:
        assert result["native_cli_v1"]["model"] == model
        assert calls == []


def test_installer_preserves_model_on_update_and_adopts_new_provider_default(
    tmp_path: Path,
) -> None:
    installer, env = fake_guide(tmp_path)
    state = tmp_path / ".claude-smart"
    state.mkdir()
    (state / ".env").write_text(
        'REFLEXIO_URL="http://localhost:8071/"\nCLAUDE_SMART_LEARNING_PROVIDER="codex"\nCLAUDE_SMART_LEARNING_MODEL="gpt-6.1-sol"\n'
    )
    node = shutil.which("node")
    assert node is not None, "Node is required for CLI integration tests"
    for args, expected in [
        (["--host", "claude-code"], "gpt-6.1-sol"),
        (["--host", "claude-code", "--provider", "claude-code"], "sonnet"),
        (
            [
                "--host",
                "claude-code",
                "--provider",
                "claude-code",
                "--model",
                "claude-sonnet-5",
            ],
            "claude-sonnet-5",
        ),
    ]:
        result = subprocess.run(  # noqa: S603 -- owned Node fixture and explicit executable.
            [node, str(installer), "update", *args],
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert (
            dotenv_values(state / ".env", interpolate=False)[
                "CLAUDE_SMART_LEARNING_MODEL"
            ]
            == expected
        )
        assert f"Learning model: {expected}" in result.stdout
