"""Small isolated-process adapter for deterministic environment replay.

This module intentionally has no dependency on OptPilot.  The parent method
starts it with a scrubbed environment, a bounded request, and a hard timeout.
Only this child imports the environment evaluator and, transitively, generated
candidate code.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping


def _read_json(path: Path, max_bytes: int) -> dict[str, Any]:
    with path.open("rb") as handle:
        encoded = handle.read(max_bytes + 1)
    if len(encoded) > max_bytes:
        raise ValueError("Replay request exceeds its byte limit.")
    value = json.loads(encoded.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Replay request must contain a JSON object.")
    return value


def _encode_json(value: Any, max_bytes: int) -> bytes:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > max_bytes:
        raise ValueError("Replay result exceeds its byte limit.")
    return encoded


def _write_atomic(path: Path, payload: bytes) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary_path = Path(handle.name)
            os.chmod(handle.name, 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _bounded_message(error: BaseException, max_characters: int = 1000) -> str:
    return " ".join(str(error).split())[:max_characters]


def _run(
    request: Mapping[str, Any],
    environment_root: Path,
    environment_callable: str,
) -> Mapping[str, Any]:
    candidate_dir = Path(str(request["candidate_dir"])).resolve(strict=True)
    database_path = Path(str(request["database_path"])).resolve(strict=False)
    settings = request["settings"]
    seed = request["seed"]
    if not candidate_dir.is_dir():
        raise ValueError("candidate_dir must be a directory.")
    if not isinstance(settings, dict):
        raise ValueError("settings must be an object.")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer.")

    import sys

    sys.path.insert(0, str(environment_root))
    # Candidate code should not learn parent adapter paths from argv.
    sys.argv[:] = [str(Path(__file__).name)]
    module_name, _, function_name = environment_callable.partition(":")
    if not module_name or not function_name:
        raise ValueError("environment callable must be module:function.")
    module = importlib.import_module(module_name)
    replay = getattr(module, function_name, None)
    if not callable(replay):
        raise RuntimeError(
            f"Environment does not expose {environment_callable}()."
        )
    result = replay(
        candidate_dir=candidate_dir,
        settings=dict(settings),
        seed=seed,
        database_path=database_path,
    )
    if not isinstance(result, Mapping):
        raise TypeError("The replay callable must return a mapping.")
    return dict(result)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--environment-root", required=True)
    parser.add_argument("--environment-callable", required=True)
    parser.add_argument("--request", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--max-result-bytes", type=int, required=True)
    arguments = parser.parse_args()

    result_path = Path(arguments.result)
    max_bytes = int(arguments.max_result_bytes)
    try:
        environment_root = Path(arguments.environment_root).resolve(strict=True)
        environment_callable = str(arguments.environment_callable)
        module_name = environment_callable.partition(":")[0]
        module_path = environment_root / (
            module_name.replace(".", "/") + ".py"
        )
        if module_path.is_symlink() or not module_path.is_file():
            raise ValueError(
                "environment root does not contain the declared capability "
                f"module {module_name!r}."
            )
        request = _read_json(Path(arguments.request), max_bytes)
        payload = {
            "ok": True,
            "result": _run(request, environment_root, environment_callable),
        }
        encoded = _encode_json(payload, max_bytes)
        return_code = 0
    except BaseException as error:  # emit one bounded machine-readable failure
        payload = {
            "ok": False,
            "error_type": type(error).__name__,
            "message": _bounded_message(error),
        }
        encoded = _encode_json(payload, max_bytes)
        return_code = 1
    _write_atomic(result_path, encoded)
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
