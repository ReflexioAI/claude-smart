"""Contained happy-path E2E coverage for host hook integration."""

from __future__ import annotations

from pathlib import Path

import pytest  # type: ignore[reportMissingImports]

from claude_smart import runtime
from tests.host_learning_harness import run_host_learning_happy_path


@pytest.mark.parametrize(
    "host",
    [runtime.HOST_CLAUDE_CODE, runtime.HOST_CODEX, runtime.HOST_OPENCODE],
)
def test_host_happy_path_applies_learning_buffers_tools_and_publishes(
    host: str,
    session_dir: Path,
    tmp_path: Path,
) -> None:
    run_host_learning_happy_path(
        host,
        work_root=tmp_path,
        expected_state_dir=session_dir,
    )
