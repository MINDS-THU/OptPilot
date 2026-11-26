# Source provenance and cleanup record

This package was extracted from the research prototype supplied with the paper
“LLM-Guided Heuristic Design from Simulation Traces: A Case Study in Dynamic
Production and AGV Scheduling” by Jinbo Li and Chuanhao Li. The extraction was
performed in July 2026 from the source snapshot placed in this repository.

## Imported snapshot identity

The supplied research-source directory had no nested VCS metadata, so it has
no upstream commit identifier. Its imported content was frozen by a
deterministic tree digest on 2026-08-04. After excluding only Finder metadata
(`.DS_Store`) and generated Python bytecode (`__pycache__` and `*.pyc`), the
snapshot contained 277 files. For each byte-sorted `./relative/path`, the
verification command emits `SHA256(file)  ./relative/path`; hashing that full
manifest yields:

```text
f28aa9c3281c10bf3ebdc83449c3ade033bc273b80db8e6c6719acb6d10a2aca
```

This digest identifies the local research import used for extraction. It does
not substitute for an upstream release tag or grant redistribution rights.

## Preserved behavior

- The three-line discrete-event factory layout, product routes, resource
  interactions, order generation, quality/rework logic, charging behavior,
  optional faults, KPI definitions, and score calculation.
- The executable `scheduler.py` / `param_estimator.py` policy interface used by
  the initial rule and by LLM-generated policies.
- All 135 combinations of the paper's line-selection, task-ranking, and
  AGV-assignment rule grid, including `DEFAULT/DEFAULT/DEFAULT`.
- The 14-dimensional weighted-rule representation and the GA, DE, and PSO
  search families.
- The manager, trace-query, parallel-editor, evaluation, and elite-preserving
  structure of the process-aware LLM method.
- The supplied compiled Unity WebGL factory visualization. The retained export
  reports Unity `2022.3.23f1`; it is an opaque build artifact rather than the
  editable Unity project.

## Deliberate cleanup

- Removed broker/network orchestration and real-time sleeping from the
  simulator. In-process command delivery now drives the same discrete-event
  model without requiring an MQTT service.
- Added an optional launch-local MQTT-over-WebSocket bridge for visualization
  only. It binds the simulation worker and compiled client to a private
  same-origin endpoint and does not restore public-broker access to evaluation.
- Made stochastic seeds explicit and reused the same replication seeds for
  every candidate in an environment configuration.
- Isolated policy imports between trials so one generated module cannot leak
  into another candidate's evaluation.
- Replaced experiment scripts, global working directories, and CSV side
  effects with OptPilot environment, method, and study contracts.
- Kept scheduling rules in method-produced candidate files. The environment
  knows only the policy entry points and does not select a rule or baseline.
- Retained the lowest-scoring replication as a diagnostic replay rather than
  copying arbitrary run databases into the method workspace.
- Reimplemented the evolutionary orchestration with the Python standard
  library so package validation does not require the prototype's experiment
  framework. The candidate representation and paper hyperparameters remain
  explicit in the method configurations.

## Reproducibility boundaries

- Aggregate stochastic results should be compared under the same environment
  configuration and seed list. They are not expected to reproduce tables in
  the paper bit-for-bit across Python, solver, or platform versions.
- LLM-generated source is inherently dependent on the selected model and
  provider. Prompts, model metadata, candidate lineage, and evaluated files are
  retained as run evidence.
- The rolling-MILP policy variants from the paper are not packaged here: they
  require a licensed Gurobi installation, which cannot be redistributed inside
  a published container image. Ordinary solve failures in the packaged methods
  use the paper's explicitly labelled heuristic fallback, and fallback counts
  remain visible in evaluation metrics
  rather than being relabelled as successful MILP solves.
- The supplied research snapshot did not include a top-level license file.
  Confirm redistribution rights for the extracted research code before
  distributing this package outside the project. The separately vendored
  SimPy runtime retains its own license beside its source.
- The supplied WebGL export does not include the Unity `Assets/`, `Packages/`,
  `ProjectSettings/`, C# sources, or third-party asset license inventory.
  Rebuilding or modifying the compiled client requires the complete Unity
  project; redistribution requires confirmation of the applicable Unity and
  bundled-asset terms.
