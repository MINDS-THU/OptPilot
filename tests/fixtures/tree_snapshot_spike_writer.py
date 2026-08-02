"""Hold a source descriptor open, then mutate it after a test signal."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path


def _require(arguments, parser: argparse.ArgumentParser, *names: str) -> None:
    missing = [name for name in names if getattr(arguments, name) is None]
    if missing:
        parser.error(f"mode requires: {', '.join('--' + name.replace('_', '-') for name in missing)}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("writer", "crash-before-publish"), default="writer")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--ready", type=Path)
    parser.add_argument("--go", type=Path)
    parser.add_argument("--replacement")
    parser.add_argument("--allowed-root", type=Path)
    parser.add_argument("--selection")
    parser.add_argument("--store", type=Path)
    arguments = parser.parse_args()

    if arguments.mode == "crash-before-publish":
        _require(arguments, parser, "allowed_root", "selection", "store")
        repository_root = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(repository_root))
        from scripts.spikes.tree_snapshot_spike import TreeObjectStoreSpike

        store = TreeObjectStoreSpike(arguments.store)

        def crash(step: str) -> None:
            if step == "after_staging_durable_before_publish":
                os._exit(73)

        store.fault_hook = crash
        store.capture(arguments.allowed_root, arguments.selection)
        return 4

    _require(arguments, parser, "source", "ready", "go", "replacement")
    descriptor = os.open(arguments.source, os.O_RDWR)
    try:
        arguments.ready.write_text("ready\n", encoding="utf-8")
        deadline = time.monotonic() + 10.0
        while not arguments.go.exists():
            if time.monotonic() >= deadline:
                return 3
            time.sleep(0.01)
        payload = arguments.replacement.encode("utf-8")
        os.ftruncate(descriptor, 0)
        os.pwrite(descriptor, payload, 0)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
