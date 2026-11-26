Here is the definition of `Atomic` class. These states the available methods and their purpose. Use these interface stubs to understand how to interact with the xDEVS framework.

class Port:
    def __init__(self, p_type: type, name: str): ...
    def add(self, val): 
        """Add an event to the output port. EXCLUSIVELY used in lambdaf().""" ...
    def empty(self) -> bool:
        return not bool(self._values or self._bag)
    def clear(self):
        self._values.clear()
        self._bag.clear()
    @property
    def values(self) -> Generator[T, None, None]: 
        """Iterate over received events. Used in deltext(e). Example: for packet in self.input['port_name'].values:""" ...
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


class Component:
    parent: Coupled | None
    input: dict[str, Port]
    output: dict[str, Port]
    def add_in_port(self, port: Port): ...
    def add_out_port(self, port: Port): ...
    def hold_in(self, phase: str, sigma: float): 
        """Change the model's phase and set the time remaining until the next internal event.""" ...
    @abstractmethod
    def initialize(self):
        """This method is executed before starting a simulation."""
        pass
    @abstractmethod
    def exit(self):
        """This method is executed after finishing a simulation."""
        pass

    @property
    def used_in_ports(self) -> Generator[Port, None, None]:
        return (port for port in self.in_ports if port)
    @property
    def used_out_ports(self) -> Generator[Port, None, None]:
        return (port for port in self.out_ports if port)

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
        """
        Internal transition. Triggered immediately AFTER lambdaf().
        1. Update state variables reflecting that the previous phase has ended.
        2. Prepare the payload for the NEXT output event.
        3. Call self.hold_in(phase, sigma). DO NOT OUTPUT HERE.
        """
        pass

    @abstractmethod
    def deltext(self, e: float):
        """
        External transition. Triggered when input arrives. 'e' is elapsed time since last transition.
        1. Read self.input["port"].values.
        2. Prepare the payload for the NEXT output event.
        3. Call self.hold_in(phase, sigma). DO NOT OUTPUT HERE.
        """
        pass

    @abstractmethod
    def lambdaf(self):
        """
        Output function. Triggered exactly when the current phase's sigma expires.
        Executes BEFORE deltint(). 
        THIS IS THE ONLY PLACE CAN OUTPUT. Use self.output['port'].add(payload).
        """
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