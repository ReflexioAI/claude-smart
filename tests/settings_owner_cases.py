"""Real dependency preparation and publication, without an SDK or host install."""

import base64
import hashlib
import http.server
import io
import json
import os
import shutil
import subprocess
import threading
import tomllib
import urllib.request
import zipfile
from contextlib import suppress
from pathlib import Path

import pytest
from claude_smart import env_config

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def locked_owner_wheel():
    lock = tomllib.loads((ROOT / "plugin/uv.lock").read_text(encoding="utf-8"))
    package = next(item for item in lock["package"] if item["name"] == "filelock")
    wheel = package["wheels"][0]
    assert wheel["url"].startswith("https://")
    with urllib.request.urlopen(wheel["url"], timeout=30) as response:  # noqa: S310 -- Committed HTTPS artifact.
        data = response.read()
    assert hashlib.sha256(data).hexdigest() == wheel["hash"].removeprefix("sha256:")
    changed = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(data)) as source,
        zipfile.ZipFile(changed, "w") as target,
    ):
        for info in source.infolist():
            target.writestr(info, source.read(info.filename))
        target.comment = b"owned valid-archive hash mismatch"
    with zipfile.ZipFile(io.BytesIO(changed.getvalue())) as candidate:
        assert candidate.testzip() is None
    return wheel["url"].rsplit("/", 1)[-1], data, changed.getvalue()


def assert_real_settings_owner(tmp_path, case, wheel):
    node = shutil.which("node")
    uv = shutil.which("uv")
    assert node and uv, (
        "Native publisher qualification requires the CI-provisioned Node and UV"
    )
    name, good, changed = wheel
    requested = threading.Event()
    release = threading.Event()
    authorized = []
    expected_auth = "Basic " + base64.b64encode(b"owned-user:index-canary").decode()

    class Index(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *_args: object) -> None:
            pass

        def respond(self, head=False):
            if self.headers.get("Authorization") != expected_auth:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="owned"')
                self.end_headers()
                return
            if self.path.rstrip("/") == "/simple/filelock":
                data = f'<a href="/files/{name}">{name}</a>'.encode()
                kind = "text/html"
            elif self.path == "/files/" + name:
                authorized.append(True)
                requested.set()
                if case in {"cancel", "timeout"}:
                    assert release.wait(30), "Owned preparation was not released"
                data = changed if case in {"bad-hash", "no-verify-hash"} else good
                kind = "application/octet-stream"
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if not head:
                # An intentionally interrupted owned client.
                with suppress(ConnectionError):
                    self.wfile.write(data)

        def do_GET(self):
            self.respond()

        def do_HEAD(self):
            self.respond(head=True)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Index)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    home = tmp_path / "owned home λ"
    home.mkdir()
    temporary = tmp_path / "owned temporary λ"
    temporary.mkdir()
    active = tmp_path / "caller-venv"
    active.mkdir()
    (active / "marker").write_text("keep")
    poison = tmp_path / "poisoned-import-path"
    poison.mkdir()
    imported = tmp_path / "foreign-startup-ran"
    (poison / "sitecustomize.py").write_text(
        "from pathlib import Path\nPath("
        + json.dumps(str(imported))
        + ").write_text('unexpected')\n",
        encoding="utf-8",
    )
    config = tmp_path / "uv.toml"
    config.write_text(
        "[[index]]\n"
        f'url="http://owned-user:index-canary@127.0.0.1:{server.server_port}/simple"\n'
        "default=true\n"
    )
    redirected = tmp_path / "unexpected-install-target"
    if case in {"target-config", "prefix-config"}:
        field = "target" if case == "target-config" else "prefix"
        config.write_text(
            config.read_text()
            + "\n[pip]\n"
            + field
            + "="
            + json.dumps(str(redirected))
            + "\n"
        )
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("UV_", "PIP_", "PYTHON")) and key != "NODE_OPTIONS"
    }
    environment.update(
        HOME=str(home),
        USERPROFILE=str(home),
        UV_CONFIG_FILE=str(config),
        UV_CACHE_DIR=str(tmp_path / "cache"),
        VIRTUAL_ENV=str(active),
        UV_PROJECT_ENVIRONMENT=str(active),
        UV_PYTHON_DOWNLOADS="never",
        TMPDIR=str(temporary),
        TEMP=str(temporary),
        TMP=str(temporary),
        UV_CONCURRENT_DOWNLOADS="4",
        UV_CONCURRENT_INSTALLS="4",
        UV_CONCURRENT_BUILDS="1",
        RAYON_NUM_THREADS="4",
        PYTHONIOENCODING="cp1252",
        PYTHONPATH=str(poison),
    )
    if case == "no-verify-hash":
        environment["UV_NO_VERIFY_HASHES"] = "true"
    if case == "seed-config":
        environment["UV_VENV_SEED"] = "true"
    request = {
        "updates": {"CLAUDE_SMART_CLI_PATH": " C:\\Users\\owned λ "},
        "snapshot": {
            "exists": False,
            "sha256": hashlib.sha256(b"").hexdigest(),
            "legacy_marker": False,
        },
        "legacy": None,
    }
    driver = """
if (process.argv[2] === 'timeout') {
  const original = global.setTimeout;
  global.setTimeout = (callback, delay, ...args) =>
    original(callback, delay === 120000 ? 2000 : delay, ...args);
}
const owner = require(process.argv[1]);
const input = require('node:readline').createInterface({input:process.stdin});
let started = false;
input.on('line', line => {
  if (started) { if (line === 'interrupt') process.emit('SIGTERM'); return; }
  started = true;
  owner.publishInstallSettings(JSON.parse(line)).then(
    () => { process.stdout.write('saved'); input.close(); },
    error => { process.stderr.write(error.message); process.exitCode=1; input.close(); }
  );
});
"""
    child = subprocess.Popen(  # noqa: S603 -- Repository driver and owned HOME/index.
        [node, "-e", driver, str(ROOT / "bin/claude-smart.js"), case],
        cwd=tmp_path,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        if case == "cancel":
            assert child.stdin is not None
            child.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
            child.stdin.flush()
            assert requested.wait(20), "Actual UV never reached the owned index"
            child.stdin.write("interrupt\n")
            child.stdin.flush()
            output, errors = child.communicate(timeout=20)
        else:
            output, errors = child.communicate(
                json.dumps(request, ensure_ascii=False) + "\n", timeout=60
            )
        settings = home / ".claude-smart/.env"
        if case in {"success", "target-config", "prefix-config", "seed-config"}:
            assert child.returncode == 0, errors
            assert output == "saved"
            values = dict(
                entry
                for line in settings.read_text(encoding="utf-8").split("\n")
                if (entry := env_config.parse_env_line(line)) is not None
            )
            assert (
                values["CLAUDE_SMART_CLI_PATH"]
                == request["updates"]["CLAUDE_SMART_CLI_PATH"]
            )
        else:
            assert child.returncode == (143 if case == "cancel" else 1), errors
            assert not settings.exists()
            if case == "timeout":
                assert "preparation timed out" in errors
        assert authorized or case == "no-verify-hash"
        assert not redirected.exists()
        assert not list(temporary.glob("claude-smart-settings-*"))
        assert (active / "marker").read_text() == "keep"
        assert {item.name for item in active.iterdir()} == {"marker"}
        assert not imported.exists()
        assert "index-canary" not in output + errors
    finally:
        release.set()
        if child.poll() is None:
            assert child.stdin is not None
            try:
                child.stdin.write("interrupt\n")
                child.stdin.flush()
            except (BrokenPipeError, ValueError):
                pass
            child.communicate(timeout=20)
        server.shutdown()
        server.server_close()
        serving.join(timeout=3)
        assert not serving.is_alive()
