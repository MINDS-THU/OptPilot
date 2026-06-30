"""Studio command registration for the core OptPilot CLI."""

from __future__ import annotations

from .ui.server import add_ui_arguments, run_ui


def add_ui_subcommand(subparsers) -> None:
    parser = subparsers.add_parser("ui", help="Start OptPilot Studio")
    add_ui_arguments(parser)
    parser.set_defaults(handler=_run_ui_command)


def _run_ui_command(args) -> int:
    run_ui(
        host=args.host,
        port=args.port,
        catalog_roots=args.catalog,
        run_roots=args.runs,
        code_server_bin=args.code_server_bin,
        code_server_host=args.code_server_host,
        code_server_port=args.code_server_port,
        code_server_auth=args.code_server_auth,
        code_server_password=args.code_server_password,
        workspace_runtime_executable=args.workspace_runtime_bin,
        workspace_runtime_image=args.workspace_runtime_image,
        workspace_runtime_network=args.workspace_runtime_network,
        workspace_runtime_port_start=args.workspace_runtime_port_start,
        open_browser=args.open_browser,
    )
    return 0
