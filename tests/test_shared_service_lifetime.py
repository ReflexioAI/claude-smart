"""Session completion cannot terminate services shared with other sessions."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.request import urlopen

import pytest

from tests.test_install_scripts import (
    REPO_ROOT,
    _backend_service_env,
    _isolated_env,
    _seed_backend_service_test_plugin,
    _write_executable,
)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process ownership fixture")
@pytest.mark.parametrize("service", ["backend", "dashboard"])
def test_session_end_preserves_shared_service_until_explicit_stop(
    tmp_path: Path, service: str
) -> None:
    # A harmless real listener represents a shared service. Metadata probes
    # identify this owned fixture; signals and HTTP requests remain real.
    server = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-c",
            "from http.server import HTTPServer, BaseHTTPRequestHandler\n"
            "class Handler(BaseHTTPRequestHandler):\n"
            " def do_GET(self):\n"
            "  self.send_response(200); self.end_headers(); self.wfile.write(b'ready')\n"
            " def log_message(self, *args): pass\n"
            "server = HTTPServer(('127.0.0.1', 0), Handler)\n"
            "print(server.server_port, flush=True)\n"
            "server.serve_forever()\n",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        assert server.stdout is not None
        port = int(server.stdout.readline().strip())
        plugin = _seed_backend_service_test_plugin(tmp_path, port=port)
        state = tmp_path / ".claude-smart"
        state.mkdir()
        pid_file = state / f"{service}.pid"
        pid_file.write_text(f"{server.pid}\n")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _write_executable(
            bin_dir / "ps",
            "#!/bin/sh\n"
            'case " $* " in\n'
            ' *" command= "*)\n'
            '  case " $* " in\n'
            '   *" $OWNED_PID "*) printf "%s\\n" "$OWNED_COMMAND"; exit 0 ;;\n'
            "  esac ;;\n"
            "esac\n"
            'exec /bin/ps "$@"\n',
        )
        _write_executable(
            bin_dir / "lsof",
            "#!/bin/sh\n"
            'case " $* " in\n'
            ' *" -d cwd -Fn "*) printf "n%s\\n" "$OWNED_ROOT" ;;\n'
            "esac\n",
        )
        env = _backend_service_env(
            tmp_path,
            bin_dir,
            port,
            BACKEND_PORT=str(port),
            EMBEDDING_PORT=str(port),
            DASHBOARD_PORT=str(port),
            OWNED_PID=str(server.pid),
            OWNED_ROOT=str(plugin),
            OWNED_COMMAND=(
                "python -m reflexio.cli services start --only backend --no-reload"
                if service == "backend"
                else f"node {plugin}/dashboard/node_modules/next start"
            ),
            CLAUDE_SMART_BACKEND_STOP_ON_END="1",
            CLAUDE_SMART_DASHBOARD_STOP_ON_END="1",
        )
        script = plugin / "scripts" / f"{service}-service.sh"

        def request() -> bytes:
            with urlopen(f"http://127.0.0.1:{port}/", timeout=3) as response:
                return response.read()

        assert request() == b"ready"
        result = subprocess.run(
            ["bash", str(script), "session-end"],
            cwd=plugin,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert server.poll() is None, "one session stopped the shared service"
        assert request() == b"ready", "another session must still be served"
        assert pid_file.exists(), "session completion discarded service ownership"

        result = subprocess.run(
            ["bash", str(script), "stop"],
            cwd=plugin,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        server.wait(timeout=5)
        assert not pid_file.exists()
    finally:
        if server.poll() is None:
            server.terminate()
            server.wait(timeout=5)
        if server.stdout is not None:
            server.stdout.close()
        if server.stderr is not None:
            server.stderr.close()


@pytest.mark.parametrize("service", ["backend", "dashboard"])
@pytest.mark.parametrize("host", ["claude-code", "codex"])
def test_cached_session_end_does_not_initialize_shared_service_state(
    tmp_path: Path, service: str, host: str
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    env = _isolated_env(home)
    env.update(
        CLAUDE_SMART_HOST=host,
        CLAUDE_SMART_BACKEND_STOP_ON_END="1",
        CLAUDE_SMART_DASHBOARD_STOP_ON_END="1",
        REFLEXIO_URL="",
        BACKEND_PORT="0",
        EMBEDDING_PORT="0",
        DASHBOARD_PORT="0",
    )
    result = subprocess.run(
        [
            "bash",
            str(REPO_ROOT / "plugin/scripts" / f"{service}-service.sh"),
            "session-end",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    expected = {"continue": True}
    if host == "claude-code":
        expected["suppressOutput"] = True
    assert json.loads(result.stdout) == expected
    assert list(home.iterdir()) == []
