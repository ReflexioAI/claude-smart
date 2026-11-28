"""Run Reflexio CLI with claude-smart metadata visible in process argv."""

from __future__ import annotations

import os
import runpy
import sqlite3
import sys
from importlib.util import find_spec
from pathlib import Path

from claude_smart.native_selection import (
    LearningSelectionError,
    admit_native_learning_target,
)

# The one file every claude-smart release up to 0.2.x stored memory in.
LEGACY_DB_FILENAME = "reflexio.db"
ORG_ID = "claude-smart"
LEGACY_ORG_ENV = "REFLEXIO_DEFAULT_ORG_ID"
LEGACY_MODEL_ENV = "CLAUDE_SMART_CLI_MODEL"


def normalize_inherited_org_selector() -> bool:
    """Keep the saved instance binding authoritative for a current SDK."""
    if find_spec("reflexio.removed_env") is None:
        return False
    from reflexio.removed_env import REMOVED_ENV_ALIASES

    if LEGACY_ORG_ENV not in REMOVED_ENV_ALIASES:
        return False
    os.environ.pop(LEGACY_ORG_ENV, None)
    return True


def check_openclaw_configuration() -> None:
    """Run the bundled SDK's read-only startup admission before replacement."""
    if find_spec("reflexio.server.llm._openclaw_config") is None:
        return
    from reflexio.cli.env_loader import load_local_launch_env
    from reflexio.server.llm._openclaw_config import openclaw_executable
    from reflexio.server.llm._provider_errors import ProviderRequestGuardError

    try:
        normalize_inherited_org_selector()
        load_local_launch_env(provision=False)
        openclaw_executable()
    except (ProviderRequestGuardError, ValueError) as exc:
        raise LearningSelectionError(str(exc)) from None


def prepare_native_sdk_environment() -> None:
    """Publish admitted intent only in the backend child and its workers."""
    target = admit_native_learning_target()
    if (
        target is not None
        and target.host == "opencode"
        and not os.environ.get("CLAUDE_SMART_CLI_PATH", "").strip()
    ):
        adapter = Path(__file__).parent / (
            "opencode-claude-compat.cmd"
            if os.name == "nt"
            else "opencode-claude-compat"
        )
        os.environ["CLAUDE_SMART_CLI_PATH"] = str(adapter)
    os.environ["REFLEXIO_NATIVE_CLI_MODEL"] = target.reference if target else ""
    # The SDK reloads dotenv in workers; empty masks never modify the saved file.
    os.environ["CLAUDE_SMART_USE_LOCAL_CLI"] = ""
    os.environ[LEGACY_MODEL_ENV] = ""


def _incomplete_installation(path: Path, org_id: str) -> bool:
    """Recognize a first-install reservation before using the legacy config writer."""
    from reflexio.server.services.storage.sqlite_storage._dataset_path import (
        DatasetIdentityError,
    )
    from reflexio.server.services.storage.sqlite_storage._learning_policy_authority import (
        verify_sqlite_creation_reservation,
    )
    from reflexio.server.services.storage.storage_base.learning_transition import (
        LearningTransitionBlockedError,
    )

    try:
        verify_sqlite_creation_reservation(path, org_id)
    except (
        DatasetIdentityError,
        LearningTransitionBlockedError,
        OSError,
        ValueError,
        sqlite3.Error,
    ):
        return False
    return True


def pin_legacy_sqlite_db_path(
    backend_port: int | None = None, backend_host: str | None = None
) -> None:
    """Prepare the Claude Smart dataset and its launch binding.

    Newer Reflexio resolves a null ``storage_config.db_path`` per org and
    refuses to adopt a shared ``reflexio.db`` holding rows another org
    labelled, opening an empty ``reflexio_<org>.db`` instead -- which made
    0.3.0 installs look wiped. An explicit path bypasses that resolver.

    Existing installations retain their pinned file. For an older installation
    without a stored path, prefer its legacy file when present. A new install
    with binding support uses the namespace's own SQLite file.

    It must land on disk: uvicorn runs in a child process that re-reads the
    config. A path someone already set, and any non-SQLite storage, is left
    alone. Supported saved-file upgrades run before strict discovery, and the
    path-only edit preserves unrelated raw fields under the shared file lock.
    Invalid configuration is never replaced. Preparation or identity-pin
    failure stops startup instead of letting the resolver select another file.
    An occupied backend port exits before file preparation.

    Returns:
        None: Side effect only -- may rewrite the org config file.
    """
    if os.environ.get("CLAUDE_SMART_BACKEND") != "1":
        return
    try:
        # Must precede the imports below: sqlite_storage pulls in LiteLLM,
        # whose import-time load_dotenv() would otherwise walk up to an
        # unrelated .env before reflexio.cli installs the same guard.
        from reflexio.cli.env_loader import block_implicit_dotenv_walkup

        block_implicit_dotenv_walkup()
        retired_org_selector = normalize_inherited_org_selector()
        from reflexio.cli.run_services import DEFAULT_OSS_PORTS, get_env_port
        from reflexio.cli.utils import ensure_requested_ports_available
        from reflexio.config_file import FILE_FORMAT_VERSION, LocalConfigFile
        from reflexio.models.config_schema import (
            StorageConfigSQLite,
            validate_stored_config,
        )
        from reflexio.server import LOCAL_STORAGE_PATH
        from reflexio.server.services.configurator.local_file_config_storage import (
            LocalFileConfigStorage,
        )
        from reflexio.server.services.storage.sqlite_storage._dataset_path import (
            derive_db_path,
        )

        prepare_instance = None
        if find_spec("reflexio.local_instance") is not None:
            from reflexio import local_instance

            prepare_instance = getattr(
                local_instance, "prepare_local_sqlite_instance", None
            )
        org_id = ORG_ID
        if not retired_org_selector and (
            prepare_instance is None or os.environ.get(LEGACY_ORG_ENV)
        ):
            # Old bundles require the selector, and older dotenv templates
            # can supply the standalone default, which this launcher replaces
            # for Claude Smart.
            os.environ[LEGACY_ORG_ENV] = org_id

        port = (
            backend_port
            if backend_port is not None
            else get_env_port("BACKEND_PORT", DEFAULT_OSS_PORTS["backend"])
        )
        # Must precede file preparation; a refused duplicate start cannot
        # rewrite the settings of the process already serving on this port.
        host = (
            backend_host
            or os.environ.get("CLAUDE_SMART_BACKEND_HOST", "").strip()
            or "127.0.0.1"
        )
        ensure_requested_ports_available({"backend": port}, host=host)
        storage = LocalFileConfigStorage(org_id=org_id)
        root = Path(LOCAL_STORAGE_PATH).absolute()
        legacy = root / LEGACY_DB_FILENAME
        derived = derive_db_path(root, org_id)
        config_path = Path(storage.config_file)
        if prepare_instance is not None and not config_path.exists():
            legacy_present = legacy.exists() or legacy.is_symlink()
            derived_present = derived.exists() or derived.is_symlink()
            resuming = derived.is_file() and _incomplete_installation(derived, org_id)
            if resuming and legacy_present:
                raise RuntimeError("A legacy file appeared during local installation.")
            if resuming or not (legacy_present or derived_present):
                prepare_instance(org_id, derived)
                return

        target = derived if derived.exists() and not legacy.exists() else legacy
        warn_legacy = target == legacy and derived.exists()
        pinned_legacy = False

        def pin(document, _version):
            nonlocal pinned_legacy
            storage_config = validate_stored_config(document).storage_config
            if (
                not isinstance(storage_config, StorageConfigSQLite)
                or storage_config.db_path is not None
            ):
                return
            pinned_legacy = warn_legacy
            document["storage_config"] = {
                **document.get("storage_config", {}),
                "type": "sqlite",
                "db_path": str(target),
            }

        # New readers couple data admission and the selected path to the same
        # generation publication. Preserve the original startup API for older
        # bundles; generation 11 is the protocol floor requiring data admission.
        prepare = getattr(storage, "prepare_for_startup", None)
        if prepare is None:
            if FILE_FORMAT_VERSION >= 11:
                raise RuntimeError("Required storage admission API is unavailable")
            from reflexio.cli.config_upgrade import prepare_local_config

            prepare_local_config(org_id=org_id)
            storage.load_config()
            LocalConfigFile(storage.config_file).edit_document(
                lambda document: pin(document, FILE_FORMAT_VERSION)
            )
        else:
            initial = (
                storage.get_default_config().model_dump(mode="json")
                if not Path(storage.config_file).exists()
                else None
            )
            prepare(edit=pin, initial_document=initial)
        if prepare_instance is not None:
            saved = validate_stored_config(
                LocalConfigFile(config_path).read_document()
            ).storage_config
            saved_path = None
            if isinstance(saved, StorageConfigSQLite):
                if saved.db_path is None:
                    raise RuntimeError("The prepared SQLite configuration has no file.")
                saved_path = Path(saved.db_path)
            prepare_instance(org_id, saved_path)
        if pinned_legacy:
            print(
                f"[claude-smart] using {legacy}; memories written by 0.3.0 "
                f"remain in {derived} and are not merged",
                file=sys.stderr,
            )
    except Exception:  # noqa: BLE001 — redact saved settings and database details
        raise RuntimeError(
            "Could not prepare the local configuration and pin SQLite db_path; "
            "startup stopped to preserve database identity. Stop old writers, "
            "inspect the saved configuration and file permissions, then retry."
        ) from None


def main() -> int:
    """Mirror claude-smart metadata into env and delegate to Reflexio CLI.

    Args:
        None: Reads process argv. Arguments before ``--`` are
            ``--claude-smart-*`` metadata kept visible in the process command
            line; arguments after ``--`` become ``reflexio.cli`` arguments.

    Returns:
        int: Exit status. Returns ``2`` when required argument separators or
            Reflexio CLI arguments are missing.
    """
    if sys.argv[1:] == ["--check-learning-model"]:
        try:
            prepare_native_sdk_environment()
            check_openclaw_configuration()
        except LearningSelectionError as exc:
            print(exc)
            return 3
        return 0

    try:
        separator = sys.argv.index("--")
    except ValueError:
        return 2

    metadata_args = sys.argv[1:separator]
    reflexio_args = sys.argv[separator + 1 :]
    if not reflexio_args:
        return 2

    for arg in metadata_args:
        if not arg.startswith("--claude-smart-") or "=" not in arg:
            continue
        key, value = arg[2:].split("=", 1)
        env_key = key.replace("-", "_").upper()
        os.environ.setdefault(env_key, value)

    if (
        reflexio_args[:2] == ["services", "start"]
        and os.environ.get("CLAUDE_SMART_BACKEND") == "1"
    ):
        # Use the same parser and precedence as the bundled launcher, including
        # a forwarded --backend-port; unknown Typer options remain its concern.
        from reflexio.cli.run_services import (
            DEFAULT_OSS_PORTS,
            _build_run_services_parser,
            parse_only_flag,
            resolve_ports,
        )

        options, _unknown = _build_run_services_parser().parse_known_args(
            reflexio_args[2:]
        )
        selected = parse_only_flag(options.only, {"backend", "docs"})
        if "backend" in selected:
            prepare_native_sdk_environment()
            pin_legacy_sqlite_db_path(
                resolve_ports(options, DEFAULT_OSS_PORTS)["backend"],
                options.backend_host,
            )
    sys.argv = ["reflexio.cli", *reflexio_args]
    runpy.run_module("reflexio.cli", run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
