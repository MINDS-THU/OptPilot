# OptPilot experiment model

This is the public vocabulary for versioned OptPilot experiments. The implementation review below records how the earlier environment/method runtime informed the design.

The versioned client and CLI now share manifest validation, immutable definitions and file snapshots, reviewed Studies, Runs, per-instance Trials, retries, metrics and evidence. The desktop draws actual Run and Trial inputs from Core’s records. It supports local Python evaluation, local Harbor jobs and importing external results. The native generator emits parameters or frozen executable programs and retains exact allowed feedback for each generation. The project ledger is shared by all public entry points; original Realm internals remain only for retained-runtime regression. [Versioned experiment API](../src/optpilot/experiment.py) · [Desktop workflow](../docs/ui.md) · [External results](../docs/external-results.md)

The three main records are **Task**, **Study**, and **Run**. A Task defines a reusable test. A Study selects the task instances to use, the candidates to assess, and the comparison conditions. Each Run executes one revision of a Study and records what happened. The names below are the preferred terms across the UI, documentation, and public API.

## Define a test

| Term | Definition | Relationship |
| --- | --- | --- |
| **Environment** | A model of a system. It defines the entities, their possible states, how those states change over time or in response to interventions, and how entities affect one another. | The Environment declares the settings it accepts. Many Tasks can use the same Environment and expose different parts of it. |
| **Task** | A decision problem. It declares a Decision Contract for the submission, the information made available, the criteria for grading, and which settings may vary across Instances. | Each Task determines how to evaluate an accepted candidate. It may grade a value directly or run a submitted program and grade its behavior in an Environment. |
| **Task Instance** (short form **Instance**) | One concrete problem obtained by binding a Task version to its parameter values and any required data. | Each Instance belongs to exactly one Task version. Its settings can include Environment settings, initial conditions, or private grading data. A fully specified Task can supply one default Instance. A Dataset can supply data for Instances of several Tasks. |
| **Task Suite** (short form **Suite**) | A named collection of Task Instances. | A Suite can include Instances from one Task or several Tasks. An Instance can belong to several Suites and can also be selected directly. A Run records the exact members it uses. Editing the Suite later leaves that historical selection intact. |

A Task defines the following parts. They can live together in a single Task definition.

- **Observations** specify the information available when preparing a submission or making decisions during execution, including permitted feedback and when it is given. The Grader can also use private information.
- **Decision Contract** specifies what may be submitted for evaluation. For data, it defines the accepted structure and validity constraints. For an executable submission, it also defines how the Task can invoke or interact with it, including the expected inputs and outputs. A **Decision Candidate** (short form **Candidate**) is one concrete submission under this contract, identified by its value or version.
- **Metrics** specify what is measured, its meaning and units where applicable, and when the measurement is taken. The Task defines when the assessment ends. It can take a reported measurement directly or use a **Grader**, scoring code that computes one or more metrics from a submission, trace, or final state. A Task can include that code or reference a shared implementation. Creating a separate Grader is optional.
- **Task Parameters** declare which values can vary, their meaning, and their allowed values or defaults. The Task may fix an Environment setting, use its default, or expose it as a Task Parameter. A Task Parameter may also configure observations, decision limits, or scoring. The Task's observation rules specify which parameter values are public.

For example, a maze Environment defines positions, walls, and movement. One Task accepts a complete route as its Decision Candidate and checks whether it reaches the exit. Another Task accepts a policy that takes a local observation and returns a move. One submitted policy is one Candidate for the entire assessment. Its moves are recorded as steps in the Trial. The map and step limit can be Task Parameters. An Instance fixes their values, and different policies can be evaluated on that same Instance.

The Run records which Instances it uses, including their Task versions, resolved parameter values, concrete data, and the Environment and Grader versions used where applicable. When a Study samples or generates Instances, the Run records them as they are selected. Keeping the same instructions while changing the underlying simulator or grading implementation can change the effective test. A native Task can reference these parts separately. An imported task package can bind them together and be versioned as a unit.

## Design an experiment

| Term | Definition | Relationship |
| --- | --- | --- |
| **Study** | The editable design of an experiment. It names the research question, selection of Instances and Candidates, objective or reporting rule, and execution conditions. | A Study can select Instances directly, select them through Suites, or specify how to obtain them from a Task and its data. It selects a compatible Runner for the chosen Tasks. Each Candidate must satisfy the Decision Contract of every Task on which it is evaluated. A Study can list saved candidates directly, use a Candidate Generator, or compare generated candidates with fixed baselines. |
| **Candidate Generator** | An optional component that produces Decision Candidates conforming to a Task’s Decision Contract. It can supply a batch at once or use permitted evaluation feedback to generate the next batch within the Study budget. | A Candidate Generator selects candidates across Trials. A search algorithm or a coding agent can fill this role. A researcher can also submit a revised candidate in a later Run. |
| **Runner** | The component that executes or delegates evaluations and returns results and evidence. | The Runner evaluates Candidates on Task Instances and invokes or delegates grading. It can execute many Trials in one external job. Candidate selection and execution are independently chosen, where compatible. |

Each Task selects and defines its performance metrics, including their units and any objective or trade-off given to the candidate. A Study can choose which of those measurements to report, how to summarize them across Instances, and how to compare Candidates. A Study also specifies applicable data splits, repetitions, and overall experiment budgets. For example, a limit of 50 moves for solving one maze belongs to the Task and can be an Instance parameter. A decision to evaluate each candidate three times belongs to the Study. A setting of the candidate's own algorithm belongs to the Candidate being evaluated.

A **Compute Target** is where execution takes place, such as the local machine or a cluster. A **Sandbox** isolates an execution. A Runner can use a Compute Target and start sandboxes. OptPilot already uses **Resource** for package assets such as viewers, templates, and reference material. An Environment supplies domain behavior. A file-scoring Task may run in a sandbox even when it has no domain Environment.

## Record an execution

| Term | Definition and cardinality |
| --- | --- |
| **Run** | One execution of exactly one Study revision. A Study can have zero or more Runs. At launch, a Run records the study revision and known inputs. It records newly proposed candidates when they are submitted. |
| **Trial** | One logical assessment in one Run, of one Decision Candidate on one Task Instance for one independent repetition. A Run can have zero or more Trials. A multi-step interaction can be one Trial when it has one scoring unit. |
| **Attempt** | One execution of a Trial. A queued Trial can have zero Attempts. Infrastructure retries add Attempts to the same Trial. Independently repeating the assessment creates a new Trial. A retry can still consume time or compute. |
| **Result** | The adopted outcome of a Trial, with status, metric values when available, and their validity. A pending Trial can have no Result. There is at most one adopted Result per Trial at a given record revision. A valid zero, a missing score, and cancellation before grading have distinct meanings. |
| **Evidence** | The material supporting or explaining what happened, such as grader output, logs, action histories, generated files, or an external record identifier. Evidence can belong to a failed Attempt even when there is no valid score. |

For two candidates evaluated on the same Suite of five instances with three independent repetitions each, the study calls for 30 trials. Retrying one trial adds an attempt. The report identifies its comparison group, scoring conditions, aggregation rule, and valid sample count. Only feedback declared available to the participant or Candidate Generator is exposed during the experiment.

For delegated execution, OptPilot records the external job and trial identities alongside its local ones. The external Runner can own the entire sandbox, execution, grading, and retry lifecycle. Reconnection and result import must resolve the same recorded work. A backend that exposes only an aggregate result is recorded at that granularity.

## Identity and lineage

An **Artifact** is an identifiable item that can be saved and reused. A **Version** identifies a particular recorded state of that item. Tasks, environments, programs, datasets, agent configurations, and studies can all have versions. A Trial can also accept an inline value and preserve it directly. Registration makes an artifact discoverable; recording a submitted value requires no manual registration step.

“Decision Candidate”, “Candidate Generator”, and “output” describe how something is used. A program can propose parameters in one experiment and be the Candidate in another. A file produced by a Trial can become a Candidate in a later Trial. Store the actual versions used and produced, including the source Trial when applicable. For hosted models, record the version information the provider exposes and any limit on pinning it.

## Which pattern to use

| Research activity | Candidate | How it is selected and evaluated |
| --- | --- | --- |
| Compare two existing programs | Each program version | List the programs and evaluate them on the same Task Instances. |
| Improve a design in a conversation | Each submitted revision | A working assistant and researcher inspect evidence, edit the files, and start another Run. |
| Search for parameters | Each parameter set | A Candidate Generator, possibly using BoTorch, supplies candidates to a native Runner under a budget. |
| Benchmark interactive agents | Each agent configuration | List the configurations. A Harbor Runner can execute the same Suite for each configuration. An agent choosing actions within a Trial is acting as the Candidate. |
| Optimize an agent configuration | Each proposed configuration | Combine a Candidate Generator with a compatible Harbor Runner that returns development-task feedback between batches. |
| Compare optimizers | Each optimizer version | The Task accepts a search program, gives it a fixed evaluation budget, and grades performance at the end of the search. Scores obtained along the way can be returned as feedback and retained in the Trial evidence. |
| Verify an output independently | The saved output version | Start a new assessment of the output. To assess the producer again, select the producer as the Candidate instead. |

## The desktop view

Conversations are independent of the artifact map. A conversation names its editable directory. Shared directories reuse files; independent copies separate edits but use the same project ledger. The right-hand map displays real artifact versions and evaluations, with links from the versions actually used.

Selecting an artifact or Run opens its recorded history positions. Save/start/import actions, verified edits, public command receipts and sent evidence attachments establish references. Drafts never count. A history entry jumps to the original message, including across sessions and after restart.

## Implementation

`experiment.py` is the public Core API; `experiment_cli.py` and the desktop bridge call it. `_experiment_catalog.py` freezes source and definitions, `_experiment_study.py` resolves immutable inputs, and `_experiment_execution.py` executes candidates and feedback generators. `_experiment_store.py` retains versions, Runs, Trials, Attempts and generation receipts. `harbor_runner.py` delegates to real Harbor jobs and reconciles their terminal reports. Local Python executes trusted code; Harbor owns its isolation and credential boundary.
