"""Path-stable loading for the packaged factory layout."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict


CONFIG_DIRECTORY = Path(__file__).resolve().parent.parent / "config"
_BUNDLED_LAYOUT_NAMES = {
    "factory_layout_multi.json",
    "factory_layout_multi.yaml",
    "factory_layout_multi.yml",
}


def load_factory_config(config_file_name: str = "factory_layout_multi.yml") -> Dict[str, Any]:
    """Load a layout shipped with this environment.

    The upstream implementation resolved layouts from the process working
    directory.  Evaluations run in disposable trial workspaces, so packaged
    layouts are instead resolved relative to this module.
    """

    requested = Path(config_file_name)
    if not requested.is_absolute() and requested.name in _BUNDLED_LAYOUT_NAMES:
        # The interface runtime intentionally uses a dependency-free Python
        # image.  Keep one JSON representation of the shipped layout so both
        # the evaluator and the visual replay worker can load it without
        # reaching a package index for PyYAML.
        path = CONFIG_DIRECTORY / "factory_layout_multi.json"
    else:
        path = requested if requested.is_absolute() else CONFIG_DIRECTORY / requested
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Factory layout not found: {path}")
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        quantity_weights = payload.get("order_generator", {}).get("quantity_weights")
        if isinstance(quantity_weights, dict):
            # JSON object keys are strings; the simulation expects integer
            # order quantities just as the original YAML loader returned.
            payload["order_generator"]["quantity_weights"] = {
                int(key) if str(key).isdigit() else key: value
                for key, value in quantity_weights.items()
            }
    else:
        try:
            import yaml
        except ImportError as error:
            raise RuntimeError(
                "Loading an external YAML layout requires PyYAML; use the "
                "bundled factory_layout_multi layout in dependency-free runtimes."
            ) from error
        with path.open("r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise TypeError(f"Factory layout must contain an object: {path}")
    return payload
