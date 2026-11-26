# Example projects

All official examples have a versioned `optpilot.experiment.yaml`: document search, toy factory, DEVS gallery, OR report checking, and production/AGV scheduling. Open their folder in Desktop or use `optpilot experiment` commands. Supporting simulator and solver code is reused; old runtime configuration files are retained only as regression/source references.

The toy factory is deterministic and dependency-light. DEVS uses its declared xDEVS dependencies. OR solving invokes the actual external COOPA installation and provider credentials; its score checks report completeness, not mathematical optimality. The AGV grader owns the simulator and measures commands supplied by the candidate program.
