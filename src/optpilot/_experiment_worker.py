"""Trusted local Python runner. The JSON pipes carry only explicit calls and return values."""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import traceback

WORKER = str(Path(__file__).resolve())
MAX_RESULT = 8 * 1024 * 1024


def send(stream, payload):
    line = json.dumps(payload, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    if len(line) > MAX_RESULT:
        raise ValueError("Worker response exceeded the size limit.")
    stream.write(line + "\n")
    stream.flush()


def receive(stream):
    line = stream.readline(MAX_RESULT + 2)
    if not line or len(line) > MAX_RESULT + 1:
        raise ValueError("Missing or oversized worker response.")
    return json.loads(line)


def entrypoint(value):
    filename, name = value.rsplit(":", 1)
    path = Path(filename).resolve()
    sys.path[:0] = [str(path.parent), os.getcwd()]
    module_spec = importlib.util.spec_from_file_location("_optpilot_project_entry", path)
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    return getattr(module, name)


@contextlib.contextmanager
def candidate_endpoint(spec):
    if spec["format"] == "parameters":
        yield spec["value"]
        return
    process = subprocess.Popen(
        [sys.executable, "-I", "-B", "-u", WORKER, "candidate", spec["entrypoint"]],
        cwd=spec["directory"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        encoding="utf-8",
    )
    try:
        validators = {}
        if spec.get("input") is not None or spec.get("output") is not None:
            from jsonschema import Draft202012Validator
            from referencing import Registry
            validators = {key: Draft202012Validator(spec[key], registry=Registry())
                          for key in ("input", "output") if key in spec}
        def invoke(*args, **kwargs):
            # The program call boundary is JSON, including when grading runs locally.
            arguments = {"args": list(args), "kwargs": kwargs}
            if "input" in validators:
                validators["input"].validate(arguments)
            send(process.stdin, arguments)
            result = receive(process.stdout)
            if "error" in result:
                raise RuntimeError(result["error"])
            if "output" in validators:
                validators["output"].validate(result["value"])
            return result["value"]
        yield invoke
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdout.close()


def main():
    mode = sys.argv[1]
    output = sys.stdout
    with contextlib.redirect_stdout(sys.stderr):
        if mode == "candidate":
            function = entrypoint(sys.argv[2])
            for line in sys.stdin:
                try:
                    payload = json.loads(line)
                    send(output, {"value": function(*payload["args"], **payload["kwargs"])})
                except Exception:
                    send(output, {"error": traceback.format_exc(limit=8)[-16000:]})
            return
        payload = receive(sys.stdin)
        try:
            function = entrypoint(payload["entrypoint"])
            if mode == "evaluate":
                with candidate_endpoint(payload["candidate"]) as candidate:
                    result = function(candidate, payload["context"])
            else:
                result = function(payload["context"])
            send(output, {"value": result})
        except Exception:
            send(output, {"error": traceback.format_exc(limit=8)[-16000:]})


if __name__ == "__main__":
    main()

