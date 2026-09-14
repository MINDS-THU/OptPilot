The following signatures are the relevant xDEVS API available to generated
coupled models. They are an interface reference, not code to copy.

```python
class Port:
    def __init__(self, p_type: type | None = None, name: str = None, serve: bool = False): ...


class Component:
    def __init__(self, name: str = None): ...

    parent: Coupled | None
    input: dict[str, Port]
    output: dict[str, Port]

    def add_in_port(self, port: Port): ...
    def add_out_port(self, port: Port): ...


class Coupled(Component):
    def __init__(self, name: str = None): ...

    def add_component(self, component: Component): ...
    def add_coupling(self, p_from: Port, p_to: Port, host=None): ...
```

Register every child with `add_component(...)` before coupling its ports.
Use `add_coupling(...)` for external-input couplings, internal couplings, and
external-output couplings; xDEVS determines the coupling kind from the two port
owners. A coupled model is a structural container and does not need atomic
transition methods.
