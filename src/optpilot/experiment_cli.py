"""CLI entry points for the same immutable studies used by OptPilot Desktop."""

from pathlib import Path
import json
import os
import uuid


def register(subparsers):
    experiment = subparsers.add_parser("experiment", help="Inspect, review and run a versioned experiment project")
    actions = experiment.add_subparsers(dest="experiment_action", required=True)
    from .resource_commands import add_commands
    add_commands(actions)
    descriptions = {
        "discover": "List current versions in the working project",
        "save": "Save the current artifact version",
        "review": "Resolve a Study selection to immutable versions",
        "run": "Execute a frozen Study revision or review and run a selection",
        "graph": "List saved versions and actual inputs of every run",
        "detail": "Show Trial outcomes, individual Attempts and comparisons",
        "version": "Inspect a frozen definition and the hashes of its saved files",
        "version-file": "Read a saved file from an immutable version",
        "refresh-results": "Re-read a completed managed Harbor job without launching work",
        "cancel": "Cancel a queued or running evaluation",
        "inspect-results": "Preview a Harbor job folder or an external results report",
        "import-results": "Attach external results to reviewed versions",
    }
    for name, description in descriptions.items():
        command = actions.add_parser(name, help=description)
        command.set_defaults(handler=dispatch)
        command.add_argument("--project", default=".", help="Directory containing optpilot.experiment.yaml")
        command.add_argument("--store", help="Retained records directory (default PROJECT/.optpilot/experiments)")
        if name in ("inspect-results", "import-results"):
            command.add_argument("path", type=Path, help="Job folder or exported JSON report")
        if name == "import-results":
            command.add_argument("--review", required=True, help="Frozen Study revision")
            command.add_argument("--bindings", required=True, type=Path, help="JSON mappings for candidates, instances and suiteVersionIds")
            command.add_argument("--digest", help="Expected digest from inspect-results")
        if name == "save":
            command.add_argument("artifact_id")
        if name in ("review", "run"):
            command.add_argument("--study", help="ID of a Study in the project manifest")
            command.add_argument("--selection", help="YAML or JSON file containing the Study selection")
            command.add_argument("--suite", action="append", help="Task Suite ID or immutable version (repeatable)")
            command.add_argument("--instance", action="append", help="Task Instance ID or immutable version (repeatable)")
            command.add_argument("--candidate", action="append", help="Candidate ID or immutable version (repeatable)")
            command.add_argument("--generator", help="Candidate Generator ID or version")
            command.add_argument("--generation-budget", type=int)
            command.add_argument("--repetitions", type=int)
            command.add_argument("--seed", type=int)
            command.add_argument("--retries", type=int)
            command.add_argument("--metric")
            command.add_argument("--direction", choices=["minimize", "maximize"])
            command.add_argument("--title")
        if name == "run":
            command.add_argument("--review", help="ID of a previously reviewed frozen Study")
            command.add_argument("--run-id", help="Explicit run ID to replay or resume a submission")
        if name in ("detail", "cancel", "refresh-results"):
            command.add_argument("run_id")
        if name in ("version", "version-file"):
            command.add_argument("version_id")
        if name == "version-file":
            command.add_argument("path", help="Exact saved file path reported by version")
    return actions


def study_selection(args):
    import yaml

    fields = {"suite": "suiteIds", "instance": "instanceIds", "candidate": "candidateIds",
              "generator": "generatorId", "generation_budget": "generationBudget",
              "repetitions": "repetitions", "seed": "seed", "retries": "retries", "metric": "metric",
              "direction": "direction", "title": "title"}
    options = {field: getattr(args, key) for key, field in fields.items() if getattr(args, key) is not None}
    if sum(map(bool, (args.study, args.selection, options, getattr(args, "review", None)))) != 1:
        raise ValueError("Choose one Study source: --study, --selection, --review, or individual selection options.")
    if args.selection:
        try:
            return yaml.safe_load(Path(args.selection).read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ValueError(f"Invalid Study selection: {exc}") from exc
    return args.study or options


def dispatch(args):
    from . import experiment

    root = Path(args.project)
    desktop_root = os.environ.get("OPTPILOT_EXPERIMENT_PROJECT")
    desktop_store = os.environ.get("OPTPILOT_EXPERIMENT_STORE") if desktop_root and Path(desktop_root).resolve() == root.resolve() else None
    store = Path(args.store or desktop_store or root / ".optpilot" / "experiments")
    action = args.experiment_action
    if action in ("inspect-results", "import-results"):
        from .external_results import import_file, preview
        result = preview(args.path) if action == "inspect-results" else import_file(
            store, args.review, args.path, json.loads(args.bindings.read_text()), args.digest)
    elif action == "refresh-results":
        result = experiment.refresh_results(store, args.run_id)
    elif action == "discover":
        result = experiment.discover(root, store)
    elif action == "save":
        result = experiment.save(root, store, args.artifact_id)
    elif action in ("review", "run"):
        selection = study_selection(args)
        if action == "review":
            result = experiment.review(root, store, selection)
        else:
            review_id = args.review or experiment.review(root, store, selection)["id"]
            result = experiment.execute(store, review_id, args.run_id or f"run-{uuid.uuid4()}")
    elif action == "graph":
        result = experiment.graph(store)
    elif action == "version":
        result = experiment.version(store, args.version_id)
    elif action == "version-file":
        result = experiment.version_file(store, args.version_id, args.path)
    elif action == "cancel":
        result = experiment.cancel(store, args.run_id)
    else:
        result = experiment.detail(store, args.run_id)
    print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
    return int(action == "run" and result["state"] != "succeeded")

