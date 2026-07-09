#!/usr/bin/env python3
"""Run Reflexio CLI with claude-smart metadata visible in process argv."""

from __future__ import annotations

import os
import runpy
import sys


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

    sys.argv = ["reflexio.cli", *reflexio_args]
    runpy.run_module("reflexio.cli", run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
