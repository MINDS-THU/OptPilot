# Framework Comparison: simpy vs xdevs.py

## Overview

| Aspect | simpy | xdevs.py |
|---|---|---|
| Paradigm | Process-oriented (generators) | Event-oriented (state machines) |
| Learning Curve | Low — Python developers familiar | High — DEVS-specific concepts |
| LLM Familiarity | High — widely used, well-documented | Low — niche academic framework |
| Code Correctness | More reliable first-pass generation | Requires precise DEVS semantics |
| Hierarchical Models | Manual composition | Built-in Coupled/Atomic hierarchy |
| Event Scheduling | `yield env.timeout()`, `Resource`, `Store` | `hold_in(phase, sigma)`, ports |
| Coupling | Implicit (shared Environment) | Explicit (EIC/IC/EOC) |
| Verification | Manual testing | Built-in spec checker, simulation checker |

## When to Choose simpy

### Strong Indicators
- Requirements describe **processes**, **queues**, **resources**, **pipelines**
- No explicit DEVS terminology (ports, phases, sigma)
- Simple event scheduling (delays, timeouts, resource contention)
- LLM has shown better simpy code quality in past runs
- Rapid prototyping is the priority

### Typical Use Cases
- Barbershop (reception → checkhair → cuthair pipeline)
- M/M/1 Queue (arrival → service → departure)
- Manufacturing line (stations with processing times)
- Customer service center (agents, queues, priorities)

### Code Pattern
```python
def customer_process(self):
    while True:
        yield self.env.timeout(arrival_interval)
        with self.resource.request() as req:
            yield req
            yield self.env.timeout(service_time)
```

## When to Choose xdevs.py

### Strong Indicators
- Requirements explicitly mention **DEVS**, **Atomic**, **Coupled**
- Complex state machines with multiple phases
- Hierarchical model decomposition needed
- Formal port-based communication required
- Need built-in verification (spec checker, simulation checker)

### Typical Use Cases
- ABP Protocol (sender/receiver with state machines, timeouts, retransmission)
- Network protocols (formal state transitions)
- Complex control systems (multiple interacting state machines)
- Systems requiring formal verification

### Code Pattern
```python
def deltint(self):
    if self.phase == "SENDING":
        if self.timer_expired:
            self.retransmit()
            self.hold_in("WAITING_ACK", self.timeout)
        else:
            self.hold_in("SENDING", self.remaining_time)
```

## Decision Flowchart

```
Requirements
    │
    ├── Contains DEVS terminology (ports, phases, sigma, Atomic, Coupled)?
    │   └── YES → xdevs.py
    │
    ├── Describes state machines with explicit transitions?
    │   └── YES → xdevs.py
    │
    ├── Describes processes/queues/resources/pipelines?
    │   └── YES → simpy
    │
    ├── Simple event scheduling (delays, timeouts)?
    │   └── YES → simpy
    │
    └── Unclear → simpy (default, higher success rate)
```

## Migration Notes

If switching between frameworks:
- **simpy → xdevs**: Each `process()` becomes an Atomic model; shared `Environment` becomes a Coupled model; `Resource`/`Store` become ports with protocol
- **xdevs → simpy**: Each Atomic becomes a `process()` generator; Coupled becomes composition; `hold_in` becomes `yield env.timeout`; ports become `Store`
