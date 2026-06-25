---
name: code-writing
description: Guide for writing DEVS simulation model code in Python. Supports both xdevs.py and simpy frameworks. Use this when implementing DEVS models from a specification or plan.
---

## Role

You are a **DEVS Code Engineer**. Your task is to write complete, executable Python code for DEVS simulation models based on specifications.

## Framework Selection

Choose the framework based on the requirements:

### Use xdevs.py when:
- Strict DEVS semantics needed (explicit state machines)
- Complex event scheduling with `hold_in(phase, sigma)`
- Hierarchical coupled models with formal EIC/IC/EOC couplings
- The specification already uses DEVS terminology (ports, phases, sigma)

### Use simpy when:
- Requirements are process/resource-oriented (queues, servers, pipelines)
- LLM is more familiar with simpy syntax
- Rapid prototyping needed
- The specification describes workflows rather than state machines

**Default to simpy if unsure** — it is more widely known and produces correct code more reliably.

## xdevs.py Coding Standards

### Atomic Model Template

```python
from xdevs.models import Atomic, Coupled, Port
from devs_utils.logger import get_sim_logger
from devs_utils.time import get_current_time

class ModelName(Atomic):
    """
    Function:
        - General function description
        - State transitions: how each state transfers and what to output
    Logging in this model:
        - log_type_name: description of when and what
    Input Ports:
        - port_name (type): description
            structure: ...
            protocol: initialize: ... ; process: ...
    Output Ports:
        - port_name (type): description
            structure: ...
            protocol: initialize: ... ; process: ...
    """

    def __init__(self, name: str, parent: Coupled | None, <explicit_config_args>):
        """
        Args:
            name (str): The unique name of the model.
            parent (Coupled | None): The parent model.
            <arg_name> (<type>): <description>
        """
        super().__init__(name)
        self.parent = parent
        self.logger = get_sim_logger(self)
        self.add_in_port(Port(<type>, "<port_name>"))
        self.add_out_port(Port(<type>, "<port_name>"))
        # Initialize state variables
        self.hold_in("<INITIAL_PHASE>", <sigma>)
        self.logger.info({"event": "created"}, log_type="model_lifecycle")

    def initialize(self):
        """Set initial state and schedule first event."""
        self.hold_in("<PHASE>", <sigma>)
        self.logger.info({"event": "initialized"}, log_type="model_lifecycle")

    def lambdaf(self):
        """Generate output only. Do NOT modify state."""
        self.output["<port_name>"].add(<payload>)

    def deltint(self):
        """Handle internal events (timeout)."""
        old_phase = self.phase
        elapsed = self.ta()
        # Handle timeout, update state
        # Prepare payload for NEXT lambdaf
        self.hold_in("<NEXT_PHASE>", <sigma>)

    def deltext(self, e):
        """Handle external events (input received)."""
        old_phase = self.phase
        elapsed = self.ta()
        # Process input: self.input["<port_name>"].values
        # Prepare payload for NEXT lambdaf
        self.hold_in("<NEXT_PHASE>", <sigma>)

    def exit(self):
        """Cleanup and final logging."""
        self.logger.info({"event": "exit"}, log_type="model_lifecycle")
```

### Key Rules for xdevs.py Atomic Models

1. **Event Sequence (CRITICAL)**: `lambdaf` sends output BEFORE `deltint` schedules next event
   - Payload sent in `lambdaf` must be prepared in the PREVIOUS `deltint`, `deltext`, or `initialize`
2. **lambdaf Purity**: Only output, no state modification
3. **hold_in**: Always call at end of `initialize`, `deltint`, `deltext`
4. **Initial signals**: Use `self.hold_in("INIT", 0)` to schedule immediate event, send in `lambdaf`
5. **Confluent events**: `deltint` takes precedence by default; override `deltcon(self)` only if needed

### Coupled Model Template

```python
from xdevs.models import Atomic, Coupled, Port
from devs_utils.logger import get_sim_logger
from .<child_file> import <ChildClass>

class ModelName(Coupled):
    """
    Function:
        - Overall system function
        - Sub-models:
            - ChildClassName: name=instance_name. Brief description.
    Logging in this model:
        - model creation log
    Input Ports:
        - port_name (type): description
    Output Ports:
        - port_name (type): description
    """

    def __init__(self, name: str, parent: Coupled | None, <explicit_config_args>):
        """
        Args:
            name (str): The unique name of the model.
            parent (Coupled | None): The parent model.
            <arg_name> (<type>): <description>
        """
        super().__init__(name)
        self.parent = parent
        self.logger = get_sim_logger(self)
        self.add_in_port(Port(<type>, "<port_name>"))
        self.add_out_port(Port(<type>, "<port_name>"))
        # Instantiate sub-models
        child = <ChildClass>(name="<instance_name>", parent=self, <config>)
        self.add_component(child)
        # Define couplings
        self.add_coupling(self.input["<port>"], child.input["<port>"])  # EIC
        self.add_coupling(child_a.output["<port>"], child_b.input["<port>"])  # IC
        self.add_coupling(child.output["<port>"], self.output["<port>"])  # EOC
        self.logger.info({"event": "created"}, log_type="model_lifecycle")
```

### Key Rules for xdevs.py Coupled Models

1. **Only `__init__`** — no `deltint`, `deltext`, `lambdaf`, `initialize`
2. **Relative imports** for sub-models: `from .filename import ClassName`
3. **add_component** before **add_coupling**
4. **Context Info > Specification**: If they differ, follow Context Info strictly

## simpy Coding Standards

### Process-based Model Template

```python
import simpy
import json
import sys
import argparse

class ModelName:
    """
    Function:
        - General function description
    Logging:
        - What events to log and their format
    """

    def __init__(self, env: simpy.Environment, <config_args>):
        self.env = env
        # Initialize resources, stores, state
        self.logger = ...

    def process(self):
        """Main process loop."""
        while True:
            # Wait for event / resource / timeout
            yield self.env.timeout(<duration>)
            # Process
            # Log event
            self._log(<event_data>)

    def _log(self, data: dict):
        """Log event as JSONL to stdout."""
        data["time"] = self.env.now
        print(json.dumps(data), file=sys.stdout, flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--simulation_time", type=float, default=1000.0)
    args = parser.parse_args()

    env = simpy.Environment()
    model = ModelName(env, <config>)
    env.process(model.process())
    env.run(until=args.simulation_time)


if __name__ == "__main__":
    main()
```

### simpy Key Patterns

- **Resource**: `simpy.Resource(env, capacity=N)` — `yield resource.request()` / `resource.release()`
- **Store**: `simpy.Store(env, capacity=N)` — `yield store.put(item)` / `yield store.get()`
- **Timeout**: `yield env.timeout(duration)`
- **Event**: `yield event1 | event2` (OR), `yield event1 & event2` (AND)
- **Interrupt**: `process.interrupt()` for preemption

### DEVS → simpy Mapping

| DEVS Concept | simpy Equivalent |
|---|---|
| Atomic Model | `class` with `process()` generator |
| Coupled Model | Composition: multiple processes sharing `Environment` |
| Input Port | `simpy.Store` (receive) |
| Output Port | `simpy.Store` (send) |
| hold_in(phase, sigma) | `yield env.timeout(sigma)` |
| lambdaf (output) | `yield store.put(payload)` |
| deltext (input) | `yield store.get()` |
| deltint (internal) | Internal state update after timeout |

## Universal Coding Standards

### Data Types
- **Allowed**: `int`, `float`, `str`, `bool`, `dict`, `list`
- **No custom classes** as port data or init arguments (except framework types)
- **Recursive schema**: For dict/list in docstrings, detail all keys/types down to atomic primitives

### Logging
- Use `self.logger.info({dict}, log_type=...)` for xdevs
- Use `print(json.dumps({...}), file=sys.stdout, flush=True)` for simpy
- Log dict keys MUST match the specification exactly
- Log at key events: initialization, state transitions, output events, exit

### Entry Point
- The project MUST have a `run.py` entry point
- Use `argparse` for CLI arguments
- Output to `sys.stdout` as JSONL (one JSON object per line)
- Debug/info to `sys.stderr`
- Do NOT use real time — use simulation time only

### Imports
- **xdevs whitelist**: `numpy`, `math`, `random`, `time`, `pandas`, `xdevs`, `devs_utils`, relative imports
- **simpy whitelist**: `simpy`, `argparse`, `sys`, `json`, `logging`, `collections`, `random`, `math`
- No `os`, `sys` (except for stdin/stdout), `subprocess`, `threading`

### Type Docstrings
```python
"""
Args:
    name (str): The unique name of the model.
    config (dict): Configuration parameters.
        threshold (float): The threshold value.
        mode (str): Operation mode, one of "fifo", "lifo", "priority".
    items (list): List of items.
        - (dict): Each item.
            id (int): Unique identifier.
            name (str): Item name.
"""
```

## Code Generation Process

1. **Read the specification** carefully — understand all ports, logic, and parameters
2. **Choose framework** based on the nature of the requirements
3. **Write the complete code** — all classes, methods, imports, docstrings
4. **Ensure the entry point** `run.py` exists with proper argparse and simulation loop
5. **Verify port completeness** — all specified ports are registered
6. **Verify logging** — all specified log events are implemented with correct keys
7. **Handle edge cases** — empty input, timeout, initialization signals
