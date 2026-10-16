"""Model selection must reach real CLI subprocesses, not just installer output."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from claude_smart import optimizer_assistant, runtime
from tests.test_install_guide import fake_guide

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name == "nt", reason="Executable POSIX fake CLI fixture")
@pytest.mark.parametrize(
    "provider,model", [("codex", "gpt-6.1-sol"), ("opencode", "openai/gpt-6.1-sol")]
)
def test_extraction_bridge_passes_selected_model_to_agent(
    tmp_path, monkeypatch, provider, model
):
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
    result = subprocess.run(
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
    monkeypatch, tmp_path, provider, model
):
    runtime.set_host(runtime.HOST_CODEX)
    monkeypatch.setenv("CLAUDE_SMART_LEARNING_PROVIDER", provider)
    monkeypatch.setenv("CLAUDE_SMART_LEARNING_MODEL", model)
    cli = (
        "/fake/claude"
        if provider == "claude-code"
        else f"/fake/{provider}-claude-compat"
    )
    monkeypatch.setenv("CLAUDE_SMART_CLI_PATH", cli)
    monkeypatch.setattr(optimizer_assistant.shutil, "which", lambda _: "/fake/codex")
    output = tmp_path / "reply.txt"
    monkeypatch.setattr(optimizer_assistant, "_temporary_output_path", lambda: output)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        output.write_text("reply")
        return SimpleNamespace(returncode=0, stdout='{"result":"reply"}', stderr="")

    monkeypatch.setattr(optimizer_assistant.subprocess, "run", fake_run)
    assert (
        optimizer_assistant._run_local_cli(prompt="prompt", system_prompt="") == "reply"
    )
    assert calls[0][calls[0].index("--model") + 1] == model


def test_installer_preserves_model_on_update_and_adopts_new_provider_default(tmp_path):
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
        result = subprocess.run(
            [node, str(installer), "update", *args],
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert (
            f'CLAUDE_SMART_LEARNING_MODEL="{expected}"' in (state / ".env").read_text()
        )
        assert f"Learning model: {expected}" in result.stdout
