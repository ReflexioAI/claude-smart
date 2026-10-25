"""Exercise the installed hook and SDK against an owned HTTP server."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


@pytest.mark.parametrize("legacy_value", [None, "1", "", "invalid"])
def test_session_start_preserves_server_extraction_policy(tmp_path, legacy_value):
    """Repeated sessions must not read/replace the server's extraction policy.

    Keep the independent optimizer opt-in disabled so its existing configuration
    path cannot obscure an extraction-policy regression. Exercise the real hook
    dispatcher, Adapter and installed Reflexio SDK over loopback HTTP.
    """
    requests: list[tuple[str, str]] = []
    config = {
        "window_size": 23,
        "stride_size": 7,
        "future_server_field": {"preserve": [1, 2]},
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(("GET", self.path))
            if self.path == "/stall_state":
                body = {"stalled": False}
                self.send_response(200)
            elif self.path == "/api/get_config":
                body = config
                self.send_response(200)
            else:
                body = {"error": "unexpected request"}
                self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def do_POST(self):
            requests.append(("POST", self.path))
            self.send_error(500, "SessionStart must not write extraction policy")

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    project = tmp_path / "project"
    project.mkdir()
    env = os.environ.copy()
    for name in (
        "CLAUDE_SMART_INTERNAL",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_SMART_MANAGED_SETUP",
        "CLAUDE_SMART_READ_ONLY",
        "CLAUDE_SMART_HOOK_LOG",
    ):
        env.pop(name, None)
    env.update(
        HOME=str(tmp_path),
        USERPROFILE=str(tmp_path),
        REFLEXIO_URL=f"http://127.0.0.1:{server.server_port}",
        REFLEXIO_API_KEY="owned-test-key",
        CLAUDE_SMART_ENABLE_OPTIMIZER="0",
        CLAUDE_SMART_STATE_DIR=str(tmp_path / "state"),
        PYTHONPATH=str(Path(__file__).resolve().parents[1] / "plugin" / "src"),
    )
    for name in ("CLAUDE_SMART_WINDOW_SIZE", "CLAUDE_SMART_STRIDE_SIZE"):
        env.pop(name, None)
        if legacy_value is not None:
            env[name] = legacy_value
    try:
        for index in range(2):
            # Simulate an operator changing server policy between sessions.
            config["window_size"] = 23 + index
            config["stride_size"] = 7 + index
            expected = copy.deepcopy(config)
            result = subprocess.run(
                [sys.executable, "-m", "claude_smart.hook", "session-start"],
                input=json.dumps({"session_id": f"s{index}", "cwd": str(project)}),
                text=True,
                capture_output=True,
                cwd=project,
                env=env,
                timeout=20,
                check=True,
            )
            assert json.loads(result.stdout) == {
                "continue": True,
                "suppressOutput": True,
            }
            assert config == expected
        assert requests == [("GET", "/stall_state"), ("GET", "/stall_state")]
        # The dispatcher's fail-open output alone cannot establish success.
        records = [
            json.loads(line)
            for line in (tmp_path / ".claude-smart" / "hook.log")
            .read_text()
            .splitlines()
        ]
        assert len(records) == 2
        assert all(record["handler_status"] == "ok" for record in records)
        assert all(record["internal_skipped"] is False for record in records)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
