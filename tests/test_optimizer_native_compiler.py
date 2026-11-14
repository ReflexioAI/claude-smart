"""The shipped assistant compiles native requests; the SDK owns their execution."""

# ruff: noqa: S101 -- pytest assertions are the test interface.

import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from claude_smart import optimizer_assistant as helper
from claude_smart import runtime


def _invoke(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    result = helper.main()
    return result, out.getvalue(), err.getvalue()


@pytest.fixture
def invocation(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setenv(
        "_REFLEXIO_ASSISTANT_INVOCATION",
        json.dumps(
            {"protocol": "native-cli/v1", "deadline_monotonic": time.monotonic() + 30}
        ),
    )
    monkeypatch.delenv("CLAUDE_SMART_CLI_PATH", raising=False)
    monkeypatch.delenv("CLAUDE_SMART_LEARNING_MODEL", raising=False)
    monkeypatch.delenv("CLAUDE_SMART_LEARNING_PROVIDER", raising=False)
    monkeypatch.setattr(
        helper.shutil,
        "which",
        lambda name: {"claude": "/bin/claude", "codex": "/bin/codex"}.get(name),
    )
    return {"messages": [{"role": "user", "content": "owned task"}], "playbooks": []}


@pytest.mark.parametrize("host", ["claude-code", "codex", "opencode"])
@pytest.mark.parametrize(
    "provider,protocol,executable",
    [("claude-code", "claude-json", "/bin/claude"), ("codex", "codex", "/bin/codex")],
)
def test_native_compiler_never_launches_cli(
    monkeypatch: pytest.MonkeyPatch,
    invocation: dict[str, Any],
    host: str,
    provider: str,
    protocol: str,
    executable: str,
) -> None:
    runtime.set_host(host)
    monkeypatch.setenv("CLAUDE_SMART_LEARNING_PROVIDER", provider)
    launch = Mock(side_effect=AssertionError("compiler attempted model execution"))
    monkeypatch.setattr(helper.subprocess, "run", launch)
    rc, out, err = _invoke(monkeypatch, invocation)
    assert rc == 0 and err == ""
    result = json.loads(out)["native_cli_v1"]
    assert result["protocol"] == protocol and result["executable"] == executable
    assert result["dialogue"] == "owned task"
    assert result["model"] == ("sonnet" if provider == "claude-code" else None)
    assert set(result) == {
        "protocol",
        "executable",
        "model",
        "system_prompt",
        "dialogue",
    }
    launch.assert_not_called()


@pytest.mark.skipif(os.name == "nt", reason="owned POSIX shebang executable")
@pytest.mark.parametrize("delay", [0, 30])
def test_real_plugin_claude_json_permissions_and_retirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, delay: int
) -> None:
    from reflexio.server.services.playbook_optimizer.assistant_webhook import (
        LocalScriptAssistant,
        LocalScriptFailedError,
    )
    from reflexio.server.services.playbook_optimizer.models import ChatMessage

    executable = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())
    fake_cli = tmp_path / "owned-claude"
    record = tmp_path / "claude.json"
    fake_cli.write_text(
        f"#!{executable}\n"
        "import sys,os,json,time\nfrom pathlib import Path\n"
        "data={'pid':os.getpid(),'ppid':os.getppid(),'argv':sys.argv,'stdin':sys.stdin.read()}\n"
        "data['env']={k:os.environ.get(k) for k in ('CLAUDE_SMART_INTERNAL','CLAUDE_CODE_ENTRYPOINT')}\n"
        f"with Path({str(record)!r}).open('a') as f:f.write(json.dumps(data)+'\\n')\n"
        f"time.sleep({delay})\n"
        "print(json.dumps({'result':'owned Claude reply'}))\n",
        encoding="utf-8",
    )
    fake_cli.chmod(0o755)
    monkeypatch.setenv("CLAUDE_SMART_CLI_PATH", str(fake_cli))
    monkeypatch.setenv("CLAUDE_SMART_LEARNING_PROVIDER", "claude-code")
    monkeypatch.setenv("CLAUDE_SMART_LEARNING_MODEL", "owned-claude-model")
    monkeypatch.setenv("CLAUDE_SMART_HOST", "codex")
    children = []
    original_popen = subprocess.Popen

    def observed(*args: Any, **kwargs: Any) -> subprocess.Popen[Any]:
        child = original_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", observed)
    assistant = LocalScriptAssistant(
        sys.executable, ["-m", "claude_smart.optimizer_assistant"], 2 if delay else 10
    )
    messages = [ChatMessage(role="user", content="owned synthetic Claude task")]
    if delay:
        with pytest.raises(LocalScriptFailedError, match="timed out"):
            assistant(messages, [])
    else:
        assert assistant(messages, []) == "owned Claude reply"
    entries = record.read_text().splitlines()
    assert len(entries) == 1, "a failed native call was replayed"
    entry = json.loads(entries[0])
    assert entry["ppid"] == os.getpid()
    owned = [child for child in children if child.pid == entry["pid"]]
    assert len(owned) == 1 and owned[0].poll() is not None
    assert entry["env"] == {
        "CLAUDE_SMART_INTERNAL": "1",
        "CLAUDE_CODE_ENTRYPOINT": "optimizer",
    }
    argv = entry["argv"]
    assert argv[argv.index("--model") + 1] == "owned-claude-model"
    assert argv[argv.index("--permission-mode") + 1] == "plan"
    assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob,LS"
    assert (
        argv[argv.index("--disallowedTools") + 1]
        == "Bash,Edit,Write,MultiEdit,NotebookEdit"
    )
    assert "--no-session-persistence" in argv and "--strict-mcp-config" in argv
    assert json.loads(argv[argv.index("--mcp-config") + 1]) == {"mcpServers": {}}
    assert entry["stdin"] == "owned synthetic Claude task"
    with pytest.raises(ProcessLookupError):
        os.kill(entry["pid"], 0)


@pytest.mark.parametrize("host,provider", [("codex", None), ("claude-code", "codex")])
@pytest.mark.parametrize("model", [None, "owned/native-model"])
@pytest.mark.parametrize("delay", [0, 30])
def test_real_plugin_compiler_sdk_native_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host: str,
    provider: str | None,
    model: str | None,
    delay: int,
) -> None:
    """Compile the shipped plugin, then execute only an owned fake native CLI."""
    from reflexio.models.api_schema.domain import AgentPlaybook
    from reflexio.server.services.playbook_optimizer import assistant_webhook as caller
    from reflexio.server.services.playbook_optimizer.models import ChatMessage

    executable = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    record = tmp_path / "native.json"
    # Python's exec argument loads sitecustomize before looking for the script.
    # All Codex lookups resolve to this owned interpreter, never a user's CLI.
    (hooks / "sitecustomize.py").write_text(
        "import sys,os,json,time,shutil\nfrom pathlib import Path\n"
        f"_owned={executable!r}\n_original=shutil.which\n"
        "shutil.which=lambda name,*a,**kw: _owned if name=='codex' else _original(name,*a,**kw)\n"
        "if Path(sys.argv[0]).name == 'exec':\n"
        " data={'pid':os.getpid(),'ppid':os.getppid(),'argv':sys.argv,'stdin':sys.stdin.read()}\n"
        " data['env']={k:os.environ.get(k) for k in ('CLAUDE_SMART_INTERNAL','CLAUDE_CODE_ENTRYPOINT')}\n"
        f" Path({str(record)!r}).write_text(json.dumps(data),encoding='utf-8')\n"
        f" time.sleep({delay})\n"
        " Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text('owned reply',encoding='utf-8')\n"
        " os._exit(0)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "PYTHONPATH", str(hooks) + os.pathsep + os.environ.get("PYTHONPATH", "")
    )
    monkeypatch.setenv("CLAUDE_SMART_HOST", host)
    monkeypatch.setenv("CLAUDE_SMART_CLI_MODEL", "must-not-select-ambient-model")
    for key, value in (
        ("CLAUDE_SMART_LEARNING_PROVIDER", provider),
        ("CLAUDE_SMART_LEARNING_MODEL", model),
    ):
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    monkeypatch.delenv("CLAUDE_SMART_CLI_PATH", raising=False)
    real_dispatch = caller.run_native_assistant

    def guarded_dispatch(request: Any, deadline: float) -> str:
        assert request.protocol == "codex" and request.executable == executable
        return real_dispatch(request, deadline)

    monkeypatch.setattr(caller, "run_native_assistant", guarded_dispatch)
    assistant = caller.LocalScriptAssistant(
        sys.executable, ["-m", "claude_smart.optimizer_assistant"], 2 if delay else 10
    )
    messages = [ChatMessage(role="user", content="owned synthetic task")]
    playbooks = [AgentPlaybook(agent_version="test", content="owned candidate rule")]
    if delay:
        with pytest.raises(caller.LocalScriptFailedError, match="timed out"):
            assistant(messages, playbooks)
    else:
        assert assistant(messages, playbooks) == "owned reply"
    entry = json.loads(record.read_text())
    assert entry["ppid"] == os.getpid(), (
        "the compiler, rather than SDK, launched the CLI"
    )
    assert entry["env"] == {
        "CLAUDE_SMART_INTERNAL": "1",
        "CLAUDE_CODE_ENTRYPOINT": "optimizer",
    }
    assert entry["argv"][entry["argv"].index("--sandbox") + 1] == "read-only"
    assert all(
        flag in entry["argv"]
        for flag in ("--ephemeral", "--ignore-rules", "--skip-git-repo-check")
    )
    assert "must-not-select-ambient-model" not in entry["argv"]
    if model is None:
        assert "--model" not in entry["argv"]
    else:
        assert entry["argv"][entry["argv"].index("--model") + 1] == model
    assert (
        "owned candidate rule" in entry["stdin"]
        and "owned synthetic task" in entry["stdin"]
    )
    assert not Path(
        entry["argv"][entry["argv"].index("--output-last-message") + 1]
    ).exists()
    if os.name != "nt":
        with pytest.raises(ProcessLookupError):
            os.kill(entry["pid"], 0)


@pytest.mark.parametrize(
    "header",
    [
        None,
        "private-invalid",
        '{"protocol":"old","deadline_monotonic":1}',
        '{"protocol":"native-cli/v1","deadline_monotonic":true}',
    ],
)
def test_old_or_invalid_caller_refuses_before_execution(
    monkeypatch: pytest.MonkeyPatch,
    invocation: dict[str, Any],
    header: str | None,
) -> None:
    if header is None:
        monkeypatch.delenv("_REFLEXIO_ASSISTANT_INVOCATION")
    else:
        monkeypatch.setenv("_REFLEXIO_ASSISTANT_INVOCATION", header)
    launch = Mock(side_effect=AssertionError("unexpected execution"))
    monkeypatch.setattr(helper.subprocess, "run", launch)
    rc, out, err = _invoke(monkeypatch, invocation)
    assert rc == 1 and out == ""
    assert "Upgrade the calling server SDK" in err and "private-invalid" not in err
    launch.assert_not_called()
