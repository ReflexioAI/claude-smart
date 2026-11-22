"""Real shell selection and bundled SDK dispatch, with an owned stand-in CLI."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name == "nt", reason="Backend selection uses the POSIX shell")
@pytest.mark.parametrize("coding_host", ["claude-code", "codex", "opencode"])
@pytest.mark.parametrize("explicit_path", [False, True])
@pytest.mark.parametrize("learning_model", [None, " selected-native-model "])
@pytest.mark.parametrize("legacy_path", [False, True])
def test_saved_codex_learning_reaches_native_bundled_sdk(
    tmp_path: Path,
    coding_host: str,
    explicit_path: bool,
    learning_model: str | None,
    legacy_path: bool,
) -> None:
    vendor = ROOT / "plugin/vendor/reflexio"
    assert (vendor / "reflexio").is_dir(), "Materialize the locked SDK bundle first"
    assert (vendor / "reflexio/server/llm/_native_model_ref.py").is_file()
    record = tmp_path / "calls.jsonl"
    hooks = tmp_path / "python-hooks"
    hooks.mkdir()
    (hooks / "sitecustomize.py").write_text(
        "import sys,os,json\nfrom pathlib import Path\n"
        "if Path(sys.argv[0]).name == 'exec':\n"
        " data={'argv':sys.argv,'stdin':sys.stdin.read()}\n"
        f" with Path({str(record)!r}).open('a',encoding='utf-8') as out: out.write(json.dumps(data)+'\\n')\n"
        " Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text('owned response',encoding='utf-8')\n"
        " os._exit(0)\n",
        encoding="utf-8",
    )
    binaries = tmp_path / "bin"
    binaries.mkdir()
    cli = binaries / ("custom codex" if explicit_path else "codex")
    executable = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())
    cli.write_text(f'#!/bin/sh\nexec {shlex.quote(executable)} "$@"\n')
    cli.chmod(0o755)
    # A wrong learning-host binding must fail instead of touching an installed CLI.
    for name in ("claude", "opencode"):
        trap = binaries / name
        trap.write_text("#!/bin/sh\nexit 77\n")
        trap.chmod(0o755)
    empty = tmp_path / "empty.env"
    empty.touch()
    env = {
        key: os.environ[key] for key in ("PATH", "TMPDIR", "LANG") if key in os.environ
    }
    env.update(
        HOME=str(tmp_path),
        PATH=str(binaries) + os.pathsep + env.get("PATH", ""),
        PYTHONPATH=str(hooks) + os.pathsep + str(vendor),
        PYTHONNOUSERSITE="1",
        PYTHON_DOTENV_DISABLED="1",
        REFLEXIO_ENV_FILE=str(empty),
        LITELLM_LOCAL_MODEL_COST_MAP="true",
        OPENAI_API_KEY="sk-test-not-real",
        OMP_NUM_THREADS="1",
        OPENBLAS_NUM_THREADS="1",
        CLAUDE_SMART_HOST=coding_host,
        CLAUDE_SMART_LEARNING_PROVIDER="codex",
        CLAUDE_SMART_USE_LOCAL_CLI="1",
    )
    if legacy_path:
        env["CLAUDE_SMART_CODEX_PATH"] = "private-retired-executable"
    if explicit_path:
        env["CLAUDE_SMART_CLI_PATH"] = str(cli)
    if learning_model is not None:
        env["CLAUDE_SMART_LEARNING_MODEL"] = learning_model
    source = (ROOT / "plugin/scripts/backend-service.sh").read_text()
    selection = source[
        source.index("learning_provider=") : source.index("\nSTATE_DIR=")
    ]
    runner = str(ROOT / "plugin/scripts/backend-python-runner.py")
    program = (
        "import runpy\n"
        f"runpy.run_path({runner!r})['prepare_native_sdk_environment']()\n"
    )
    program += (
        "from reflexio.server.llm._native_model_ref import configured_native_model\n"
        "from reflexio.server.llm.providers import native_cli_provider as provider\n"
        "assert provider.register_native_provider()\n"
        "target = configured_native_model()\n"
        "assert target.host == 'codex'\n"
        "model = target.reference\n"
    )
    program += (
        "import litellm\n"
        "result=litellm.completion(model=model,messages=[{'role':'user','content':'private synthetic prompt'}],timeout=2.5,num_retries=0)\n"
        "assert result.choices[0].message.content=='owned response'\n"
        "assert result._hidden_params.get('reflexio_served_model') is None\n"
    )
    shell = (
        f". {shlex.quote(str(ROOT / 'plugin/scripts/_lib.sh'))}\n"
        f"PLUGIN_ROOT={shlex.quote(str(ROOT / 'plugin'))}\n"
        f"{selection}\n"
        f"exec {shlex.quote(sys.executable)} -c {shlex.quote(program)}\n"
    )
    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", shell],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if legacy_path and not explicit_path:
        assert result.returncode != 0
        assert "CLAUDE_SMART_CODEX_PATH" in result.stderr
        assert "private-retired-executable" not in result.stdout + result.stderr
        assert not record.exists(), "Retired selection must refuse before any CLI call"
        return
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-3000:]
    calls = [json.loads(line) for line in record.read_text().splitlines()]
    assert len(calls) == 1
    call = calls[0]
    args = call["argv"]
    assert args[0] == "exec" and args[-1] == "-"
    assert args[args.index("--sandbox") + 1] == "read-only"
    if learning_model:
        assert args[args.index("--model") + 1] == learning_model.strip()
    else:
        assert "--model" not in args
    assert "private synthetic prompt" in call["stdin"]
    assert "private synthetic prompt" not in repr(args)
    assert "opaque-role" not in repr(args)
    assert not Path(args[args.index("--output-last-message") + 1]).exists()


@pytest.mark.parametrize(
    "enabled,selected", [("1", ""), ("0", ""), ("", "local-cli/codex/private-native")]
)
def test_predecessor_sdk_never_receives_retired_model_selection(
    tmp_path: Path, enabled: str, selected: str
) -> None:
    """A missing bundled ABI must never revive the retired model selector."""
    vendor = Path(os.environ["CLAUDE_SMART_TEST_PREDECESSOR_SDK"])
    assert (vendor / "reflexio").is_dir()
    assert not (vendor / "reflexio/server/llm/_native_model_ref.py").exists()
    dotenv = tmp_path / ".env"
    original = "# saved configuration must remain unchanged\n"
    dotenv.write_text(original)
    env = {
        key: os.environ[key] for key in ("PATH", "TMPDIR", "LANG") if key in os.environ
    }
    env.update(
        HOME=str(tmp_path),
        PYTHONPATH=str(vendor),
        PYTHONNOUSERSITE="1",
        PYTHON_DOTENV_DISABLED="1",
        REFLEXIO_ENV_FILE=str(dotenv),
        CLAUDE_SMART_HOST="codex",
        CLAUDE_SMART_USE_LOCAL_CLI=enabled,
        CLAUDE_SMART_LEARNING_MODEL="private-selected-model",
        REFLEXIO_NATIVE_CLI_MODEL=selected,
    )
    program = """import os, runpy, sys
prepare = runpy.run_path(sys.argv[1])["prepare_native_sdk_environment"]
before = dict(os.environ)
if os.environ["CLAUDE_SMART_USE_LOCAL_CLI"] == "0":
    prepare()
else:
    try:
        prepare()
    except ValueError as exc:
        assert "bundled SDK" in str(exc)
        assert "private-selected-model" not in str(exc)
    else:
        raise AssertionError("An obsolete SDK must refuse native selection")
assert dict(os.environ) == before
assert not os.environ.get("CLAUDE_SMART_CLI_MODEL")
"""
    result = subprocess.run(
        [
            sys.executable,
            "-P",
            "-c",
            program,
            str(ROOT / "plugin/scripts/backend-python-runner.py"),
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert dotenv.read_text() == original


@pytest.mark.parametrize("host", ["unsupported-host", "codex/private-provider"])
def test_native_host_validation_preserves_environment(tmp_path, monkeypatch, host):
    import runpy

    dotenv = tmp_path / ".env"
    dotenv.touch()
    monkeypatch.setenv("REFLEXIO_ENV_FILE", str(dotenv))
    monkeypatch.setenv("CLAUDE_SMART_HOST", host)
    monkeypatch.setenv("CLAUDE_SMART_USE_LOCAL_CLI", "1")
    monkeypatch.setenv("CLAUDE_SMART_LEARNING_MODEL", "private-model")
    monkeypatch.delenv("REFLEXIO_NATIVE_CLI_MODEL", raising=False)
    monkeypatch.delenv("CLAUDE_SMART_CLI_MODEL", raising=False)
    prepare = runpy.run_path(str(ROOT / "plugin/scripts/backend-python-runner.py"))[
        "prepare_native_sdk_environment"
    ]
    before = dict(os.environ)
    with pytest.raises(ValueError, match="unsupported") as failure:
        prepare()
    assert "private" not in str(failure.value)
    assert dict(os.environ) == before
    assert dotenv.read_bytes() == b""
