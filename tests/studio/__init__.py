"""Studio test package.

Isolates these tests from per-user state outside the repository.

Studio looks for packages in the person's own packages folder as well as
beside the project, which is how an installed OptPilot finds the examples it
ships. That folder is machine-global, so without this a developer who has
started Studio once sees different results from one who never has -- tests
passing or failing based on what happens to be installed on the machine.

This lives here rather than only in the parent package because the suite is
run with ``unittest discover -s tests``, which makes ``tests`` the top-level
directory: modules import as ``studio.test_x``, and ``tests/__init__.py`` is
never executed. This file is imported either way.
"""

from __future__ import annotations

import os
import tempfile

if not os.environ.get("OPTPILOT_PACKAGES_ROOT"):
    os.environ["OPTPILOT_PACKAGES_ROOT"] = tempfile.mkdtemp(
        prefix="optpilot-test-packages-"
    )
