"""Component resolution for builtins and Python-based plugins."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any, Dict, Type


BUILTIN_COMPONENTS: Dict[str, Dict[str, str]] = {
    "method": {
        "builtin.reference_random_search": "optpilot.methods:ReferenceRandomSearchMethod",
    },
    "adapter": {
        "builtin.configured_environment": "optpilot.adapters:ConfiguredEnvironmentAdapter",
        "builtin.python_callable": "optpilot.adapters:PythonCallableEnvironmentAdapter",
        "builtin.cli_environment": "optpilot.adapters:CLIEnvironmentAdapter",
    },
    "backend": {
        "builtin.local_backend": "optpilot.execution:LocalExecutionBackend",
        "builtin.local_subprocess_backend": "optpilot.execution:LocalSubprocessExecutionBackend",
        "builtin.container_backend": "optpilot.execution:LocalContainerExecutionBackend",
    },
    "scheduler": {
        "builtin.local_scheduler": "optpilot.scheduler:LocalTrialScheduler",
    },
    "materializer": {
        "builtin.parameter_to_config": "optpilot.candidate_materialization:ParameterPassthroughMaterializer",
        "builtin.workspace_bundle": "optpilot.candidate_materialization:WorkspaceBundleMaterializer",
    },
    "validator": {
        "builtin.schema_validation": "optpilot.candidate_materialization:BoundsCandidateValidator",
        "builtin.workspace_policy": "optpilot.candidate_materialization:FileCandidateManifestValidator",
    },
    "interface": {
        "builtin.sqlite_query": "optpilot.adapters:ReadOnlySQLiteQuery",
    },
}


class RegistryError(RuntimeError):
    pass



def resolve_component(category: str, implementation: str) -> Type[Any]:
    if implementation.startswith("builtin."):
        dotted = BUILTIN_COMPONENTS.get(category, {}).get(implementation)
        if dotted is None:
            raise RegistryError(f"Unknown builtin {category} implementation: {implementation}")
        return _load_dotted(dotted)
    if ":" in implementation and not implementation.startswith("python:"):
        dotted = implementation
        try:
            return _load_dotted(dotted)
        except ModuleNotFoundError as exc:
            module_name, _, _ = dotted.partition(":")
            top_level = module_name.split(".", 1)[0]
            if exc.name != top_level:
                raise
            cwd = str(Path.cwd())
            if cwd not in sys.path:
                sys.path.insert(0, cwd)
            return _load_dotted(dotted)
    raise RegistryError(
        f"Unsupported implementation identifier '{implementation}'. Use 'builtin.*' or 'module:object'."
    )



def _load_dotted(dotted: str) -> Type[Any]:
    module_name, _, attr = dotted.partition(":")
    if not module_name or not attr:
        raise RegistryError(f"Invalid component path: {dotted}")
    module = importlib.import_module(module_name)
    try:
        return getattr(module, attr)
    except AttributeError as exc:
        raise RegistryError(f"Component '{attr}' not found in module '{module_name}'") from exc
