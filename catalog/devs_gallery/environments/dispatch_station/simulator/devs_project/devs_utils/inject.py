from xdevs.models import Atomic, Coupled, Port
from .devs_logger import get_sim_logger

class EventInjector(Atomic):
    """
    Function:
        - Acts as a reliable source generator that parses a schedule of events.
        - Schedules internal interrupts to match the event times strictly.
        - Dynamically routes payloads to specific ports defined in the event list.
    Logging in this model:
        - Event Injection: Logs the details of the injected event (time, port, payload).
        - Completion: Logs when all events have been processed.
    Input Ports:
        - (None)
    Output Ports:
        - (dynamic_name) (any): Dynamic output ports created based on the input event list.
            structure: Depends on the payload type in the event list.
            protocol: initialize: None; process: Pushes payload at scheduled time (Fire-and-Forget).
    """

    def __init__(self, name: str, parent: Coupled | None, events: list[dict]):
        """
        Args:
            name (str): The unique name of the model.
            parent (Coupled | None): The parent model.
            events (list[dict]): A list of event dictionaries to be injected.
                - (dict): A single event definition.
                    time (float): The absolute simulation time to trigger the event.
                    port (str): The name of the target input port.
                    payload (any): The data to be injected.
        """
        super().__init__(name)
        self.parent = parent
        self.logger = get_sim_logger(self)

        # Sort events by time to ensure correct scheduling order
        self.events = sorted(events, key=lambda x: x["time"])
        self.event_idx = 0

        # Dynamically register output ports based on unique port names in the event list
        self.registered_ports = set()
        for evt in self.events:
            p_name = evt["port"]
            if p_name not in self.registered_ports:
                # We assume the type is inferred from the first occurrence or generic
                self.add_out_port(Port(type(evt["payload"]), p_name))
                self.registered_ports.add(p_name)

        self.logger.info({
            "event": "Model Created", 
            "total_events": len(self.events), 
            "unique_ports": list(self.registered_ports)
        })

    def initialize(self):
        """
        Initialize the model. 
        Schedule the first event if the list is not empty.
        """
        self.event_idx = 0
        if self.event_idx < len(self.events):
            first_evt_time = self.events[0]["time"]
            # Sigma is the duration from now (0.0) to the first event time
            self.hold_in("injecting", first_evt_time)
            self.logger.info({"event": "Initialized", "next_event_time": first_evt_time})
        else:
            self.hold_in("passive", float("inf"))

    def deltext(self, e):
        """
        Handle external events. 
        This is a generator model, so it usually ignores external inputs, 
        but must maintain valid state transitions.
        """
        self.hold_in(self.phase, self.sigma - e)

    def deltint(self):
        """
        Handle internal transitions.
        Move to the next event in the list and calculate the time delta (sigma) 
        to the subsequent event.
        """
        self.event_idx += 1
        
        if self.event_idx < len(self.events):
            current_time = self.events[self.event_idx - 1]["time"]
            next_time = self.events[self.event_idx]["time"]
            sigma = next_time - current_time
            
            # Sanity check for non-monotonic time (though sorted in init)
            if sigma < 0:
                sigma = 0
                
            self.hold_in("injecting", sigma)
        else:
            self.hold_in("finished", float("inf"))
            self.logger.info({"event": "Injection Completed", "total_sent": self.event_idx})

    def lambdaf(self):
        """
        Output function.
        Executes immediately before the internal transition.
        Retrieves the current event and pushes the payload to the matching port.
        """
        if self.event_idx < len(self.events):
            evt = self.events[self.event_idx]
            port_name = evt["port"]
            payload = evt["payload"]
            
            if port_name in self.output:
                self.output[port_name].add(payload)
                self.logger.info({
                    "event": "Event Injected", 
                    "port": port_name,
                    "payload": str(payload),
                    "index": self.event_idx
                })
            else:
                self.logger.info({"event": "Injection Error", "reason": f"Port {port_name} not found"})

    def exit(self):
        pass


class ReliableInjectionSystem(Coupled):
    """
    Function:
        - Acts as a Testbench/Harness container.
        - Connects a provided Core Simulation Model with an Event Injector.
        - Automatically establishes couplings based on the ports defined in the event list.
        - Sub-models:
            - injector: name="event_injector". The EventInjector instance.
            - core: name=[User Defined]. The passed-in core model instance.
    Logging in this model:
        - Topology: Logs the dynamic couplings created between Injector and Core.
    Input Ports:
        - (None)
    Output Ports:
        - (dynamic) (any): Can optionally route core model outputs to system outputs (EOC).
            structure: Depends on core model.
            protocol: Standard EOC.
    """

    def __init__(self, name: str, parent: Coupled | None, core_model: Atomic | Coupled, events: list[dict]):
        """
        Args:
            name (str): The unique name of the coupled model.
            parent (Coupled | None): The parent model.
            core_model (Atomic): An instantiated DEVS Atomic model to be tested.
            events (list[dict]): The list of events to inject into the core model.
                - (dict):
                    time (float): Time.
                    port (str): Target port name on the core_model.
                    payload (any): Data.
        """
        super().__init__(name)
        self.parent = parent
        self.logger = get_sim_logger(self)

        # 1. Instantiate and Add Injector
        self.injector = EventInjector("event_injector", self, events)
        self.add_component(self.injector)

        # 2. Add Core Model
        # Assuming core_model is already instantiated, we register it as a component.
        # We ensure the core model considers this system as its parent (or re-parenting logic if needed by engine).
        self.core_model = core_model
        core_model.parent = self
        self.add_component(self.core_model)

        # 3. Define Couplings (IC: Internal Coupling)
        # We iterate through the unique ports required by the events and connect Injector -> Core
        unique_ports = set(evt["port"] for evt in events)
        coupling_count = 0
        
        for p_name in unique_ports:
            # Verify the port exists on the core model to avoid runtime errors
            if p_name in self.core_model.input:
                self.add_coupling(self.injector.output[p_name], self.core_model.input[p_name])
                coupling_count += 1
            else:
                self.logger.info({
                    "event": "Coupling Warning",
                    "message": f"Target port '{p_name}' requested by event list but not found in core model.",
                    "available_ports": list(self.core_model.input.keys())
                })

        self.logger.info({
            "event": "Model Created",
            "core_model": self.core_model.name,
            "couplings_established": coupling_count
        })
        
def get_raw_input_content() -> str:
    """
    安全地从标准输入读取所有文本内容。
    生成的代码通过调用此函数获取数据，而无需直接访问 sys.stdin。
    """
    import sys
    try:
        # 检查是否有关联的 stdin
        if sys.stdin.isatty():
            print("[Warning] No input piped to stdin.")
            return ""
            
        content = sys.stdin.read()
        return content if content else ""
    except Exception as e:
        print(f"[Error] Failed to read stdin: {e}")
        return ""