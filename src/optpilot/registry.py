"""Component resolution for builtins and Python-based plugins."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any, Dict, Type


BUILTIN_COMPONENTS: Dict[str, Dict[str, str]] = {
    "controller": {
        "builtin.single_engine_controller": "optpilot.controllers:SingleEngineController",
    },
    "engine": {
        "builtin.reference_random_search": "optpilot.engines:ReferenceRandomSearchEngine",
    },
    "adapter": {
        "builtin.configured_environment": "optpilot.adapters:ConfiguredEnvironmentTargetAdapter",
        "builtin.python_callable": "optpilot.adapters:PythonCallableTargetAdapter",
        "builtin.cli_target": "optpilot.adapters:CLITargetAdapter",
    },
    "backend": {
        "builtin.local_backend": "optpilot.execution:LocalExecutionBackend",
        "builtin.local_subprocess_backend": "optpilot.execution:LocalSubprocessExecutionBackend",
    },
    "scheduler": {
        "builtin.local_scheduler": "optpilot.scheduler:LocalTrialScheduler",
    },
    "materializer": {
        "builtin.parameter_to_config": "optpilot.artifacts:ParameterPassthroughMaterializer",
        "builtin.workspace_bundle": "optpilot.artifacts:WorkspaceBundleMaterializer",
    },
    "validator": {
        "builtin.schema_validation": "optpilot.artifacts:BoundsArtifactValidator",
        "builtin.workspace_policy": "optpilot.artifacts:CodeArtifactManifestValidator",
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
    if implementation.startswith("python:"):
        dotted = implementation[len("python:") :]
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
        f"Unsupported implementation identifier '{implementation}'. Use 'builtin.*' or 'python:module:Class'."
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
