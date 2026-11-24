"""Invocation attribution must not borrow an ambient integration identity."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "ambient", ["claude-code", "codex", "opencode", "cursor", "invalid"]
)
def test_unbound_process_cannot_claim_an_ambient_host(
    tmp_path: Path, ambient: str
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import json;from claude_smart import runtime;"
            "print(json.dumps({'host':runtime.host(),'attribution':runtime.attribution_host()}))",
        ],
        cwd=tmp_path,
        env={**os.environ, "HOME": str(tmp_path), "CLAUDE_SMART_HOST": ambient},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "host": "claude-code",
        "attribution": "unknown",
    }
