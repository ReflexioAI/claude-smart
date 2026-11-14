"""Real file/process invariants of the Python dotenv publication boundary."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from claude_smart import env_config


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
    original = env_config._upsert_env
    def pause(text, values):
        ready.write_text('ready')
        sys.stdin.readline()
        return original(text, values)
    env_config._upsert_env = pause
env_config.set_env_vars(path, {'FIRST': 'one'})
"""
    environment = dict(os.environ)
    environment.update(HOME=str(path.parent), USERPROFILE=str(path.parent))
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(Path(__file__).resolve().parents[1] / "plugin/src"), *sys.path]
    )
    with subprocess.Popen(
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


def test_upsert_preserves_existing_policy_and_comments(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# keep\nOTHER='opaque\\path'\nexport DUP = 'old'\nDUP=older\n")
    added = env_config.set_env_vars(path, {"DUP": 'new"value', "NEW": "two"})
    assert added == ["NEW"]
    assert (
        path.read_text()
        == '# keep\nOTHER=\'opaque\\path\'\nDUP="new\\"value"\nDUP="new\\"value"\nNEW="two"\n'
    )


def test_defaults_preserve_caps_read_only_and_installer_host_semantics(
    tmp_path, monkeypatch
):
    path = tmp_path / ".env"
    path.write_text(
        '# keep\nCLAUDE_SMART_HOST=claude-code\nCLAUDE_SMART_HOST=opencode\nCLAUDE_SMART_READ_ONLY="1"\nREFLEXIO_ROW_LIMIT_INTERACTIONS=0\nREFLEXIO_API_KEY="obsolete"\n'
    )
    monkeypatch.setenv("REFLEXIO_ROW_LIMIT_INTERACTIONS", "9000")
    env_config.ensure_local_env_defaults(path, host="codex")
    text = path.read_text()
    assert text.count("CLAUDE_SMART_HOST=") == 1
    assert "CLAUDE_SMART_HOST=codex\n" in text
    assert 'CLAUDE_SMART_READ_ONLY="1"\n' in text
    assert "REFLEXIO_ROW_LIMIT_INTERACTIONS=0\n" in text
    assert "REFLEXIO_API_KEY" not in text
    before = path.read_bytes()
    env_config.ensure_local_env_defaults(path, host="codex")
    assert path.read_bytes() == before


@pytest.mark.parametrize("operation", ["upsert", "defaults"])
def test_both_mutators_reject_a_live_transaction_then_read_latest_state(
    tmp_path, operation
):
    path = tmp_path / ".env"
    path.write_text("ORIGINAL=keep\n")

    def save():
        if operation == "upsert":
            env_config.set_env_vars(path, {"SECOND": "two"})
        else:
            env_config.ensure_local_env_defaults(path, host="codex", prune=False)

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
    assert (
        'SECOND="two"\n' if operation == "upsert" else "CLAUDE_SMART_HOST=codex\n"
    ) in text


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
    env_config.set_env_vars(path, {"SECOND": "two"})
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
        env_config.set_env_vars(path, {"SECOND": value})
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
        env_config.set_env_vars(path, {"NEW": "two"})
    assert original.read_text() == "PRIVATE=keep\n"
    assert not list(tmp_path.glob(".env-*"))


def test_invalid_utf8_is_not_treated_as_empty_and_is_redacted(tmp_path):
    path = tmp_path / ".env"
    original = b"SECRET=fake-credential\xff\n"
    path.write_bytes(original)
    with pytest.raises(ValueError, match="valid UTF-8") as error:
        env_config.ensure_local_env_defaults(path)
    assert "credential" not in str(error.value)
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "key,value",
    [
        ("BAD\nKEY", "v"),
        ("GOOD", "fake\nINJECT=1"),
        ("GOOD", "fake\rINJECT=1"),
        ("GOOD", "fake\0"),
    ],
)
def test_invalid_assignments_have_no_side_effects(tmp_path, key, value):
    path = tmp_path / "not-created" / ".env"
    with pytest.raises(ValueError, match="Invalid environment assignment") as error:
        env_config.set_env_vars(path, {key: value})
    assert "fake" not in str(error.value)
    assert not path.parent.exists()


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
        env_config.set_env_vars(path, {"NEW": "two"})
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
        env_config.ensure_local_env_defaults(path)
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
        env_config.set_env_vars(path, {"KEY": "fake-secret"})
    finally:
        os.umask(prior_umask)
    assert modes == [0o600]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "separator", ["\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"]
)
def test_all_reader_line_boundaries_are_rejected_in_assignments(tmp_path, separator):
    path = tmp_path / ".env"
    with pytest.raises(ValueError, match="Invalid environment assignment"):
        env_config.set_env_vars(
            path, {"KEY": f"safe{separator}CLAUDE_SMART_READ_ONLY=0{separator}"}
        )
    assert not path.exists()


def test_existing_file_does_not_require_posix_nonblocking_flag(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("OLD=keep\n")
    from types import SimpleNamespace

    # Simulate only our module's Windows flag surface; the native POSIX lock
    # must keep its real OS API rather than inherit this simulated platform.
    flags = SimpleNamespace(**vars(os))
    flags.__dict__.pop("O_NONBLOCK", None)
    monkeypatch.setattr(env_config, "os", flags)
    env_config.set_env_vars(path, {"NEW": "two"})
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
            env_config.set_env_vars(path, {"NEW": "two"})
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
            env_config.set_env_vars(path, {"NEW": "two"})
            assert 'NEW="two"' in path.read_text()
        else:
            env_config.ensure_local_env_defaults(path, host="codex")
            assert "CLAUDE_SMART_HOST=codex" in path.read_text()
    finally:
        path.chmod(0o600)


def _native_powershell(script):
    import base64
    import json

    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
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
    env_config.set_env_vars(path, {"KEY": "fake-secret"})
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
