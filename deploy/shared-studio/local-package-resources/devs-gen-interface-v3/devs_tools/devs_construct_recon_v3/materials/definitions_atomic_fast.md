The following signatures are the relevant xDEVS API available to generated
atomic models. They are an interface reference, not code to copy.

```python
class Port:
    def __init__(self, p_type: type | None = None, name: str = None, serve: bool = False): ...
    @property
    def values(self):
        """Iterate over every event in the current DEVS message bag."""
    def empty(self) -> bool: ...
    def get(self):
        """Return the first current event; raise StopIteration if empty."""
    def add(self, val):
        """Add one event; val must be an instance of p_type when p_type is set."""
    def extend(self, vals):
        """Add each event from an iterable, applying the same type check."""


class Component:
    def __init__(self, name: str = None): ...

    parent: Coupled | None
    input: dict[str, Port]
    output: dict[str, Port]

    def add_in_port(self, port: Port): ...
    def add_out_port(self, port: Port): ...


class Atomic(Component):
    phase: str
    sigma: float

    def __init__(self, name: str = None): ...

    def ta(self) -> float:
        """Return the remaining time until the next internal event."""

    def hold_in(self, phase: str, sigma: float):
        """Schedule an internal event after sigma time units."""

    def passivate(self, phase: str = "passive"):
        """Enter a phase with no scheduled internal event (sigma is infinity)."""

    def continuef(self, e: float):
        """Keep the current phase and subtract elapsed time e from sigma."""

    def initialize(self): ...
    def deltext(self, e: float): ...
    def lambdaf(self): ...
    def deltint(self): ...
    def deltcon(self): ...
    def exit(self): ...
```

`p_type` must be one concrete runtime class such as `dict`, `list`, `str`,
`int`, `float`, `bool`, or `object`. Do not pass `typing.Dict`,
`typing.List`, `X | Y`, or `typing.Union[...]`; xDEVS inspects the class at
runtime when a coupling is created.

`values` is a generator over the current message bag, including events arriving
through coupled ports. Read all received events with
`for value in self.input["port_name"].values`. It is not a list: do not index it
or call `.pop()` on it. Use `get()` only when the contract intentionally consumes
just the first current event.
Write DEVS output events with `self.output["port_name"].add(value)` in
`lambdaf()`. At an internal event, xDEVS calls `lambdaf()` before `deltint()`;
state needed by that output must already exist before the event, rather than
being computed for the first time in `deltint()`. Every transition must leave
the model either scheduled with
`hold_in(...)` or waiting with `passivate(...)`; `continuef(e)` is the concise
way to preserve an already-active phase after an external input.
