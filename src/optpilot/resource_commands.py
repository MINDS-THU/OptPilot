"""Shared CLI/desktop operations for resource setup and experiment receipts."""
import json
from pathlib import Path
import sys


def dispatch_resource(action, root, store, request):
    from . import experiment_resources as ledger
    from . import experiment_resource_plan as planning
    if action == "resource_inventory":
        return {"resources": ledger.inventory()}
    if action == "resource_register":
        return {"resource": ledger.register(request["resource"])}
    if action == "resource_delete":
        return ledger.delete(request["resourceId"])
    if action == "resource_plan":
        return planning.preview_plan(store, request["reviewId"])
    if action == "resource_link":
        plan = planning.preview_plan(store, request["reviewId"])
        index = request.get("claimIndex")
        rows = plan["items"]
        if index is not None:
            if type(index) is not int or not 0 <= index < len(rows):
                raise ValueError("Invalid requirement index. Refresh the plan before linking a resource.")
            rows = [rows[index]]
        matches = [r for r in rows if r["key"] == request["claimKey"] and r["capability"] == request["capability"]]
        if len(matches) != 1:
            raise ValueError("Select one requirement from this frozen review. Use --claim-index for a repeated requirement.")
        return {"resource": ledger.link_capability(matches[0], request["resourceId"])}
    if action == "run_resources":
        return planning.run_resources(store, request["runId"])
    if action == "resource_requirements":
        from .resource_authoring import set_requirements
        return set_requirements(root, store, request["artifactId"], request["expectedVersionId"], request["requirements"])
    if action == "resource_observe":
        usage = planning.run_resources(store, request["runId"])
        if not any(a["owner"] == request["owner"] for a in usage["activities"]):
            raise ValueError("This allocation does not belong to the selected Run.")
        ledger.record_usage(request["owner"], request["key"], request["amount"], request["receipt"],
                            complete=request.get("complete", True))
        return planning.run_resources(store, request["runId"])
    if action.startswith("manual_"):
        from . import experiment_manual as manual
        if action == "manual_tasks":
            return manual.tasks(store, request["runId"])
        if action == "manual_claim":
            return manual.claim(store, request["runId"], request["trialId"], request["operator"])
        if action == "manual_complete":
            return manual.complete(store, request["runId"], request["trialId"], request["metrics"],
                                   request.get("evidence"), request["receipt"])
    if action.startswith("ssh_"):
        from . import experiment_ssh as ssh
        if action == "ssh_targets":
            return ssh.targets()
        if action == "ssh_register":
            return ssh.register(request["target"])
        if action == "ssh_probe":
            return ssh.target_status(request["targetId"])
    raise ValueError(f"Unknown resource operation {action!r}")


def _json_arg(value):
    try:
        return json.loads(sys.stdin.read() if value == "-" else Path(value).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Expected a JSON document: {exc}") from exc


def add_commands(actions):
    specs = {
        "resource-inventory": ("List the shared experimental resource inventory", []),
        "resource-register": ("Register or update a supply from a JSON document", ["resource@"]),
        "resource-delete": ("Remove a supply from the shared inventory", ["resourceId"]),
        "resource-plan": ("Check the runtime bindings and gaps for a frozen Study", ["reviewId"]),
        "resource-link": ("Confirm that an existing supply provides a frozen requirement", ["reviewId", "claimKey", "capability", "resourceId"]),
        "resource-requirements": ("Save requirements on the current Task, Instance, Candidate or Generator", ["artifactId", "expectedVersionId", "requirements@"]),
        "run-resources": ("Read resource activities, allocations and use recorded for a Run", ["runId"]),
        "resource-observe": ("Record measured use against an allocation and an idempotent receipt", ["runId", "owner", "key", "amount#", "receipt"]),
        "manual-tasks": ("Show the human work orders of a manual evaluation", ["runId"]),
        "manual-claim": ("Claim an offered work order", ["runId", "trialId", "operator"]),
        "manual-complete": ("Record a claimed work order's measured metrics and receipt", ["runId", "trialId", "metrics@", "receipt"]),
        "ssh-targets": ("List registered native trial worker hosts", []),
        "ssh-register": ("Register an SSH worker using a configured host alias", ["target@"]),
        "ssh-probe": ("Verify that an SSH target responds using the designated Python", ["targetId"]),
    }
    for name, (help_text, fields) in specs.items():
        command = actions.add_parser(name, help=help_text)
        command.set_defaults(handler=cli_dispatch, resource_operation=name.replace("-", "_"))
        command.add_argument("--project", default=".")
        command.add_argument("--store", help="Experiment ledger directory")
        for field in fields:
            label = field.rstrip("@#")
            if field.endswith("@"):
                command.add_argument(label, type=_json_arg, help="JSON file or - to read standard input")
            elif field.endswith("#"):
                command.add_argument(label, type=float)
            else:
                command.add_argument(label)
        if name == "resource-observe":
            command.add_argument("--partial", action="store_false", dest="complete", help="This measurement is incomplete")
        if name == "resource-link":
            command.add_argument("--claim-index", dest="claimIndex", type=int, help="Zero-based position in the reviewed plan's items array")
        if name == "manual-complete":
            command.add_argument("--evidence", type=_json_arg, help="JSON evidence file or -")


def cli_dispatch(args):
    import os
    root = Path(args.project).resolve()
    active = os.environ.get("OPTPILOT_EXPERIMENT_PROJECT")
    store = args.store or (os.environ.get("OPTPILOT_EXPERIMENT_STORE") if active and Path(active).resolve() == root else None)
    result = dispatch_resource(args.resource_operation, root, Path(store or root / ".optpilot" / "experiments"), vars(args))
    print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
    return int(args.resource_operation == "ssh_probe" and not result["ready"])

