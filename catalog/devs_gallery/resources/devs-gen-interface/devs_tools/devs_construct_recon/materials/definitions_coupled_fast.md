Here is the definition of `Coupled` class. These states the available methods and their purpose.
Use these interface stubs to understand how to interact with the xDEVS framework.

class Port:
    def __init__(self, p_type: type, name: str): ...

class Component:
    parent: Coupled | None
    input: dict[str, Port]
    output: dict[str, Port]
    def add_in_port(self, port: Port): ...
    def add_out_port(self, port: Port): ...

class Atomic(Component, ABC):
    def __init__(self, name: str = None):
        """
        xDEVS implementation of DEVS Atomic Model.
        :param name: name of the atomic model. If no name is provided, it will take the class's name by default.
        """ ...

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