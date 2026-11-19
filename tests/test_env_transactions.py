"""Real file/process invariants of the Python dotenv publication boundary."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from claude_smart import env_config

from tests.settings_owner_cases import assert_real_settings_owner
from tests.settings_owner_cases import locked_owner_wheel as locked_owner_wheel


def _append_settings(path, values):
    """Test the store's RMW boundary with a simple append-only client."""
    content = "".join(f'{key}="{value}"\n' for key, value in values.items())
    return env_config._edit_env_file(path, lambda previous: (previous + content, None))


@contextmanager
def _paused_writer(path: Path, *, before_replace: bool = False):
    """An owned process runs the public helper and pauses while holding its lock."""
    ready = path.parent / "writer-ready"
    program = """
import sys
from pathlib import Path
from claude_smart import env_config
path, ready = Path(sys.argv[1]), Path(sys.argv[2])
if sys.argv[3] == 'replace':
    original = Path.replace
    def pause(self, target):
        ready.write_text('ready')
        sys.stdin.readline()
        return original(self, target)
    Path.replace = pause
else:
    def pause(previous):
        ready.write_text('ready')
        sys.stdin.readline()
        return previous + 'FIRST="one"\\n', None
    env_config._edit_env_file(path, pause)
    raise SystemExit(0)
env_config._edit_env_file(path, lambda previous: (previous + 'FIRST="one"\\n', None))
"""
    environment = dict(os.environ)
    environment.update(HOME=str(path.parent), USERPROFILE=str(path.parent))
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(Path(__file__).resolve().parents[1] / "plugin/src"), *sys.path]
    )
    with subprocess.Popen(  # noqa: S603 — owned test child.
        [
            sys.executable,
            "-c",
            program,
            str(path),
            str(ready),
            "replace" if before_replace else "edit",
        ],
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as child:
        try:
            deadline = time.monotonic() + 10
            while (
                not ready.exists()
                and child.poll() is None
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            assert ready.exists(), "The owned writer never reached its transaction"
            yield child
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)


def test_repair_preserves_connections_comments_and_duplicate_host_choices(tmp_path):
    path = tmp_path / ".env"
    original = "# keep\nOTHER=opaque\\path\nREFLEXIO_URL=http://localhost:8081/\nREFLEXIO_API_KEY=local-key\nREFLEXIO_USER_ID=shared\nCLAUDE_SMART_HOST=codex\nCLAUDE_SMART_HOST=opencode\n"
    path.write_text(original)
    env_config.repair_bootstrap_env(path)
    assert path.read_text().startswith(original)
    assert not (tmp_path / ".env.bak").exists()


def test_repair_preserves_caps_read_only_and_host_and_is_idempotent(
    tmp_path, monkeypatch
):
    path = tmp_path / ".env"
    original = 'CLAUDE_SMART_HOST=opencode\nCLAUDE_SMART_READ_ONLY="1"\nREFLEXIO_ROW_LIMIT_INTERACTIONS=0\nREFLEXIO_API_KEY="keep"\n'
    path.write_text(original)
    monkeypatch.setenv("REFLEXIO_ROW_LIMIT_INTERACTIONS", "9000")
    assert env_config.repair_bootstrap_env(path) == ["CLAUDE_SMART_USE_LOCAL_CLI"]
    assert path.read_text().startswith(original)
    before = path.read_bytes()
    assert env_config.repair_bootstrap_env(path) == []
    assert path.read_bytes() == before


@pytest.mark.parametrize("operation", ["upsert", "defaults", "install"])
def test_both_mutators_reject_a_live_transaction_then_read_latest_state(
    tmp_path, operation, monkeypatch
):
    path = tmp_path / ".env"
    path.write_text("ORIGINAL=keep\n")
    monkeypatch.setattr(env_config, "CLAUDE_SMART_ENV_PATH", path)

    def save():
        if operation == "upsert":
            _append_settings(path, {"SECOND": "two"})
        elif operation == "defaults":
            env_config.repair_bootstrap_env(path)
        else:
            env_config.publish_install({"SECOND": "two"}, _install_snapshot(path), None)

    with _paused_writer(path) as child:
        with pytest.raises(RuntimeError, match="settings are busy"):
            save()
        assert path.read_text() == "ORIGINAL=keep\n"
        assert child.stdin is not None
        child.stdin.write("continue\n")
        child.stdin.flush()
        assert child.wait(timeout=10) == 0
    save()
    text = path.read_text()
    assert 'FIRST="one"\n' in text
    assert "ORIGINAL=keep\n" in text
    if operation == "defaults":
        assert 'CLAUDE_SMART_READ_ONLY="0"\n' in text
    else:
        values = dict(
            entry
            for line in text.split("\n")
            if (entry := env_config.parse_env_line(line)) is not None
        )
        assert values["SECOND"] == "two"


def test_process_death_before_replace_preserves_original_and_releases_lock(tmp_path):
    path = tmp_path / ".env"
    path.write_text("ORIGINAL=keep\n")
    with _paused_writer(path, before_replace=True) as child:
        assert path.read_text() == "ORIGINAL=keep\n"
        candidates = list(tmp_path.glob(".env-*"))
        assert len(candidates) == 1
        if os.name != "nt":
            assert stat.S_IMODE(candidates[0].stat().st_mode) == 0o700
            assert stat.S_IMODE((candidates[0] / "settings").stat().st_mode) == 0o600
        child.kill()
        child.wait(timeout=10)
    # No stale-PID recovery or lock-file removal is necessary after kernel cleanup.
    _append_settings(path, {"SECOND": "two"})
    assert path.read_text() == 'ORIGINAL=keep\nSECOND="two"\n'


@pytest.mark.parametrize("failure", ["encode", "fsync", "replace"])
def test_failure_before_publication_preserves_original_and_cleans_temporary(
    tmp_path, monkeypatch, failure
):
    path = tmp_path / ".env"
    original = b"ORIGINAL=keep\n"
    path.write_bytes(original)

    def fail(*_args, **_kwargs):
        raise OSError("owned injected publication failure")

    if failure == "fsync":
        monkeypatch.setattr(env_config.os, "fsync", fail)
    elif failure == "replace":
        monkeypatch.setattr(Path, "replace", fail)
    value = "\ud800" if failure == "encode" else "two"
    with pytest.raises((OSError, UnicodeEncodeError)):
        _append_settings(path, {"SECOND": value})
    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".env-*"))


@pytest.mark.parametrize("target", [".env", ".env.lock"])
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory", "fifo"])
def test_unsafe_original_or_lock_is_rejected(tmp_path, target, kind):
    path = tmp_path / ".env"
    original = tmp_path / "original"
    original.write_text("PRIVATE=keep\n")
    unsafe = tmp_path / target
    if kind == "symlink":
        try:
            unsafe.symlink_to(original)
        except OSError:
            pytest.skip("This account cannot create native symlinks")
    elif kind == "hardlink":
        unsafe.hardlink_to(original)
    elif kind == "directory":
        unsafe.mkdir()
    else:
        if not hasattr(os, "mkfifo"):
            pytest.skip("Native FIFOs are POSIX-only")
        os.mkfifo(unsafe)
    with pytest.raises(ValueError, match="regular, unlinked"):
        _append_settings(path, {"NEW": "two"})
    assert original.read_text() == "PRIVATE=keep\n"
    assert not list(tmp_path.glob(".env-*"))


def test_invalid_utf8_is_not_treated_as_empty_and_is_redacted(tmp_path):
    path = tmp_path / ".env"
    original = b"SECRET=fake-credential\xff\n"
    path.write_bytes(original)
    with pytest.raises(ValueError, match="valid UTF-8") as error:
        env_config.repair_bootstrap_env(path)
    assert "credential" not in str(error.value)
    assert path.read_bytes() == original


@pytest.mark.skipif(os.name == "nt", reason="flock is POSIX-only")
def test_unsupported_native_filesystem_never_falls_back_to_a_marker_lock(
    tmp_path, monkeypatch
):
    import errno
    import fcntl

    path = tmp_path / ".env"
    path.write_text("ORIGINAL=keep\n")

    def unsupported(*_args):
        raise OSError(errno.ENOSYS, "owned unsupported flock")

    monkeypatch.setattr(fcntl, "flock", unsupported)
    with pytest.raises((OSError, NotImplementedError)):
        _append_settings(path, {"NEW": "two"})
    assert path.read_text() == "ORIGINAL=keep\n"
    assert not list(tmp_path.glob(".env-*"))


@pytest.mark.parametrize(
    "separator",
    ["\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"],
)
def test_unsafe_parent_default_is_rejected_without_replacing_original(
    tmp_path, monkeypatch, separator
):
    path = tmp_path / ".env"
    path.write_text("ORIGINAL=keep\n")
    monkeypatch.setenv("REFLEXIO_ROW_LIMIT_INTERACTIONS", f"0{separator}OTHER=bad")
    with pytest.raises(ValueError, match="Invalid local environment default"):
        env_config.repair_bootstrap_env(path)
    assert path.read_text() == "ORIGINAL=keep\n"


def test_temporary_is_private_before_secret_content_is_written(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("POSIX mode bits do not establish Windows ACL privacy")
    path = tmp_path / ".env"
    modes = []
    original = os.fdopen

    def observe(fd, mode):
        if mode == "wb":
            modes.append(stat.S_IMODE(os.fstat(fd).st_mode))
        return original(fd, mode)

    monkeypatch.setattr(env_config.os, "fdopen", observe)
    prior_umask = os.umask(0)
    try:
        _append_settings(path, {"KEY": "fake-secret"})
    finally:
        os.umask(prior_umask)
    assert modes == [0o600]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_existing_file_does_not_require_posix_nonblocking_flag(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("OLD=keep\n")
    from types import SimpleNamespace

    # Simulate only our module's Windows flag surface; the native POSIX lock
    # must keep its real OS API rather than inherit this simulated platform.
    flags = SimpleNamespace(**vars(os))
    flags.__dict__.pop("O_NONBLOCK", None)
    monkeypatch.setattr(env_config, "os", flags)
    _append_settings(path, {"NEW": "two"})
    assert path.read_text() == 'OLD=keep\nNEW="two"\n'


@pytest.mark.skipif(os.name == "nt", reason="fchmod is POSIX-only")
def test_native_lock_acquisition_failure_closes_its_descriptor(tmp_path, monkeypatch):
    import errno

    path = tmp_path / ".env"
    path.write_text("ORIGINAL=keep\n")
    real_open, real_close, real_fchmod = os.open, os.close, os.fchmod
    opened, closed = [], []

    def record_open(name, *args, **kwargs):
        descriptor = real_open(name, *args, **kwargs)
        if Path(name).name == ".env.lock":
            opened.append(descriptor)
        return descriptor

    def record_close(descriptor):
        closed.append(descriptor)
        return real_close(descriptor)

    def fail_mode(descriptor, mode):
        if descriptor in opened:
            raise OSError(errno.EIO, "owned injected fchmod failure")
        return real_fchmod(descriptor, mode)

    monkeypatch.setattr(env_config.os, "open", record_open)
    monkeypatch.setattr(env_config.os, "close", record_close)
    monkeypatch.setattr(env_config.os, "fchmod", fail_mode)
    try:
        with pytest.raises(OSError, match="owned injected fchmod failure"):
            _append_settings(path, {"NEW": "two"})
        assert opened
        assert all(descriptor in closed for descriptor in opened)
        assert path.read_text() == "ORIGINAL=keep\n"
    finally:
        # Even a broken dependency/counterfactual must not leak owned test FDs.
        for descriptor in opened:
            if descriptor not in closed:
                real_close(descriptor)


@pytest.mark.skipif(os.name != "nt", reason="Native Windows read-only attributes")
@pytest.mark.parametrize("operation", ["upsert", "defaults"])
def test_changed_publication_repairs_read_only_attribute(tmp_path, operation):
    path = tmp_path / ".env"
    path.write_text("ORIGINAL=keep\n")
    path.chmod(stat.S_IREAD)
    try:
        if operation == "upsert":
            _append_settings(path, {"NEW": "two"})
            assert 'NEW="two"' in path.read_text()
        else:
            env_config.repair_bootstrap_env(path)
            assert 'CLAUDE_SMART_READ_ONLY="0"' in path.read_text()
            assert "CLAUDE_SMART_HOST" not in path.read_text()
    finally:
        path.chmod(0o600)


def _native_powershell(script):
    import base64
    import json

    powershell = shutil.which("powershell.exe")
    assert powershell is not None
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    result = subprocess.run(  # noqa: S603 — repository-controlled test command.
        [powershell, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(result.stdout)


def _windows_allow_principals(path):
    # Only owned test paths reach PowerShell, never configuration content.
    literal = str(path).replace("'", "''")
    return _native_powershell(f"""
$ErrorActionPreference = 'Stop'
$acl = Get-Acl -LiteralPath '{literal}'
$sids = @($acl.Access | Where-Object {{ $_.AccessControlType -eq 'Allow' }} | ForEach-Object {{ $_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value }})
ConvertTo-Json -InputObject @{{ owner = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value; allowed = $sids }} -Compress
""")


@pytest.mark.skipif(os.name != "nt", reason="Native Windows DACLs")
@pytest.mark.parametrize("existing", [False, True])
def test_windows_publication_is_private_in_a_broadly_readable_parent(
    tmp_path, monkeypatch, existing
):
    parent = tmp_path / "broad parent"
    parent.mkdir()
    path = parent / ".env"
    literal = str(parent).replace("'", "''")
    # Broaden ONLY this disposable directory. The original file, if present,
    # is then protected separately to reproduce private-file/public-parent use.
    _native_powershell(f"""
$ErrorActionPreference = 'Stop'
$p = '{literal}'
$acl = Get-Acl -LiteralPath $p
$sid = [System.Security.Principal.SecurityIdentifier]::new('S-1-1-0')
$rule = [System.Security.AccessControl.FileSystemAccessRule]::new($sid, 'ReadAndExecute', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
$acl.AddAccessRule($rule)
Set-Acl -LiteralPath $p -AclObject $acl
ConvertTo-Json -InputObject $true
""")
    if existing:
        path.write_text("ORIGINAL=keep\n")
        file_literal = str(path).replace("'", "''")
        _native_powershell(f"""
$ErrorActionPreference = 'Stop'
$acl = Get-Acl -LiteralPath '{file_literal}'
$acl.SetAccessRuleProtection($true, $false)
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$rule = [System.Security.AccessControl.FileSystemAccessRule]::new($sid, 'FullControl', 'Allow')
$acl.SetAccessRule($rule)
Set-Acl -LiteralPath '{file_literal}' -AclObject $acl
ConvertTo-Json -InputObject $true
""")
    assert "S-1-1-0" in _windows_allow_principals(parent)["allowed"]
    control = parent / "inherited-control"
    control.touch()
    assert "S-1-1-0" in _windows_allow_principals(control)["allowed"]
    control.unlink()
    if existing:
        assert "S-1-1-0" not in _windows_allow_principals(path)["allowed"]
    observed = []
    real_fdopen = os.fdopen

    def before_content(fd, mode):
        if mode == "wb":
            candidates = list(parent.glob(".env-*/settings"))
            assert len(candidates) == 1
            assert candidates[0].stat().st_size == 0
            observed.append(_windows_allow_principals(candidates[0]))
        return real_fdopen(fd, mode)

    monkeypatch.setattr(env_config.os, "fdopen", before_content)
    _append_settings(path, {"KEY": "fake-secret"})
    observed.append(_windows_allow_principals(path))
    assert len(observed) == 2
    for acl in observed:
        # Current user/owner, SYSTEM and administrators are privileged readers;
        # Everyone/Users/Authenticated Users must not inherit access here.
        privileged = {acl["owner"], "S-1-5-18", "S-1-5-32-544", "S-1-3-4"}
        assert acl["allowed"]
        assert set(acl["allowed"]) <= privileged
    assert 'KEY="fake-secret"' in path.read_text()
    assert not list(parent.glob(".env-*"))


def test_publication_distinguishes_absent_empty_and_deleted(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    original = "# preserve\nOTHER=opaque\\path\nREMOVE=old\n"
    path.write_text(original)
    env_config.update_settings(
        {"REFLEXIO_NATIVE_CLI_MODEL": "", "REMOVE": None},
        {"REFLEXIO_NATIVE_CLI_MODEL": [], "REMOVE": ["REMOVE=old"]},
        path,
    )
    assert (
        path.read_text()
        == "# preserve\nOTHER=opaque\\path\nREFLEXIO_NATIVE_CLI_MODEL=\n"
    )
    before = path.read_bytes()
    env_config.update_settings({}, {}, path)
    assert path.read_bytes() == before


def test_confirmed_edit_collapses_duplicates_without_touching_other_choices(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".env"
    path.write_text("export\tMODEL=first\nOTHER=keep\nMODEL=second\nMODEL=\n")
    env_config.update_settings(
        {"MODEL": "local-cli/opencode/provider/model#high"},
        {"MODEL": ["export\tMODEL=first", "MODEL=second", "MODEL="]},
        path,
    )
    assert (
        path.read_text() == "MODEL=local-cli/opencode/provider/model#high\nOTHER=keep\n"
    )


@pytest.mark.parametrize(
    "latest", ["", "MODEL=\n", "MODEL=other\n", "MODEL=old\nMODEL=other\n"]
)
def test_stale_confirmation_never_overwrites_relevant_changes(
    tmp_path: Path, latest: str
) -> None:
    path = tmp_path / ".env"
    path.write_text(latest)
    with pytest.raises(env_config.SettingsConflictError, match="reload and retry"):
        env_config.update_settings({"MODEL": "new"}, {"MODEL": ["MODEL=old"]}, path)
    assert path.read_text() == latest


def test_rechecked_read_dependencies_and_unrelated_updates(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("MODE=local\nMODEL=old\nUNRELATED=newer\n")
    expected = {"MODEL": ["MODEL=old"], "MODE": ["MODE=local"]}
    env_config.update_settings({"MODEL": "new"}, expected, path)
    assert "UNRELATED=newer\n" in path.read_text()
    before = path.read_bytes()
    with pytest.raises(env_config.SettingsConflictError):
        env_config.update_settings(
            {"MODEL": "other"}, {"MODEL": ["MODEL=new"], "MODE": ["MODE=managed"]}, path
        )
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "value",
    [
        "\nINJECT=1",
        "x\u2028INJECT=1",
        "x\0y",
        "${OTHER}",
        "'a\"b",
        r" \\server\share ",
    ],
)
def test_non_roundtrippable_value_is_rejected_before_mutation(
    tmp_path: Path, value: str
) -> None:
    path = tmp_path / ".env"
    path.write_text("KEEP=original\n")
    with pytest.raises(ValueError, match="consistently"):
        env_config.update_settings({"MODEL": value}, {"MODEL": []}, path)
    assert path.read_text() == "KEEP=original\n"
    assert not path.with_name(".env.lock").exists()


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize(
    "value",
    [
        " leading",
        "trailing ",
        '"quoted"',
        "'quoted'",
        "value # comment",
        "\ufeffowned",
        "owned\ufeff",
        "'literal\\q",
        r" C:\temp\owned ",
        "plain\\",
    ],
)
def test_literal_publication_preserves_exact_value_and_original_preconditions(
    tmp_path: Path, value: str, existing: bool
) -> None:
    path = tmp_path / ".env"
    prefix = b"# keep\r\nUNRELATED=opaque\\path\r\n"
    path.write_bytes(prefix + (b"MODEL=old\r\n" if existing else b""))
    previous = ["MODEL=old"] if existing else []
    env_config.update_settings({"MODEL": value}, {"MODEL": previous}, path)
    published = path.read_bytes()
    assert published.startswith(prefix)
    lines = [line.removesuffix("\r") for line in published.decode().split("\n")]
    assert [
        entry[1]
        for line in lines
        if (entry := env_config.parse_env_line(line)) is not None
        and entry[0] == "MODEL"
    ] == [value]
    originals = [
        line
        for line in lines
        if (entry := env_config.parse_env_line(line)) and entry[0] == "MODEL"
    ]
    env_config.update_settings({"MODEL": value}, {"MODEL": originals}, path)
    assert path.read_bytes() == published
    with pytest.raises(env_config.SettingsConflictError):
        env_config.update_settings({"MODEL": "stale"}, {"MODEL": previous}, path)
    assert path.read_bytes() == published


@pytest.mark.parametrize(
    "value", [" private-owned-slash\\", "'private-owned-literal\\"]
)
@pytest.mark.parametrize(
    "following", ["", "AFTER='owned-keep'\r\n", 'AFTER="owned-keep"\r\n']
)
def test_quoted_dangling_backslash_is_rejected_before_mutation(
    tmp_path: Path, value: str, following: str
) -> None:
    path = tmp_path / ".env"
    original = ("MODEL=old\r\n" + following).encode()
    path.write_bytes(original)
    # A later quoted row can change how a dangling backslash is parsed.
    # Even a currently healthy EOF encoding must remain safe to extend.
    with pytest.raises(ValueError, match="consistently"):
        env_config.update_settings({"MODEL": value}, {"MODEL": ["MODEL=old"]}, path)
    assert path.read_bytes() == original
    assert not path.with_name(".env.lock").exists()


def test_confirmed_publication_shares_native_repair_lock(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("MODEL=old\n")
    with _paused_writer(path), pytest.raises(env_config.SettingsBusyError):
        env_config.update_settings({"MODEL": "new"}, {"MODEL": ["MODEL=old"]}, path)
    env_config.update_settings({"MODEL": "new"}, {"MODEL": ["MODEL=old"]}, path)
    assert path.read_text() == "MODEL=new\n"


@pytest.mark.parametrize("stdin_encoding", ["utf-8", "cp1252"])
def test_prepared_module_publishes_stdin_without_values_in_output(
    tmp_path: Path,
    stdin_encoding: str,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    environment = {
        key: os.environ[key]
        for key in ("PATH", "SystemRoot", "LANG", "TMPDIR")
        if key in os.environ
    }
    environment.update(
        HOME=str(home),
        USERPROFILE=str(home),
        PYTHONPATH=os.pathsep.join(
            [str(Path(__file__).resolve().parents[1] / "plugin/src"), *sys.path]
        ),
    )
    payload = {
        "updates": {"REFLEXIO_API_KEY": "private-canary-λ"},
        "expected": {"REFLEXIO_API_KEY": []},
    }
    program = (
        "import runpy,sys;sys.stdin.reconfigure(encoding=" + repr(stdin_encoding) + ");"
        "sys.argv=['claude_smart.env_config','publish'];"
        "runpy.run_module('claude_smart.env_config',run_name='__main__')"
    )
    args = [sys.executable, "-c", program]
    result = subprocess.run(  # noqa: S603 — fixed prepared-module invocation.
        args,
        input=json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
        env=environment,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "private-canary" not in result.stdout + result.stderr
    assert (home / ".claude-smart/.env").read_text(
        encoding="utf-8"
    ) == "REFLEXIO_API_KEY=private-canary-λ\n"
    result = subprocess.run(  # noqa: S603 — fixed prepared-module invocation.
        args,
        input=json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
        env=environment,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 73
    assert "private-canary" not in result.stdout + result.stderr
    payload = {
        "updates": {"REFLEXIO_API_KEY": "new-owned-key"},
        "expected": {"REFLEXIO_API_KEY": ["REFLEXIO_API_KEY=private-canary-λ"]},
    }
    result = subprocess.run(  # noqa: S603 — same owned module invocation.
        args,
        input=json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
        env=environment,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "private-canary" not in result.stdout + result.stderr
    assert (home / ".claude-smart/.env").read_text(
        encoding="utf-8"
    ) == "REFLEXIO_API_KEY=new-owned-key\n"


@pytest.mark.parametrize(
    "separator", ["\u2028", "\u2029", "\x85", "\v", "\f", "\x1c", "\x1d", "\x1e", "\r"]
)
@pytest.mark.parametrize("quoted", [False, True])
def test_publication_preserves_unrelated_physical_value_bytes(
    tmp_path, separator, quoted
):
    path = tmp_path / ".env"
    value = f"left{separator}right"
    if quoted:
        value = f'"{value}"'
    original = f"# keep\nOTHER={value}\nMODEL=old\n".encode()
    path.write_bytes(original)
    env_config.update_settings({"MODEL": "new"}, {"MODEL": ["MODEL=old"]}, path)
    assert path.read_bytes() == original.replace(b"MODEL=old", b"MODEL=new")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("terminated", [False, True])
def test_publication_preserves_line_endings_and_final_unrelated_span(
    tmp_path, newline, terminated
):
    path = tmp_path / ".env"
    original = f"# keep{newline}MODEL=old{newline}OTHER=untouched" + (
        newline if terminated else ""
    )
    path.write_bytes(original.encode())
    env_config.update_settings({"MODEL": "new"}, {"MODEL": ["MODEL=old"]}, path)
    assert path.read_bytes() == original.replace("MODEL=old", "MODEL=new").encode()


def test_publication_does_not_edit_assignment_text_inside_unicode_value(tmp_path):
    path = tmp_path / ".env"
    original = "OTHER=left\u2028MODEL=inside-value"
    path.write_bytes(original.encode())
    env_config.update_settings({"MODEL": "new"}, {"MODEL": []}, path)
    assert path.read_bytes() == (original + "\nMODEL=new\n").encode()


@pytest.mark.parametrize("export", ["", "export\t"])
def test_publication_compares_original_bom_assignment_and_preserves_it(
    tmp_path, export
):
    path = tmp_path / ".env"
    original = "\ufeff" + export + "MODEL=old"
    path.write_bytes((original + "\r\n").encode())
    env_config.update_settings({"NEW": "value"}, {"NEW": [], "MODEL": [original]}, path)
    assert path.read_bytes() == (original + "\r\nNEW=value\r\n").encode()
    before = path.read_bytes()
    with pytest.raises(env_config.SettingsConflictError):
        env_config.update_settings(
            {"NEW": "other"},
            {"NEW": ["NEW=value"], "MODEL": [original.replace("old", "stale")]},
            path,
        )
    assert path.read_bytes() == before


INSTALL_COMPLETION = "# claude-smart: legacy-env checked"


@pytest.fixture
def install_files(tmp_path, monkeypatch):
    target = tmp_path / ".claude-smart/.env"
    source = tmp_path / ".reflexio/.env"
    target.parent.mkdir()
    source.parent.mkdir()
    monkeypatch.setattr(env_config, "CLAUDE_SMART_ENV_PATH", target)
    monkeypatch.setattr(env_config, "LEGACY_REFLEXIO_ENV_PATH", source)
    return target, source


def _install_snapshot(target):
    return {
        "exists": target.exists(),
        "sha256": hashlib.sha256(
            target.read_bytes() if target.exists() else b""
        ).hexdigest(),
        "legacy_marker": (target.parent / "legacy-reflexio-env-checked").exists(),
    }


@pytest.mark.parametrize(
    "case",
    [
        "attached",
        "colocated",
        "unchanged",
        "stale",
        "shadow",
        "timeout",
        "editor",
        "loader",
    ],
)
def test_prepared_settings_verify_staged_windows_backend_before_commit(
    tmp_path, monkeypatch, case
):
    home = tmp_path / "owned home λ"
    target = home / ".claude-smart/.env"
    target.parent.mkdir(parents=True)
    monkeypatch.setattr(env_config, "CLAUDE_SMART_ENV_PATH", target)
    marker = target.parent / "install-failed"
    marker.write_bytes(b"previous installer diagnostic")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    importer = scripts / "backend-inference-owner.py"
    importer.write_text(
        "from pathlib import Path\n"
        "import os, sys\n"
        'assert sys.argv[1:] == ["--validate-windows-embedding"]\n'
        'assert not Path(".env").exists()\n'
        'staged = Path(os.environ["REFLEXIO_ENV_FILE"])\n'
        "assert staged.parent == Path.cwd()\n"
        "content = staged.read_bytes()\n"
        '(Path(__file__).parents[1] / "observed-settings").write_bytes(content)\n'
        'sys.exit(0 if b"REFLEXIO_EMBEDDING_SERVICE_URL=" in content else 77)\n',
        encoding="utf-8",
    )
    if case == "editor":
        importer.write_text(
            importer.read_text(encoding="utf-8").replace(
                "sys.exit(",
                f"with Path({str(target)!r}).open('ab') as live:\n"
                "    live.write(b'# external editor\\n')\nsys.exit(",
            ),
            encoding="utf-8",
        )
    loader = tmp_path / "claude-smart.js"
    if case == "loader":
        importer.write_text(
            importer.read_text(encoding="utf-8").replace(
                "sys.exit(",
                f"Path({str(loader)!r}).write_bytes(b'foreign loader\\n')\nsys.exit(",
            ),
            encoding="utf-8",
        )
    if case == "timeout":
        importer.write_text(
            importer.read_text(encoding="utf-8").replace(
                "sys.exit(",
                "with staged.open('rb'):\n    __import__('time').sleep(60)\nsys.exit(",
            ),
            encoding="utf-8",
        )
        run = subprocess.run

        def short_timeout(*args, **kwargs):
            assert kwargs["timeout"] == 15
            return run(*args, **{**kwargs, "timeout": 2})

        monkeypatch.setattr(env_config.subprocess, "run", short_timeout)
    local = "http://localhost:8071/"
    if case == "unchanged":
        env_config.publish_install(
            {"REFLEXIO_URL": local}, _install_snapshot(target), None, prepared=True
        )
    else:
        target.write_bytes(b"REFLEXIO_URL=https://old.example.invalid/\n")
    if case in {"attached", "editor", "loader"}:
        with target.open("ab") as settings:
            settings.write(b"REFLEXIO_EMBEDDING_SERVICE_URL=http://localhost:8093/\n")
    if case == "shadow":
        (tmp_path / ".env").write_bytes(
            b"REFLEXIO_EMBEDDING_SERVICE_URL=http://localhost:8093/\n"
        )
    snapshot = _install_snapshot(target)
    if case == "stale":
        with target.open("ab") as settings:
            settings.write(b"# edited\n")
    original = target.read_bytes()
    observed = tmp_path / "observed-settings"
    monkeypatch.chdir(tmp_path)
    args = ({"REFLEXIO_URL": local}, snapshot, None)
    if case == "attached":
        env_config.publish_install(*args, prepared=True, check_windows_embedding=True)
        assert f"REFLEXIO_URL={local}\n".encode() in target.read_bytes()
    elif case in {"stale", "editor"}:
        with pytest.raises(env_config.SettingsConflictError):
            env_config.publish_install(
                *args, prepared=True, check_windows_embedding=True
            )
    else:
        with pytest.raises(env_config.SettingsPreflightError) as rejected:
            env_config.publish_install(
                *args,
                prepared=True,
                check_windows_embedding=True,
                opencode_loader={
                    "path": str(loader),
                    "contents": None,
                    "identity": None,
                }
                if case == "loader"
                else None,
            )
        assert rejected.value.code == {"shadow": 79, "timeout": 78, "loader": 80}.get(
            case, 77
        )
        if case == "loader":
            assert loader.read_bytes() == b"foreign loader\n"
    if case != "attached":
        assert target.read_bytes() == original + (
            b"# external editor\n" if case == "editor" else b""
        )
    if case in {"stale", "shadow"}:
        assert not observed.exists()
    elif case in {"attached", "unchanged"}:
        assert observed.read_bytes() == target.read_bytes()
    else:
        assert b"REFLEXIO_ROW_LIMIT_INTERACTIONS=" in observed.read_bytes()
        assert f"REFLEXIO_URL={local}\n".encode() in observed.read_bytes()
    assert list(target.parent.glob(".env-*")) == []
    assert marker.read_bytes() == b"previous installer diagnostic"
    assert not (target.parent / "install.lock").exists()


def _legacy_selection(source, rows):
    return {"sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "rows": rows}


def test_setup_import_and_completion_share_the_existing_replacement(
    install_files, monkeypatch
):
    target, source = install_files
    original = b"# unrelated\r\n"
    target.write_bytes(original)
    legacy = b'\xef\xbb\xbfexport\tCLAUDE_SMART_CLI_PATH="C:\\\\Users\\\\owned"\r\n'
    source.write_bytes(legacy)
    replacements = []
    replace = env_config.Path.replace

    def observe(staging, destination):
        assert destination == target
        content = staging.read_bytes()
        assert b"CLAUDE_SMART_CLI_PATH=" in content
        assert INSTALL_COMPLETION.encode() in content
        assert b"REFLEXIO_URL=https://current.example/" in content
        replacements.append(content)
        return replace(staging, destination)

    monkeypatch.setattr(env_config.Path, "replace", observe)
    env_config.publish_install(
        {"REFLEXIO_URL": "https://current.example/"},
        _install_snapshot(target),
        _legacy_selection(source, {"CLAUDE_SMART_CLI_PATH": 0}),
    )
    assert len(replacements) == 1
    assert target.read_bytes().startswith(
        INSTALL_COMPLETION.encode() + b"\r\n" + original
    )
    assert b'CLAUDE_SMART_CLI_PATH="C:\\\\Users\\\\owned"\r\n' in target.read_bytes()
    assert source.read_bytes() == legacy
    assert not (target.parent / "legacy-reflexio-env-checked").exists()


@pytest.mark.parametrize("existed_before", [True, False])
def test_setup_distinguishes_missing_from_existing_empty_settings(
    install_files, existed_before
):
    target, _ = install_files
    if existed_before:
        target.touch()
    captured = _install_snapshot(target)
    if existed_before:
        target.unlink()
    else:
        target.touch()
    with pytest.raises(env_config.SettingsConflictError):
        env_config.publish_install({"REFLEXIO_API_KEY": ""}, captured, None)
    assert target.exists() is not existed_before
    if target.exists():
        assert target.read_bytes() == b""


@pytest.mark.parametrize("prefix", ["", "\ufeff"])
def test_completion_prevents_reimport_after_an_intervening_deletion(
    install_files, prefix
):
    target, source = install_files
    target.write_text(prefix + INSTALL_COMPLETION + "\n", encoding="utf-8")
    source.write_text("CLAUDE_SMART_READ_ONLY=1\n")
    original = target.read_bytes()
    with pytest.raises(env_config.SettingsConflictError):
        env_config.publish_install(
            {},
            _install_snapshot(target),
            _legacy_selection(source, {"CLAUDE_SMART_READ_ONLY": 0}),
        )
    assert target.read_bytes() == original


def test_completion_metadata_is_not_recognized_inside_an_opaque_value(install_files):
    target, source = install_files
    opaque = 'OTHER="first\n' + INSTALL_COMPLETION + '\nlast"\n'
    target.write_text(opaque)
    source.write_text("CLAUDE_SMART_CLI_TIMEOUT=90\n")
    env_config.publish_install(
        {},
        _install_snapshot(target),
        _legacy_selection(source, {"CLAUDE_SMART_CLI_TIMEOUT": 0}),
    )
    assert target.read_text().startswith(INSTALL_COMPLETION + "\n")
    assert opaque in target.read_text()
    assert "CLAUDE_SMART_CLI_TIMEOUT=90\n" in target.read_text()


def test_completion_survives_dashboard_edit_and_native_repair(install_files):
    target, _ = install_files
    env_config.publish_install(
        {"REFLEXIO_URL": "http://localhost:8071/", "REFLEXIO_API_KEY": ""},
        _install_snapshot(target),
        None,
    )
    env_config.update_settings(
        {"REFLEXIO_API_KEY": "owned"},
        {"REFLEXIO_API_KEY": ["REFLEXIO_API_KEY="]},
        target,
    )
    env_config.repair_bootstrap_env(target)
    assert target.read_text().splitlines().count(INSTALL_COMPLETION) == 1


@pytest.mark.parametrize(
    "rhs",
    [
        "'unfinished",
        "'ends\\'",
        '"ends\\"',
        "$" + "{DEPENDENT}",
        "path\x1b[2J",
        "path\x08name",
        "path\x7fname",
        "path\x9bname",
    ],
)
def test_legacy_transfer_refuses_context_dependent_or_noncomposable_values(
    install_files, rhs
):
    target, source = install_files
    target.write_text("# keep\n")
    source.write_text(
        "DEPENDENT=90\nCLAUDE_SMART_CLI_TIMEOUT=" + rhs + "\n", encoding="utf-8"
    )
    with pytest.raises(ValueError):
        env_config.publish_install(
            {},
            _install_snapshot(target),
            _legacy_selection(source, {"CLAUDE_SMART_CLI_TIMEOUT": 1}),
        )
    assert target.read_text() == "# keep\n"


@pytest.mark.parametrize("changed", ["source", "destination", "sidecar", "row-key"])
def test_legacy_transfer_validates_snapshot_and_provenance(install_files, changed):
    target, source = install_files
    target.write_text("OTHER=keep\n")
    source.write_text("CLAUDE_SMART_CLI_TIMEOUT=90\n")
    original = _install_snapshot(target)
    legacy = _legacy_selection(source, {"CLAUDE_SMART_CLI_TIMEOUT": 0})
    error = env_config.SettingsConflictError
    if changed == "source":
        source.write_text("CLAUDE_SMART_CLI_TIMEOUT=120\n")
    elif changed == "destination":
        target.write_text("CLAUDE_SMART_CLI_TIMEOUT=60\n")
    elif changed == "sidecar":
        (target.parent / "legacy-reflexio-env-checked").touch()
    else:
        legacy["rows"] = {"CLAUDE_SMART_CLI_PATH": 0}
        error = ValueError
    before = target.read_bytes()
    with pytest.raises(error):
        env_config.publish_install({}, original, legacy)
    assert target.read_bytes() == before


def test_existing_sidecar_is_read_only_and_does_not_read_legacy_again(install_files):
    target, source = install_files
    source.write_bytes(b"\xff")
    marker = target.parent / "legacy-reflexio-env-checked"
    marker.write_text("existing checked state\n")
    env_config.publish_install(
        {"REFLEXIO_API_KEY": ""}, _install_snapshot(target), None
    )
    assert marker.read_text() == "existing checked state\n"
    assert source.read_bytes() == b"\xff"
    assert INSTALL_COMPLETION in target.read_text()


@pytest.mark.parametrize(
    "case",
    [
        "success",
        "bad-hash",
        "cancel",
        "target-config",
        "prefix-config",
        "no-verify-hash",
        "seed-config",
        "timeout",
    ],
)
def test_real_settings_owner_preparation_and_publication(
    tmp_path, case, locked_owner_wheel
):
    assert_real_settings_owner(tmp_path, case, locked_owner_wheel)


@pytest.mark.skipif(os.name == "nt", reason="POSIX missing-UV installer process")
def test_missing_uv_bootstrap_has_the_publisher_deadline(tmp_path):
    node = shutil.which("node")
    shell = shutil.which("sh")
    sleep = shutil.which("sleep")
    assert node and shell and sleep
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "sh").symlink_to(shell)
    (tools / "sleep").symlink_to(sleep)
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    driver = r"""
const {EventEmitter} = require('node:events');
const {PassThrough} = require('node:stream');
require('node:https').get = (url, callback) => {
  if (url !== 'https://astral.sh/uv/install.sh') throw Error('Unexpected network');
  const request = new EventEmitter();
  request.setTimeout = () => request;
  setImmediate(() => {
    const response = new PassThrough();
    response.statusCode = 200;
    callback(response);
    response.end('echo $$ > "$HOME/bootstrap.pid"\nsleep 30\n');
  });
  return request;
};
const original = setTimeout;
global.setTimeout = (fn, ms, ...args) => original(fn, ms === 120000 ? 1000 : ms, ...args);
require(process.argv[1]).publishInstallSettings({}).then(
  () => { throw Error('Unexpected save'); },
  error => { process.stderr.write(error.message); process.exitCode = 1; }
);
"""
    # The bundle validation precedes preparation; use the real packaged owner.
    environment = {
        "PATH": str(tools),
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
        "TMPDIR": str(temporary),
        "TEMP": str(temporary),
        "TMP": str(temporary),
    }
    child = subprocess.Popen(  # noqa: S603 -- Owned HOME, repository source, no external network.
        [
            node,
            "-e",
            driver,
            str(Path(__file__).resolve().parents[1] / "bin/claude-smart.js"),
        ],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        output, errors = child.communicate(timeout=10)
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.communicate(timeout=5)
    assert child.returncode == 1, output + errors
    assert "preparation timed out" in errors
    assert not (tmp_path / ".claude-smart/.env").exists()
    assert not list(temporary.glob("claude-smart-settings-*"))
    pid = int((tmp_path / "bootstrap.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.parametrize(
    "change", [None, "leaf-replacement", "linked-leaf", "linked-ancestor", "hard-link"]
)
def test_prepared_settings_observe_scoped_loader_identity(
    install_files, tmp_path, change
):
    target, _ = install_files
    target.write_bytes(b"REFLEXIO_URL=https://old.example.invalid/\n")
    before = target.read_bytes()
    # macOS may report a /var alias for the test root.
    scope = tmp_path.resolve() / "project" / ".opencode" / "plugins"
    scope.mkdir(parents=True)
    loader = scope / "claude-smart.js"
    contents = "expected loader\n"
    loader.write_bytes(contents.encode())
    info = loader.stat()
    volume = info.st_dev & 0xFFFFFFFF if os.name == "nt" else info.st_dev
    identity = f"{volume}:{info.st_ino}"
    if change == "leaf-replacement":
        replacement = scope / "new.js"
        replacement.write_bytes(contents.encode())
        replacement.replace(loader)
    outside = tmp_path.resolve() / "unrelated"
    if change == "linked-leaf":
        outside.mkdir()
        loader.rename(outside / loader.name)
        try:
            loader.symlink_to(outside / loader.name)
        except OSError:
            pytest.skip("filesystem does not support this test's symlink")
    elif change == "linked-ancestor":
        project = scope.parents[1]
        project.rename(outside)
        try:
            project.symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("filesystem does not support this test's symlink")
    elif change == "hard-link":
        try:
            os.link(loader, outside)
        except OSError:
            pytest.skip("filesystem does not support this test's hard link")

    def save():
        env_config.publish_install(
            {"REFLEXIO_URL": "http://localhost:8071/"},
            _install_snapshot(target),
            None,
            prepared=True,
            opencode_loader={
                "path": str(loader),
                "contents": contents,
                "identity": identity,
            },
        )

    if change is None:
        save()
        assert b"http://localhost:8071/" in target.read_bytes()
    else:
        with pytest.raises(env_config.SettingsPreflightError) as error:
            save()
        assert error.value.code == 80
        assert target.read_bytes() == before
        assert loader.read_bytes() == contents.encode()


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory entry race")
@pytest.mark.parametrize("phase", ["uninstall", "rollback"])
@pytest.mark.parametrize("replacement", ["file", "directory"])
def test_scoped_loader_removal_preserves_replacement(
    tmp_path, monkeypatch, phase, replacement
):
    from contextlib import ExitStack

    from claude_smart import opencode_scope

    monkeypatch.setenv("HOME", str(tmp_path))
    loader = tmp_path.resolve() / "claude-smart.js"
    original = b"generated bytes"
    displaced = loader.with_name("displaced.js")
    rename = os.rename

    def replace_at_removal(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        if src == loader.name and dst == "entry":
            rename(loader, displaced)
            if replacement == "file":
                loader.write_bytes(b"foreign replacement")
            else:
                loader.mkdir()
                (loader / "sentinel").write_bytes(b"foreign replacement")
        return rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(os, "rename", replace_at_removal)
    with pytest.raises(ValueError, match="replacement was restored"):
        if phase == "uninstall":
            loader.write_bytes(original)
            plan = {
                "path": str(loader),
                **opencode_scope._snapshot(loader),
                "replacement": None,
            }
            opencode_scope._publish([plan], check=False)
        else:
            with ExitStack() as lifetime:
                created = []
                source, _ = opencode_scope.open_scoped_file(
                    loader, lifetime, writable=True, rollback_creation=created
                )
                source.write(original)
                source.flush()
                created[0]()
    restored = loader if replacement == "file" else loader / "sentinel"
    assert restored.read_bytes() == b"foreign replacement"
    assert displaced.read_bytes() == original
    assert not list((tmp_path / ".claude-smart").glob(".scope-removal-*"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX same-volume quarantine")
def test_scoped_creation_refuses_unavailable_quarantine_before_creating_leaf(
    tmp_path, monkeypatch
):
    from contextlib import ExitStack

    from claude_smart import opencode_scope

    def refuse_quarantine(_ancestors, _lifetime):
        raise ValueError("scope and account directories on the same volume")

    monkeypatch.setattr(opencode_scope, "_posix_quarantine_base", refuse_quarantine)
    loader = tmp_path.resolve() / "plugins" / "claude-smart.js"
    with ExitStack() as lifetime, pytest.raises(ValueError, match="same volume"):
        opencode_scope.open_scoped_file(
            loader, lifetime, writable=True, rollback_creation=[]
        )
    assert not loader.exists()
    assert not list(loader.parent.iterdir())


def test_scoped_loader_install_and_remove_on_separate_local_volume(
    tmp_path, monkeypatch
):
    import tempfile
    from pathlib import Path

    from claude_smart import opencode_scope

    tmpfs = Path("/dev/shm")
    if (
        os.name == "nt"
        or not tmpfs.is_dir()
        or not os.access(tmpfs, os.W_OK)
        or tmpfs.stat().st_dev == tmp_path.stat().st_dev
    ):
        pytest.skip("requires a writable tmpfs separate from the account filesystem")
    monkeypatch.setenv("HOME", str(tmp_path))
    with tempfile.TemporaryDirectory(
        prefix="claude-smart-scope-", dir=tmpfs
    ) as project:
        loader = Path(project) / "loader.js"
        edit = {
            "path": str(loader),
            "contents": None,
            "identity": None,
            "replacement": "probe",
        }
        opencode_scope._publish([edit], check=True)
        assert not loader.exists()
        opencode_scope._publish([edit], check=False)
        assert loader.read_text() == "probe"
        opencode_scope._publish(
            [
                {
                    "path": str(loader),
                    **opencode_scope._snapshot(loader),
                    "replacement": None,
                }
            ],
            check=False,
        )
        assert not list(loader.parent.iterdir())
