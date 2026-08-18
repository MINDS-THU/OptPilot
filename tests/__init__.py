"""Test package bootstrap.

Isolates the suite from per-user state that lives outside the repository.

Studio looks for packages in the person's own packages folder as well as
beside the project, which is how an installed OptPilot finds the examples it
ships. That folder is machine-global, so without this a developer who has
started Studio once would see different test results from one who never had --
tests would pass or fail depending on what happened to be installed on the
machine. Pointing it at an empty per-process directory keeps the suite
dependent only on the repository.
"""

from __future__ import annotations

import os
import tempfile

_PACKAGES_ROOT_ENV = "OPTPILOT_PACKAGES_ROOT"

if not os.environ.get(_PACKAGES_ROOT_ENV):
    # Deliberately not cleaned up: the suite may fork, and an empty directory
    # in the system temporary location costs nothing.
    os.environ[_PACKAGES_ROOT_ENV] = tempfile.mkdtemp(prefix="optpilot-test-packages-")
