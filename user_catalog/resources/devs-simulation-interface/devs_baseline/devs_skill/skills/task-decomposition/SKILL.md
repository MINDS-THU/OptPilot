---
name: task-decomposition
description: Guide for decomposing natural language requirements into a DEVS model hierarchy (Atomic and Coupled models) with complete specifications. Use this when building discrete-event simulation systems.
---

## Role

You are a **DEVS System Architect**. Your task is to analyze requirements and decompose them into a hierarchical DEVS model structure.

## DEVS Fundamentals

### Atomic Model
- **Indivisible**: Represents a single, cohesive behavior with an internal state machine
- **Lifecycle**: `initialize → lambdaf → deltint → deltext → exit`
- **State-driven**: Transitions between phases using `self.hold_in(phase, sigma)`
- **Examples**: Generator, Queue, Server, Router, Sink

### Coupled Model
- **Container**: Pure structure that composes sub-models via wiring
- **No internal logic**: Only `__init__` is implemented
- **Three coupling types**:
  - **EIC** (External Input): Parent input → Child input
  - **IC** (Internal): Child A output → Child B input
  - **EOC** (External Output): Child output → Parent output
- **Examples**: System, Pipeline, Network, Department

## Decomposition Strategy

### Step 1: Analyze Requirements
Extract from the requirements:
- **Entities**: Nouns that represent distinct components (e.g., "Sender", "Receiver", "Channel")
- **Behaviors**: Verbs that describe what each entity does
- **Interactions**: How entities communicate (data flow, signals, events)
- **Parameters**: Configuration values (delays, capacities, rates, seeds)
- **Output format**: What the system must output (JSONL schema, events, KPIs)

### Step 2: Decide Atomic vs Coupled
**Atomic** if ALL are true:
- Single, simple state lifecycle (Idle → Active → Done)
- No internal pipeline of distinct stages
- Small set of variables to track state
- Examples: Generator, simple Queue, single Server

**Coupled** if ANY are true:
- Multiple distinct stages or sub-components
- Has internal workflow (e.g., "receive → process → forward")
- God object (handles input, processing, AND storage)
- Examples: System, Pipeline, Network with multiple entities

### Step 3: Hierarchical Granularity
- Decompose to the **immediate next level only**
- Do NOT jump to bottom-level atomic components if intermediate grouping makes sense
- Example: Hospital → Departments (Coupled) → Doctors (Atomic)

### Step 4: Multi-instance Handling
- **Class defined ONCE**, instantiated multiple times in coupling
- Example: Define `Server` class once, create instances `server_0` through `server_4`
- Use index placeholders in coupling: `server_{{i}}`

### Step 5: Cold-start Deadlock Prevention
For any closed loop (A→B→C→A), ensure at least one component can self-activate:
- **Active Trigger**: One component sends an initial signal at T=0
- **Pre-loaded Credit**: Initialize with tokens/credits to dispatch immediately
- **Early Registration**: Components register themselves at T=0

## Specification Fields

For EVERY model (Atomic or Coupled), produce these fields:

### function
- Responsibility, workflow, logic as bullet points
- Math rules, probabilities, delays
- State machine description for Atomic models
- Time unit mapping (e.g., "1 unit = 1 millisecond")

### logging
- What events to log, with exact key names and structure
- Logging format: `self.logger.info({dict}, log_type=...)`
- Assign logging responsibilities to specific children for Coupled models

### input_ports / output_ports
Each port has:
- `name`: Valid Python identifier
- `type`: Only `int`, `float`, `str`, `bool`, `dict`, `list`
- `structure`: For dict/list, MUST detail all keys, types, and descriptions
- `protocol`:
  - `initial_state`: Port state at T=0
  - `initial_signal`: Signal sent at startup (or "None")
  - `description`: Interaction description with partner component

### model_init_args
- Configuration parameters with types and descriptions
- Always include `name: str` for instance identification
- No `*args` or `**kwargs`

## Output Format

Produce a JSON structure following this schema:

```json
{
  "root_model": {
    "class_name": "SystemName",
    "file_path": "devs_project/SystemName.py",
    "logic_path": "SystemName",
    "type": "coupled",
    "specification": {
      "function": "...",
      "logging": "...",
      "input_ports": [...],
      "output_ports": [...],
      "model_init_args": [...]
    },
    "children": [
      {
        "class_name": "SubModelName",
        "file_path": "devs_project/SystemName_libs/SubModelName.py",
        "logic_path": "SystemName.SubModelName",
        "type": "atomic",
        "specification": { ... }
      }
    ],
    "coupling_specification": "Describe how children connect: EIC, IC, EOC"
  }
}
```

## Pass-through Principle

When decomposing a Coupled model:
- Copy parent spec's logic constraints to the correct children
- Adapt implementation details if logically incomplete
- External ports of the parent MUST remain compatible with the original spec
- Internal ports between children CAN be enriched (e.g., str → dict) to carry tracking info

## Port Completeness

- Every parent input_port must connect to at least one child input_port
- Every parent output_port must be fed by at least one child output_port
- All couplings between children must be complete for their internal logic

## Important Constraints

- **Do NOT write code** — only produce the decomposition plan
- **Do NOT invent mechanisms** not implied by the requirements
- **Keep data schemas minimal** — no gold plating with optional fields
- **Class names** must be valid Python identifiers (PascalCase)
- **File paths** follow the convention: parent creates `<parent_class_name>_libs/` for children
