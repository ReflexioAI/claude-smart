"""Pinned SDK dispatch uses the resolved OpenCode executable and one model."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name == "nt", reason="Executable POSIX stand-in CLI")
@pytest.mark.parametrize(
    "model", [None, "opaque-model", "provider/model", "provider/model#high", "#high"]
)
def test_saved_opencode_target_reaches_executable_through_pinned_sdk(tmp_path, model):
    vendor = ROOT / "plugin/vendor/reflexio"
    assert (vendor / "reflexio/server/llm/_native_model_ref.py").is_file()
    node = shutil.which("node")
    assert node is not None
    record = tmp_path / "args.json"
    executable = tmp_path / "opencode"
    executable.write_text(
        f"#!{node}\n"
        "const fs=require('fs');const args=process.argv.slice(2);\n"
        "if(args.includes('--help')){console.log('  --variant <variant>');process.exit(0);}\n"
        f"fs.writeFileSync({json.dumps(str(record))},JSON.stringify(args));\n"
        "console.log(JSON.stringify({type:'text',part:{type:'text',text:'owned response'}}));\n"
    )
    executable.chmod(0o755)
    selected = tmp_path / ".claude-smart/.env"
    selected.parent.mkdir()
    reference = "local-cli/opencode" + (f"/{model}" if model is not None else "")
    original = f"REFLEXIO_NATIVE_CLI_MODEL={reference}\n"
    selected.write_text(original)
    env = {
        key: os.environ[key] for key in ("PATH", "TMPDIR", "LANG") if key in os.environ
    }
    env.update(
        HOME=str(tmp_path),
        REFLEXIO_ENV_FILE=str(selected),
        PATH=str(tmp_path) + os.pathsep + env.get("PATH", ""),
        PYTHONPATH=os.pathsep.join((str(vendor), str(ROOT / "plugin/src"))),
        PYTHON_DOTENV_DISABLED="true",
        LITELLM_LOCAL_MODEL_COST_MAP="true",
    )
    program = """
import json,os,runpy,sys
from pathlib import Path
import reflexio.server.llm._native_model_ref as native
from reflexio.server.llm.providers.native_cli_provider import _run_cli_stream
root=Path(sys.argv[1])
assert Path(native.__file__)==root/'plugin/vendor/reflexio/reflexio/server/llm/_native_model_ref.py'
runner=runpy.run_path(str(root/'plugin/scripts/backend-python-runner.py'))
runner['prepare_native_sdk_environment']()
assert os.environ['CLAUDE_SMART_CLI_PATH'] == str(Path.cwd() / 'opencode')
target=native.configured_native_model()
result=_run_cli_stream(os.environ['CLAUDE_SMART_CLI_PATH'],'owned system','owned dialogue',10,target.host,target.model)
assert result.success, result
assert result.terminal_text=='owned response', result
assert result.cli_binary=='opencode', result
print(json.dumps({'target':target.reference}))
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(ROOT)],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["target"] == reference
    assert selected.read_text() == original
    args = json.loads(record.read_text())
    selected_model, _, variant = (model or "").partition("#")
    if selected_model:
        assert args[args.index("--model") + 1] == selected_model
    else:
        assert "--model" not in args
    if variant:
        assert f"--variant={variant}" in args
    else:
        assert "--variant" not in args
