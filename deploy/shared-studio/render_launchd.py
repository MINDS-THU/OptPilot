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
    parser.add_argument("--calendar-hour", type=int)
    parser.add_argument("--calendar-minute", type=int)
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
    periodic = args.calendar_hour is not None or args.calendar_minute is not None
    if periodic:
        if args.calendar_hour is None or args.calendar_minute is None:
            raise SystemExit("Both calendar hour and minute are required.")
        if not 0 <= args.calendar_hour <= 23 or not 0 <= args.calendar_minute <= 59:
            raise SystemExit("Calendar hour or minute is out of range.")
    payload = {
        "Label": args.label,
        "ProgramArguments": ["/bin/bash", str(paths["script"])],
        "WorkingDirectory": str(paths["working directory"]),
        "EnvironmentVariables": {
            "HOME": str(Path.home()),
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "OPTPILOT_DEPLOY_CONFIG": str(paths["deployment config"]),
        },
        "ProcessType": "Background",
        "SoftResourceLimits": {"NumberOfFiles": 16_384},
        "HardResourceLimits": {"NumberOfFiles": 32_768},
        "ThrottleInterval": 10,
        "Umask": 0o077,
        "StandardOutPath": str(paths["log"]),
        "StandardErrorPath": str(paths["log"]),
    }
    if periodic:
        payload["StartCalendarInterval"] = {
            "Hour": args.calendar_hour,
            "Minute": args.calendar_minute,
        }
    else:
        payload["RunAtLoad"] = True
        payload["KeepAlive"] = {"SuccessfulExit": False}
    plistlib.dump(payload, sys.stdout.buffer, fmt=plistlib.FMT_XML, sort_keys=True)


if __name__ == "__main__":
    main()
