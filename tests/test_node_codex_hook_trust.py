"""Canonical npm installer trust behavior through a real owned fake Codex CLI."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from tests.test_install_scripts import (
    _isolated_env,
    _write_bootstrap_uv,
    _write_executable,
)

_PLUGIN = "claude-smart@reflexioai"
_FAKE_CODEX = '\nimport json, os, sys, time\n\nmode = os.environ.get("FAKE_CODEX_MODE", "ok")\nlog = os.environ.get("FAKE_CODEX_LOG")\n\n\ndef record(entry):\n    if log:\n        with open(log, "a") as handle:\n            handle.write(json.dumps(entry) + "\\n")\n\n\ndef emit(message):\n    sys.stdout.write(json.dumps(message) + "\\n")\n    sys.stdout.flush()\n\n\nargs = sys.argv[1:]\nrecord({"argv": args})\nif args[:1] != ["app-server"]:\n    code = int(os.environ.get("FAKE_CODEX_FEATURE_EXIT", "0")) if args[:1] == ["features"] else 0\n    sys.stdout.write(os.environ.get("FAKE_CODEX_STDOUT", ""))\n    sys.stderr.write(os.environ.get("FAKE_CODEX_STDERR", ""))\n    sys.exit(code)\n\nif mode in ("exit", "exit_after_output"):\n    # Consume the initialize request first so the parent\'s write cannot hit\n    # a closed pipe; the test then exercises stdout EOF deterministically.\n    sys.stdin.readline()\n    if mode == "exit_after_output":\n        emit({"method": "log"})\n    sys.exit(3)\n\nfor line in sys.stdin:\n    request = json.loads(line)\n    record({"request": request})\n    if mode == "hang":\n        time.sleep(60)\n    method = request.get("method")\n    if method == "initialize":\n        if mode == "burst":\n            # Noise, a notification and the reply in ONE write: the reply\n            # lands in the reader\'s buffer behind the earlier lines.\n            sys.stdout.write(\n                "starting codex app-server\\n"\n                + json.dumps({"method": "log"}) + "\\n"\n                + json.dumps({"id": request["id"], "result": {}}) + "\\n"\n            )\n            sys.stdout.flush()\n            continue\n        if mode == "error":\n            emit({"id": request["id"], "error": {"message": "unsupported client"}})\n            continue\n        emit({"id": request["id"], "result": {"ok": True}})\n    elif method == "hooks/list":\n        if mode == "nodata":\n            emit({"id": request["id"], "result": {"data": []}})\n        elif mode == "malformed":\n            emit({"id": request["id"], "result": {"data": [{"hooks": "nope"}]}})\n        else:\n            emit({"id": request["id"], "result": {"data": [{"hooks": json.loads(\n                os.environ["FAKE_CODEX_HOOKS"])}]}})\n'


@pytest.fixture
def node_install(tmp_path):
    if os.name == "nt":
        pytest.skip(
            "POSIX executable fake CLI; native publication is covered separately"
        )
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required")
    home = tmp_path / "home"
    project = tmp_path / "project"
    fake_bin = tmp_path / "bin"
    private = home / ".claude-smart/node/current/bin"
    uv_bin = home / ".local/bin"
    for directory in [home, project, fake_bin, private, uv_bin]:
        directory.mkdir(parents=True)
    _write_executable(fake_bin / "codex", f"#!{sys.executable}\n{_FAKE_CODEX}")
    _write_executable(private / "node", f'#!/bin/sh\nexec {shlex.quote(node)} "$@"\n')
    _write_executable(
        private / "npm",
        '#!/bin/sh\nif [ "$1" = "--version" ]; then echo 10.0.0; fi\nexit 0\n',
    )
    _write_bootstrap_uv(uv_bin / "uv", mode="fresh")
    account = home / ".codex"
    account.mkdir()
    config = account / "config.toml"
    original = 'model = "kept"\n[features]\nother = true\n[hooks.state."unrelated:stop"]\ntrusted_hash = "keep-other"\n[hooks.state."claude-smart@reflexioai:old"]\ntrusted_hash = "stale"\n'
    config.write_text(original)
    environment = _isolated_env(home)
    environment.update(
        USERPROFILE=str(home),
        CODEX_HOME=str(account),
        PATH=str(fake_bin) + os.pathsep + environment.get("PATH", ""),
        CLAUDE_SMART_DEFER_SHARED_SERVICES="1",
        CLAUDE_SMART_TEST_PLATFORM="linux",
        CLAUDE_SMART_TEST_ARCH="x64",
        FAKE_CODEX_LOG=str(home / "calls.jsonl"),
        CLAUDE_SMART_NODE_BASE_URL=(tmp_path / "offline-only").as_uri(),
    )
    root = Path(__file__).resolve().parents[1]

    def run(mode="ok", hooks=None, feature_exit=0, unwritable=False):
        if unwritable:
            config.unlink()
            config.mkdir()
        env = dict(environment)
        env.update(
            FAKE_CODEX_MODE=mode,
            FAKE_CODEX_HOOKS=json.dumps(
                hooks
                if hooks is not None
                else [
                    {
                        "pluginId": _PLUGIN,
                        "key": f"{_PLUGIN}:stop",
                        "currentHash": "new-stop",
                    }
                ]
            ),
            FAKE_CODEX_FEATURE_EXIT=str(feature_exit),
        )
        command = [
            node,
            str(root / "bin/claude-smart.js"),
            "install",
            "--host",
            "codex",
        ]
        with subprocess.Popen(
            command,
            cwd=project,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        ) as child:
            try:
                output, errors = child.communicate(timeout=80)
            except subprocess.TimeoutExpired:
                import signal

                # This group is created above and contains only owned fakes.
                os.killpg(child.pid, signal.SIGKILL)
                child.communicate(timeout=10)
                raise
        result = subprocess.CompletedProcess(command, child.returncode, output, errors)
        return result, config

    return run


@pytest.mark.parametrize("mode", ["ok", "burst"])
def test_node_trusts_only_current_plugin_hashes_and_preserves_unrelated_state(
    node_install, mode
):
    result, config = node_install(
        mode,
        hooks=[
            {
                "pluginId": _PLUGIN,
                "key": f"{_PLUGIN}:start",
                "currentHash": "new-start",
            },
            {"key": f"{_PLUGIN}:stop", "currentHash": "new-stop"},
            {
                "pluginId": "other",
                "key": "unrelated:stop",
                "currentHash": "do-not-trust",
            },
            None,
        ],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    data = tomllib.loads(config.read_text())
    assert data["model"] == "kept"
    assert data["features"]["other"] is True
    assert data["hooks"]["state"]["unrelated:stop"]["trusted_hash"] == "keep-other"
    assert data["hooks"]["state"][f"{_PLUGIN}:start"]["trusted_hash"] == "new-start"
    assert data["hooks"]["state"][f"{_PLUGIN}:stop"]["trusted_hash"] == "new-stop"
    assert f"{_PLUGIN}:old" not in data["hooks"]["state"]


@pytest.mark.parametrize(
    "mode", ["error", "nodata", "malformed", "exit", "exit_after_output"]
)
def test_node_reports_missing_or_bad_trust_metadata_without_granting_new_trust(
    node_install, mode
):
    result, config = node_install(mode)
    assert result.returncode != 0
    data = tomllib.loads(config.read_text())
    assert data["hooks"]["state"]["unrelated:stop"]["trusted_hash"] == "keep-other"
    assert data["hooks"]["state"][f"{_PLUGIN}:old"]["trusted_hash"] == "stale"
    assert f"{_PLUGIN}:stop" not in data["hooks"]["state"]
    assert "trust" in (result.stdout + result.stderr).lower()


def test_node_requires_a_current_hash_before_trusting_a_hook(node_install):
    result, config = node_install(
        hooks=[{"pluginId": _PLUGIN, "key": f"{_PLUGIN}:stop", "currentHash": None}]
    )
    assert result.returncode != 0
    assert "did not report trust hashes" in result.stdout + result.stderr
    assert f"{_PLUGIN}:stop" not in tomllib.loads(config.read_text())["hooks"]["state"]


def test_node_falls_back_to_feature_configuration_without_overwriting_other_features(
    node_install,
):
    result, config = node_install(feature_exit=2)
    assert result.returncode == 0, result.stdout + result.stderr
    data = tomllib.loads(config.read_text())
    assert data["features"] == {"other": True, "hooks": True, "plugin_hooks": True}


def test_node_rejects_unwritable_configuration_without_replacing_it(node_install):
    result, config = node_install(unwritable=True)
    assert result.returncode != 0
    assert config.is_dir()
    assert "automatic Codex plugin install failed" in result.stderr


def test_node_bounds_a_silent_native_app_server(node_install):
    import time

    started = time.monotonic()
    result, config = node_install(mode="hang")
    assert result.returncode != 0
    assert time.monotonic() - started < 75
    assert "timed out" in (result.stdout + result.stderr).lower()
    assert f"{_PLUGIN}:stop" not in tomllib.loads(config.read_text())["hooks"]["state"]
