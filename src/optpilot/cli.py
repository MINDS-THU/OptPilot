"""Public CLI for the shared versioned experiment contract."""
import argparse
import json
import sys
from pathlib import Path
from .experiment_cli import register
from .experiment import validate_manifest


def build_parser():
    parser = argparse.ArgumentParser(prog="optpilot")
    from . import __version__
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    register(commands)
    validate = commands.add_parser("validate", help="Validate optpilot.experiment.yaml and authored Studies")
    validate.add_argument("path", type=Path)
    validate.add_argument("--json", action="store_true")
    validate.set_defaults(handler=validate_command)
    schema = commands.add_parser("schema", help="Print the current experiment JSON Schema")
    schema.set_defaults(handler=lambda args: print(Path(__file__).with_name("schemas").joinpath("experiment.schema.json").read_text()))
    return parser


def validate_command(args):
    validate_manifest(args.path)
    print(json.dumps({"valid": True, "path": str(args.path)}) if args.json else "Valid experiment project.")
    return 0


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args) or 0)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
