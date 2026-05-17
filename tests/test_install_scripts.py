"""Focused checks for shell install helpers used by claude-smart setup."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
LIB = REPO_ROOT / "plugin" / "scripts" / "_lib.sh"
SMART_INSTALL = REPO_ROOT / "plugin" / "scripts" / "smart-install.sh"
CODEX_COMPAT = REPO_ROOT / "plugin" / "scripts" / "codex-claude-compat.py"
CODEX_HOOK = REPO_ROOT / "plugin" / "scripts" / "codex-hook.js"


def _minimal_path(tmp_path: Path, *names: str) -> str:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in names:
        target = shutil.which(name)
        if target:
            (bin_dir / name).symlink_to(target)
    return str(bin_dir)


def _isolated_env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["SHELL"] = "/bin/sh"
    env.pop("BASH_ENV", None)
    env.pop("ENV", None)
    return env


def _node_platform() -> str:
    uname_s = subprocess.check_output(["uname", "-s"], text=True).strip()
    uname_m = subprocess.check_output(["uname", "-m"], text=True).strip()
    if uname_s.startswith("Darwin"):
        node_os = "darwin"
    elif uname_s.startswith("Linux"):
        node_os = "linux"
    else:
        pytest.skip(f"unsupported private Node test OS: {uname_s}")
    if uname_m in {"x86_64", "amd64"}:
        node_arch = "x64"
    elif uname_m in {"arm64", "aarch64"}:
        node_arch = "arm64"
    else:
        pytest.skip(f"unsupported private Node test arch: {uname_m}")
    return f"{node_os}-{node_arch}"


def _fake_node_dist(tmp_path: Path, *, bad_checksum: bool = False) -> Path:
    platform = _node_platform()
    dist = tmp_path / "nodejs-dist"
    source_root = tmp_path / f"node-v22.99.0-{platform}"
    bin_dir = source_root / "bin"
    bin_dir.mkdir(parents=True)
    for name, output in {"node": "v22.99.0", "npm": "10.9.0"}.items():
        executable = bin_dir / name
        executable.write_text(f"#!/bin/sh\nprintf '%s\\n' '{output}'\n")
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)

    dist.mkdir()
    archive_name = f"{source_root.name}.tar.gz"
    archive_path = dist / archive_name
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(source_root, arcname=source_root.name)

    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    if bad_checksum:
        digest = "0" * 64
    (dist / "SHASUMS256.txt").write_text(f"{digest}  {archive_name}\n")
    return dist


def _run_private_node_install(tmp_path: Path, base_url: str) -> subprocess.CompletedProcess[str]:
    env = _isolated_env(tmp_path)
    env["CLAUDE_SMART_INSTALL_PRIVATE_NODE_ONLY"] = "1"
    env["CLAUDE_SMART_NODE_BASE_URL"] = base_url
    env["PATH"] = _minimal_path(
        tmp_path,
        "dirname",
        "mkdir",
        "rm",
        "uname",
        "mktemp",
        "awk",
        "sed",
        "tar",
        "ln",
        "mv",
        "cp",
        "sha256sum",
        "shasum",
        "openssl",
    )
    return subprocess.run(
        ["/bin/bash", str(SMART_INSTALL)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_dashboard_unavailable_marker_contains_recovery(tmp_path: Path) -> None:
    env = _isolated_env(tmp_path)
    script = (
        f'. "{LIB}"; '
        'claude_smart_write_dashboard_unavailable "simulated download failure"; '
        'cat "$HOME/.claude-smart/dashboard-unavailable"'
    )
    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", script],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert "simulated download failure" in result.stdout
    assert "The learning backend and hooks can still work" in result.stdout
    assert "/claude-smart:restart" in result.stdout
    assert str(tmp_path / ".claude-smart" / "node" / "current") in result.stdout


def test_resolve_npm_accepts_windows_cmd_shim(tmp_path: Path) -> None:
    npm_cmd = tmp_path / "npm.cmd"
    npm_cmd.write_text("#!/bin/sh\nprintf '10.0.0\\n'\n")
    npm_cmd.chmod(npm_cmd.stat().st_mode | stat.S_IXUSR)
    env = _isolated_env(tmp_path)
    env["PATH"] = str(tmp_path)
    result = subprocess.run(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-c",
            f'. "{LIB}"; claude_smart_resolve_npm; claude_smart_npm_available',
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[0] == str(npm_cmd)


def test_dashboard_build_writes_marker_when_npm_missing(tmp_path: Path) -> None:
    env = _isolated_env(tmp_path)
    env["PATH"] = _minimal_path(tmp_path, "dirname", "mkdir", "cat", "uname", "rm")
    result = subprocess.run(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            str(REPO_ROOT / "plugin" / "scripts" / "dashboard-build.sh"),
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    marker = tmp_path / ".claude-smart" / "dashboard-unavailable"
    assert result.returncode == 1
    assert marker.is_file()
    assert "npm is not on PATH" in marker.read_text()


def test_codex_claude_compat_translates_claude_contract(tmp_path: Path) -> None:
    output_file = tmp_path / "captured-output-path"
    codex = tmp_path / "codex"
    codex.write_text(
        "#!/bin/sh\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        "  if [ \"$1\" = \"--output-last-message\" ]; then\n"
        "    shift\n"
        "    out=\"$1\"\n"
        "  fi\n"
        "  shift || exit 1\n"
        "done\n"
        "cat > \"$out.prompt\"\n"
        "printf 'codex reply' > \"$out\"\n"
    )
    codex.chmod(codex.stat().st_mode | stat.S_IXUSR)
    env = _isolated_env(tmp_path)
    env["PATH"] = _minimal_path(tmp_path, "cat", "python3")
    env["CLAUDE_SMART_CODEX_PATH"] = str(codex)
    env["TMPDIR"] = str(tmp_path)

    result = subprocess.run(
        [
            str(CODEX_COMPAT),
            "-p",
            "--output-format",
            "json",
            "--model",
            "claude-sonnet-4-6",
            "--append-system-prompt",
            "system rules",
        ],
        input="answer this",
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"result": "codex reply"}
    prompt_files = list(tmp_path.glob("claude-smart-codex-*.prompt"))
    assert len(prompt_files) == 1
    assert prompt_files[0].read_text() == "system rules\n\n## Task\nanswer this"
    assert not output_file.exists()


def test_codex_claude_compat_accepts_stream_json_flags(tmp_path: Path) -> None:
    codex = tmp_path / "codex"
    codex.write_text(
        "#!/bin/sh\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        "  if [ \"$1\" = \"--output-last-message\" ]; then\n"
        "    shift\n"
        "    out=\"$1\"\n"
        "  fi\n"
        "  shift || exit 1\n"
        "done\n"
        "printf 'stream reply' > \"$out\"\n"
    )
    codex.chmod(codex.stat().st_mode | stat.S_IXUSR)
    env = _isolated_env(tmp_path)
    env["PATH"] = _minimal_path(tmp_path, "python3")
    env["CLAUDE_SMART_CODEX_PATH"] = str(codex)
    env["TMPDIR"] = str(tmp_path)

    result = subprocess.run(
        [
            str(CODEX_COMPAT),
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--model",
            "claude-sonnet-4-6",
        ],
        input="answer this",
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "type": "result",
        "subtype": "success",
        "result": "stream reply",
    }


def test_codex_hook_ensure_root_tracks_active_plugin_root(tmp_path: Path) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for codex hook wrapper tests")

    local_root = tmp_path / "repo" / "plugin"
    cache_root = (
        tmp_path
        / ".codex"
        / "plugins"
        / "cache"
        / "reflexioai"
        / "claude-smart"
        / "0.2.26"
    )
    local_root.mkdir(parents=True)
    cache_root.mkdir(parents=True)
    (local_root / "pyproject.toml").write_text("[project]\nname='local'\n")
    (cache_root / "pyproject.toml").write_text("[project]\nname='cache'\n")
    reflexio = tmp_path / ".reflexio"
    reflexio.mkdir()
    (reflexio / "plugin-root").symlink_to(local_root, target_is_directory=True)

    env = _isolated_env(tmp_path)
    env["CLAUDE_PLUGIN_ROOT"] = str(cache_root)
    result = subprocess.run(
        [node, str(CODEX_HOOK), "ensure-root"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (reflexio / "plugin-root").resolve() == cache_root
    assert json.loads(result.stdout) == {"continue": True, "suppressOutput": True}


def test_install_private_node_installs_from_verified_archive(tmp_path: Path) -> None:
    dist = _fake_node_dist(tmp_path)
    result = _run_private_node_install(tmp_path, f"file://{dist}")

    assert result.returncode == 0, result.stderr
    current = tmp_path / ".claude-smart" / "node" / "current"
    assert current.exists()
    assert (current / "bin" / "node").exists()
    assert not (tmp_path / ".claude-smart" / "dashboard-unavailable").exists()
    assert not (tmp_path / ".claude-smart" / "install-failed").exists()


def test_install_private_node_checksum_failure_is_dashboard_only(tmp_path: Path) -> None:
    dist = _fake_node_dist(tmp_path, bad_checksum=True)
    result = _run_private_node_install(tmp_path, f"file://{dist}")

    assert result.returncode == 1
    marker = tmp_path / ".claude-smart" / "dashboard-unavailable"
    assert marker.is_file()
    assert "checksum verification failed" in marker.read_text()
    assert not (tmp_path / ".claude-smart" / "install-failed").exists()


def test_install_private_node_download_failure_is_dashboard_only(tmp_path: Path) -> None:
    missing_dist = tmp_path / "missing-node-dist"
    result = _run_private_node_install(tmp_path, f"file://{missing_dist}")

    assert result.returncode == 1
    marker = tmp_path / ".claude-smart" / "dashboard-unavailable"
    assert marker.is_file()
    assert "could not download Node.js checksums" in marker.read_text()
    assert not (tmp_path / ".claude-smart" / "install-failed").exists()
