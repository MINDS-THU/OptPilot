#!/usr/bin/env python3
"""Render one private macOS launchd job for a shared Studio service."""

from __future__ import annotations

import argparse
import os
import plistlib
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--script", required=True)
    parser.add_argument("--working-directory", required=True)
    parser.add_argument("--deploy-config", required=True)
    parser.add_argument("--log", required=True)
    args = parser.parse_args()
    paths = {
        name: Path(value).resolve()
        for name, value in (
            ("script", args.script),
            ("working directory", args.working_directory),
            ("deployment config", args.deploy_config),
            ("log", args.log),
        )
    }
    if not args.label.startswith("io.optpilot.shared-"):
        raise SystemExit("Unexpected launchd label.")
    if not paths["script"].is_file() or not paths["deployment config"].is_file():
        raise SystemExit("Launchd script or deployment config is missing.")
    payload = {
        "Label": args.label,
        "ProgramArguments": ["/bin/bash", str(paths["script"])],
        "WorkingDirectory": str(paths["working directory"]),
        "EnvironmentVariables": {
            "HOME": str(Path.home()),
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "OPTPILOT_DEPLOY_CONFIG": str(paths["deployment config"]),
        },
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Background",
        "ThrottleInterval": 10,
        "Umask": 0o077,
        "StandardOutPath": str(paths["log"]),
        "StandardErrorPath": str(paths["log"]),
    }
    plistlib.dump(payload, sys.stdout.buffer, fmt=plistlib.FMT_XML, sort_keys=True)


if __name__ == "__main__":
    main()
