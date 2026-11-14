"""Real shell selection and bundled SDK dispatch, with an owned stand-in CLI."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name == "nt", reason="Backend selection uses the POSIX shell")
@pytest.mark.parametrize("coding_host", ["claude-code", "codex", "opencode"])
@pytest.mark.parametrize("explicit_path", [False, True])
@pytest.mark.parametrize("learning_model", [None, " selected-native-model "])
def test_saved_codex_learning_reaches_native_bundled_sdk(
    tmp_path: Path, coding_host: str, explicit_path: bool, learning_model: str | None
) -> None:
    vendor = ROOT / "plugin/vendor/reflexio"
    assert (vendor / "reflexio").is_dir(), "Materialize the locked SDK bundle first"
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
    if explicit_path:
        env["CLAUDE_SMART_CLI_PATH"] = str(cli)
    if learning_model is not None:
        env["CLAUDE_SMART_LEARNING_MODEL"] = learning_model
    source = (ROOT / "plugin/scripts/backend-service.sh").read_text()
    selection = source[
        source.index("learning_provider=") : source.index("\nSTATE_DIR=")
    ]
    program = (
        "import litellm\n"
        "from reflexio.server.llm.providers import claude_code_provider as provider\n"
        "assert provider.register_if_enabled()\n"
        "result=litellm.completion(model='claude-code/opaque-role',messages=[{'role':'user','content':'private synthetic prompt'}],timeout=2.5,num_retries=0)\n"
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
