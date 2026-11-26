"""Launch the AGV visualization for an OptPilot versioned program and settings."""

import argparse
import json
import os
from pathlib import Path
from shutil import copyfile

from interface_server import create_server, serve


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True, type=Path)
    context_path = parser.parse_args().context
    context = json.loads(context_path.read_text(encoding="utf-8"))
    candidate = context["candidate"]
    if candidate["format"] != "program":
        raise ValueError("AGV scheduling requires a saved program candidate.")
    root = context_path.parent
    adapter = root / "agv-policy"
    adapter.mkdir(exist_ok=True)
    environment = Path(__file__).resolve().parent
    copyfile(environment / "initial" / "param_estimator.py", adapter / "param_estimator.py")
    filename, endpoint = candidate["entrypoint"].rsplit(":", 1)
    source = Path(candidate["directory"]) / filename
    (adapter / "scheduler.py").write_text(
        "from importlib.util import module_from_spec, spec_from_file_location\n"
        "import sys\n"
        f"sys.path.insert(0, {str(source.parent)!r})\n"
        f"spec = spec_from_file_location('_optpilot_visual_candidate', {str(source)!r})\n"
        "policy = module_from_spec(spec)\n"
        "sys.modules[spec.name] = policy\n"
        "spec.loader.exec_module(policy)\n"
        f"decide = getattr(policy, {endpoint!r})\n"
        "class Scheduler:\n"
        "    def __init__(self): decide('reset')\n"
        "    def run(self, snapshot): return decide('run', snapshot)\n"
        "def create_scheduler(): return Scheduler()\n",
        encoding="utf-8",
    )
    server = create_server(host="127.0.0.1", port=int(os.environ["OPTPILOT_INTERFACE_PORT"]),
                           candidate_root=adapter, runtime_root=root / "simulation", visual_context=context)
    serve(server)


if __name__ == "__main__":
    main()
