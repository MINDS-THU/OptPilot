Here is the definition of `Coupled` class. These states the available methods and their purpose.
```python
class Atomic(Component, ABC):
    def __init__(self, name: str = None):
        """
        xDEVS implementation of DEVS Atomic Model.
        :param name: name of the atomic model. If no name is provided, it will take the class's name by default.
        """
        super().__init__(name)

        self.phase: str = PHASE_PASSIVE
        self.sigma: float = INFINITY

    def ta(self) -> float:
        """:return: remaining time for the atomic model's internal transition."""
        return self.sigma

    def __str__(self) -> str:
        return f'{self.name}({self.phase}, {self.sigma})'

    @abstractmethod
    def deltint(self):
        """Describes the internal transitions of the atomic model."""
        pass

    @abstractmethod
    def deltext(self, e: float):
        """
        Describes the external transitions of the atomic model.
        :param e: elapsed time between last transition and the external transition.
        """
        pass

    @abstractmethod
    def lambdaf(self):
        """Describes the output function of the atomic model."""
        pass

    def deltcon(self):
        """Confluent transitions of the atomic model. By default, internal transition is triggered first."""
        self.deltint()
        self.deltext(0)

    def hold_in(self, phase: str, sigma: float):
        """
        Change atomic model's phase and next timeout.
        :param phase: atomic model's new phase.
        :param sigma: time remaining to the next timeout.
        """
        self.phase = phase
        self.sigma = sigma

class Coupled(Component, ABC):
    def __init__(self, name: str = None):
        """
        xDEVS implementation of DEVS Coupled Model.
        :param name: name of the coupled model. If no name is provided, it will take the class's name by default.
        """
        super().__init__(name)
        self.components: list[Component] = list()
        self.ic: dict[Port, dict[Port, Coupling]] = dict()
        self.eic: dict[Port, dict[Port, Coupling]] = dict()
        self.eoc: dict[Port, dict[Port, Coupling]] = dict()

    def add_coupling(self, p_from: Port, p_to: Port, host=None):
        """
        Adds coupling between two submodules of the coupled model.
        :param p_from: DEVS transmitter port.
        :param p_to: DEVS receiver port.
        :param host: TODO documentation
        :raises ValueError: if coupling is not well defined.
        """
        if p_from.parent == self and p_to.parent in self.components:
            coupling_set = self.eic
        elif p_from.parent in self.components and p_to.parent == self:
            coupling_set = self.eoc
        elif p_from.parent in self.components and p_to.parent in self.components:
            coupling_set = self.ic
        else:
            raise ValueError("Components that compose the coupling are not submodules of coupled model")

        if p_from not in coupling_set:
            coupling_set[p_from] = dict()
        coupling_set[p_from][p_to] = Coupling(p_from, p_to, host)

    def remove_coupling(self, coupling: Coupling):
        """
        Removes coupling between two submodules of the coupled model.
        :param coupling: Couplings to be removed.
        :raises ValueError: if coupling is not found.
        """
        port_from = coupling.port_from
        port_to = coupling.port_to
        for coupling_set in (self.eic, self.eoc, self.ic):
            if coupling_set.get(port_from, dict()).pop(port_to, None) == coupling:
                if not coupling_set[port_from]:
                    coupling_set.pop(port_from)
                return
        raise ValueError("Coupling was not found in model definition")

    def add_component(self, component: Component):
        """
        Adds component to coupled model.
        :param component: component to be added to the Coupled model.
        """
        component.parent = self
        self.components.append(component)
```

Here is the definition of the `Component` class:
```python
class Component(ABC):
    def __init__(self, name: str = None):
        """
        Abstract Base Class for an xDEVS model.
        :param name: name of the xDEVS model. Defaults to the name of the component's class.
        """
        self.name: str = name if name else self.__class__.__name__
        self.parent: Coupled | None = None # Parent component of this component
        self.input: dict[str, Port] = dict()  # Dictionary containing all the component's input ports by name
        self.output: dict[str, Port] = dict() # Dictionary containing all the component's output ports by name
        # TODO make these lists private
        self.in_ports: list[Port] = list()   # List containing all the component's input ports (serialized for performance)
        self.out_ports: list[Port] = list()  # List containing all the component's output ports (serialized for performance)

    def __str__(self) -> str:
        in_str = " ".join([p.name for p in self.in_ports])
        out_str = " ".join([p.name for p in self.out_ports])
        return f'{self.name}: InPorts[{in_str}] OutPorts[{out_str}]'

    def __repr__(self):
        return self.name

    @abstractmethod
    def initialize(self):
        """This method is executed before starting a simulation."""
        pass

    @abstractmethod
    def exit(self):
        """This method is executed after finishing a simulation."""
        pass

    def in_empty(self) -> bool:
        """:return: True if model has not any message in all its input ports."""
        return not any(self.in_ports)

    def out_empty(self) -> bool:
        """:return: True if model has not any message in all its output ports."""
        return not any(self.out_ports)

    @property
    def used_in_ports(self) -> Generator[Port, None, None]:
        return (port for port in self.in_ports if port)

    @property
    def used_out_ports(self) -> Generator[Port, None, None]:
        return (port for port in self.out_ports if port)

    def add_in_port(self, port: Port):
        """
        Adds an input port to the xDEVS model.
        :param port: port to be added to the model.
        :panics NameError: if port name already exists.
        """
        if port.name in self.input:
            raise NameError("Input port name already exists")
        port.parent = self
        self.input[port.name] = port
        self.in_ports.append(port)

    def add_out_port(self, port: Port):
        """
        Adds an output port to the xDEVS model
        :param port: port to be added to the model.
        :panics NameError: if port name already exists.
        """
        if port.name in self.output:
            raise ValueError("Output port name already exists")
        port.parent = self
        self.output[port.name] = port
        self.out_ports.append(port)
```

Here is the definition of the `Port` class:
```python
class Port(Generic[T]):
    def __init__(self, p_type: type[T] | None = None, name: str = None, serve: bool = False):
        """
        xDEVS implementation of DEVS Port.
        :param p_type: data type of events to be sent/received via the new port instance.
        :param name: name of the new port instance. Defaults to the name of the port's class.
        :param serve: set to True if the port is going to be accessible via RPC server. Defaults to False.
        """
        self.name: str = name if name else self.__class__.__name__  # Name of the port
        self.p_type: type[T] | None = p_type  # Port type. If None, it can contain any type of event.
        self.serve: bool = serve                 # True if port is going to be accessible via RPC server
        self.parent: Component | None = None     # xDEVS Component that owns the port
        self._values: deque[T] = deque()         # Bag containing events directly written to the port
        self._bag: list[Port[T]] = list()        # Bag containing coupled ports containing events

    def __bool__(self) -> bool:
        return not self.empty()

    def __len__(self) -> int:
        return sum((len(port) for port in self._bag), len(self._values))

    def __str__(self) -> str:
        p_type = self.p_type.__name__ if self.p_type is not None else 'None'
        return f'{self.name}<{p_type}>'

    def __repr__(self) -> str:
        return str(self)

    def empty(self) -> bool:
        return not bool(self._values or self._bag)

    def clear(self):
        self._values.clear()
        self._bag.clear()

    @property
    def values(self) -> Generator[T, None, None]:
        """:return: Generator function that can iterate over all the values contained in the port."""
        for val in self._values:
            yield val
        for port in self._bag:
            for val in port.values:
                yield val

    def get(self) -> T:
        """
        :return: first value in the port.
        :raises StopIteration: if port is empty.
        """
        return next(self.values)

    def add(self, val: T):
        """
        Adds a new value to the local value bag of the port.
        :param val: event to be added.
        :raises TypeError: If event is not instance of port type.
        """
        if self.p_type is not None and not isinstance(val, self.p_type):
            raise TypeError(f'Value type is {type(val).__name__} ({self.p_type.__name__} expected)')
        self._values.append(val)

    def extend(self, vals: Iterator[T]):
        """
        Adds a set of new values to the local value bag of the port.
        :param vals: list containing all the values to be added.
        :raises TypeError: If one of the values is not instance of port type.
        """
        for val in vals:
            self.add(val)
```