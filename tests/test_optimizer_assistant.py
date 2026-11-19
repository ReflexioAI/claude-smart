"""Prompt compilation and the retained OpenCode compatibility path."""

# ruff: noqa: S101 -- pytest assertions are the test interface.

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from claude_smart import optimizer_assistant, runtime


@pytest.fixture(autouse=True)
def _invocation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "_REFLEXIO_ASSISTANT_INVOCATION",
        json.dumps(
            {"protocol": "native-cli/v1", "deadline_monotonic": time.monotonic() + 30}
        ),
    )
    monkeypatch.setenv("CLAUDE_SMART_HOST", runtime.HOST_CLAUDE_CODE)
    monkeypatch.delenv("CLAUDE_SMART_LEARNING_PROVIDER", raising=False)
    monkeypatch.delenv("CLAUDE_SMART_CLI_PATH", raising=False)
    monkeypatch.delenv("CLAUDE_SMART_LEARNING_MODEL", raising=False)
    monkeypatch.delenv("CLAUDE_SMART_USE_LOCAL_CLI", raising=False)


def _run_main(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any] | str
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    rc = optimizer_assistant.main()
    return rc, out.getvalue(), err.getvalue()


def _payload() -> dict[str, Any]:
    return {
        "messages": [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "seen"},
            {"role": "user", "content": "answer now"},
        ],
        "playbooks": [{"id": 1, "content": "Be concise.", "trigger": "summaries"}],
    }


@pytest.mark.parametrize("provider", ["claude-code", "codex"])
@pytest.mark.parametrize("override", [None, "missing-custom-cli"])
def test_unresolved_bare_cli_never_selects_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str, override: str | None
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_SMART_LEARNING_PROVIDER", provider)
    if override:
        monkeypatch.setenv("CLAUDE_SMART_CLI_PATH", override)
    name = override or ("claude" if provider == "claude-code" else "codex")
    (tmp_path / name).write_text("untrusted working-directory program")
    monkeypatch.setattr(optimizer_assistant.shutil, "which", lambda _: None)
    launch = Mock(side_effect=AssertionError("untrusted program launched"))
    monkeypatch.setattr(optimizer_assistant.subprocess, "run", launch)
    rc, out, err = _run_main(monkeypatch, _payload())
    assert rc == 1 and out == "" and "not found on PATH" in err
    launch.assert_not_called()


@pytest.mark.parametrize(
    "host", [runtime.HOST_CLAUDE_CODE, runtime.HOST_CODEX, runtime.HOST_OPENCODE]
)
@pytest.mark.parametrize(
    "provider,override,protocol,executable",
    [
        ("claude-code", "", "claude-json", "/bin/claude"),
        ("codex", "", "codex", "/bin/codex"),
        ("codex", "/opt/custom-router", "claude-json", "/opt/custom-router"),
    ],
)
def test_optimizer_compiles_saved_provider_independently_of_hook_host(
    monkeypatch: pytest.MonkeyPatch,
    host: str,
    provider: str,
    override: str,
    protocol: str,
    executable: str,
) -> None:
    runtime.set_host(host)
    monkeypatch.setenv("CLAUDE_SMART_LEARNING_PROVIDER", provider)
    monkeypatch.setenv("CLAUDE_SMART_CLI_PATH", override)
    monkeypatch.setattr(
        optimizer_assistant.shutil,
        "which",
        lambda name: {"claude": "/bin/claude", "codex": "/bin/codex"}.get(name),
    )
    launch = Mock(side_effect=AssertionError("compiler launched model"))
    monkeypatch.setattr(optimizer_assistant.subprocess, "run", launch)
    rc, out, err = _run_main(monkeypatch, _payload())
    assert rc == 0 and err == ""
    request = json.loads(out)["native_cli_v1"]
    assert request["protocol"] == protocol and request["executable"] == executable
    assert request["dialogue"] == "answer now"
    assert "Be concise." in request["system_prompt"]
    assert (
        "User: first" in request["system_prompt"]
        and "Assistant: seen" in request["system_prompt"]
    )
    assert "timeout" not in request and "permissions" not in request
    launch.assert_not_called()


@pytest.mark.parametrize("model", [None, "sonnet", "chosen-model"])
@pytest.mark.parametrize("provider", ["claude-code", "codex"])
def test_optimizer_preserves_model_and_native_defaults(
    monkeypatch: pytest.MonkeyPatch, model: str | None, provider: str
) -> None:
    monkeypatch.setenv("CLAUDE_SMART_LEARNING_PROVIDER", provider)
    monkeypatch.setenv("CLAUDE_SMART_CLI_MODEL", "unused-generation-model")
    if model is not None:
        monkeypatch.setenv("CLAUDE_SMART_LEARNING_MODEL", model)
    monkeypatch.setattr(
        optimizer_assistant.shutil, "which", lambda name: "/bin/" + name
    )
    rc, out, err = _run_main(monkeypatch, _payload())
    assert rc == 0 and err == ""
    assert json.loads(out)["native_cli_v1"]["model"] == (
        model or ("sonnet" if provider == "claude-code" else None)
    )


@pytest.mark.parametrize(
    "payload",
    [
        "{not-json",
        {},
        {"messages": [], "playbooks": []},
        {"messages": [{"content": 42}], "playbooks": []},
    ],
)
def test_optimizer_rejects_invalid_payload_without_execution(
    monkeypatch: pytest.MonkeyPatch, payload: Any
) -> None:
    launch = Mock(side_effect=AssertionError("invalid input dispatched"))
    monkeypatch.setattr(optimizer_assistant.subprocess, "run", launch)
    rc, out, _err = _run_main(monkeypatch, payload)
    assert rc == 1 and out == ""
    launch.assert_not_called()


def test_optimizer_reports_retired_opencode_model_before_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_SMART_LEARNING_PROVIDER", "opencode")
    monkeypatch.setenv("CLAUDE_SMART_CLI_PATH", "/plugin/opencode-claude-compat")
    monkeypatch.setenv("CLAUDE_SMART_OPENCODE_MODEL", "private-retired-selection")
    runner = Mock(side_effect=AssertionError("retired setting launched a process"))
    monkeypatch.setattr(optimizer_assistant.subprocess, "run", runner)
    rc, out, err = _run_main(monkeypatch, _payload())
    assert rc == 1 and out == ""
    assert "CLAUDE_SMART_OPENCODE_MODEL has been retired" in err
    assert "CLAUDE_SMART_LEARNING_MODEL" in err
    assert "private-retired-selection" not in err
    runner.assert_not_called()


@pytest.mark.parametrize("response", ["result", "response"])
def test_optimizer_legacy_opencode_retains_wire_model_variant_and_budget(
    monkeypatch: pytest.MonkeyPatch, response: str
) -> None:
    monkeypatch.setenv("CLAUDE_SMART_LEARNING_PROVIDER", "opencode")
    monkeypatch.setenv("CLAUDE_SMART_CLI_PATH", "/plugin/opencode-claude-compat")
    monkeypatch.setenv("CLAUDE_SMART_LEARNING_MODEL", "provider/model")
    monkeypatch.setenv("CLAUDE_SMART_OPENCODE_VARIANT", "high")
    monkeypatch.setenv(
        "_REFLEXIO_ASSISTANT_INVOCATION",
        json.dumps(
            {"protocol": "native-cli/v1", "deadline_monotonic": time.monotonic() + 600}
        ),
    )
    calls = []

    def run(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append((cmd, kwargs))
        return SimpleNamespace(
            returncode=0, stdout=json.dumps({response: "owned reply"}), stderr=""
        )

    monkeypatch.setattr(optimizer_assistant.subprocess, "run", run)
    rc, out, err = _run_main(monkeypatch, _payload())
    assert rc == 0 and err == "" and json.loads(out) == {"content": "owned reply"}
    cmd, kwargs = calls[0]
    assert cmd[:4] == [
        "/plugin/opencode-claude-compat",
        "-p",
        "--output-format",
        "json",
    ]
    assert cmd[cmd.index("--model") + 1] == "provider/model"
    assert "--permission-mode" not in cmd and "--tools" not in cmd
    assert kwargs["env"]["CLAUDE_SMART_OPENCODE_VARIANT"] == "high"
    assert kwargs["env"]["CLAUDE_SMART_INTERNAL"] == "1"
    assert kwargs["env"]["CLAUDE_CODE_ENTRYPOINT"] == "optimizer"
    assert 300 < kwargs["timeout"] <= 600


@pytest.mark.parametrize(
    "body,code",
    [("private-invalid", 0), ("[]", 0), ("{}", 0), ('{"result":"private-output"}', 2)],
)
def test_legacy_bridge_failure_is_single_attempt_and_private(
    monkeypatch: pytest.MonkeyPatch, body: str, code: int
) -> None:
    monkeypatch.setenv("CLAUDE_SMART_CLI_PATH", "/plugin/opencode-claude-compat")
    launch = Mock(
        return_value=SimpleNamespace(
            returncode=code, stdout=body, stderr="private-error"
        )
    )
    monkeypatch.setattr(optimizer_assistant.subprocess, "run", launch)
    rc, out, err = _run_main(monkeypatch, _payload())
    assert rc == 1 and out == "" and "private" not in err
    assert launch.call_count == 1


def test_expired_invocation_never_launches_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_SMART_CLI_PATH", "/plugin/opencode-claude-compat")
    monkeypatch.setenv(
        "_REFLEXIO_ASSISTANT_INVOCATION",
        json.dumps(
            {"protocol": "native-cli/v1", "deadline_monotonic": time.monotonic() - 1}
        ),
    )
    launch = Mock()
    monkeypatch.setattr(optimizer_assistant.subprocess, "run", launch)
    rc, out, err = _run_main(monkeypatch, _payload())
    assert rc == 1 and out == "" and "deadline expired" in err
    launch.assert_not_called()


@pytest.mark.parametrize("relative", [False, True])
def test_override_missing_path_does_not_choose_another_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, relative: bool
) -> None:
    selected = str(tmp_path / "missing-cli")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "CLAUDE_SMART_CLI_PATH", "./missing-cli" if relative else selected
    )
    lookups = []

    def which(name: str) -> str | None:
        lookups.append(name)
        return None

    monkeypatch.setattr(optimizer_assistant.shutil, "which", which)
    rc, out, err = _run_main(monkeypatch, _payload())
    assert rc == 0 and err == ""
    assert json.loads(out)["native_cli_v1"]["executable"] == selected
    assert "claude" not in lookups


@pytest.mark.skipif(os.name == "nt", reason="POSIX plugin-root symlink layout")
def test_installed_optimizer_helper_survives_plugin_root_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit stable backend path follows an install's refreshed root link."""
    import shutil

    from reflexio.server.services.playbook_optimizer.assistant_webhook import (
        LocalScriptAssistant,
    )
    from reflexio.server.services.playbook_optimizer.models import ChatMessage

    root = Path(__file__).resolve().parents[1]
    helper = Path(sys.executable).parent / "claude-smart-optimizer-assistant"
    assert helper.is_file(), "install the plugin console entry points before testing"
    home = tmp_path / "home"
    home.mkdir()
    fake_cli = tmp_path / "owned-cli"
    fake_cli.write_text(
        "#!/bin/sh\n"
        'test "$CLAUDE_SMART_INTERNAL" = 1 || exit 44\n'
        'echo \'{"result":"owned reply"}\'\n'
    )
    fake_cli.chmod(0o755)
    env = os.environ.copy()
    env.update(
        HOME=str(home),
        USERPROFILE=str(home),
        CLAUDE_SMART_CLI_PATH=str(fake_cli),
        CLAUDE_SMART_HOST="claude-code",
        CLAUDE_SMART_LEARNING_PROVIDER="claude-code",
    )
    env.pop("CODEX_HOME", None)
    env.pop("CLAUDE_SMART_LEARNING_MODEL", None)
    saved_binding = (
        home / ".reflexio/plugin-root/.venv/bin/claude-smart-optimizer-assistant"
    )
    bash = shutil.which("bash")
    assert bash is not None
    old_install = None
    for version in ("1.0.0", "2.0.0"):
        install = home / ".claude/plugins/cache/reflexioai/claude-smart" / version
        install.mkdir(parents=True)
        (install / "pyproject.toml").write_text(
            (root / "plugin/pyproject.toml").read_text()
        )
        (install / ".venv").symlink_to(helper.parent.parent, target_is_directory=True)
        subprocess.run(  # noqa: S603 -- owned test installer script and temporary home.
            [bash, str(root / "plugin/scripts/ensure-plugin-root.sh"), str(install)],
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        assert (home / ".reflexio/plugin-root").resolve() == install
        if old_install is not None:
            shutil.rmtree(old_install)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        caller = LocalScriptAssistant(str(saved_binding), [], 15)
        assert (
            caller([ChatMessage(role="user", content="synthetic")], []) == "owned reply"
        )
        old_install = install
