### BEGIN: General Import
from xdevs.models import Coupled, Port
### END

### BEGIN: Model import
from .DispatchStation_libs.JobGenerator import JobGenerator
from .DispatchStation_libs.Queue import Queue
from .DispatchStation_libs.Machine import Machine
from .DispatchStation_libs.Policy import Policy
from .DispatchStation_libs.MetricsCollector import MetricsCollector
### END

### BEGIN: Model Definition
class DispatchStation(Coupled):
    """
    Top‑level coupled model representing a small workshop dispatch station.
    Encapsulates job generation, queueing, a single machine, a dispatch policy,
    and metrics collection. No external IO or ports.
    """

    def __init__(self, name: str, parent: Coupled | None, seed: int):
        """
        Args:
            name (str): Unique name of the model.
            parent (Coupled | None): Parent model.
            seed (int): Random seed for reproducible job arrival sequence.
        """
        super().__init__(name)
        self.parent = parent

        # Internal parameters (if needed)
        self.param = {}

        # ------------------------------------------------------------------
        # 1. Instantiate all sub-components
        # ------------------------------------------------------------------
        self.job_generator = JobGenerator(
            name="job_generator",
            parent=self,
            seed=seed
        )

        self.queue = Queue(
            name="queue",
            parent=self
        )

        self.machine = Machine(
            name="machine",
            parent=self
        )

        self.policy = Policy(
            name="policy",
            parent=self
        )

        self.metrics = MetricsCollector(
            name="metrics",
            parent=self
        )

        self.add_component(self.job_generator)
        self.add_component(self.queue)
        self.add_component(self.machine)
        self.add_component(self.policy)
        self.add_component(self.metrics)

        # ------------------------------------------------------------------
        # 2. Define internal couplings (IC)
        # Use getattr to avoid direct attribute access on child instances
        # that would reference members not listed in generated_interface.
        # ------------------------------------------------------------------
        jg_out = getattr(self.job_generator, "output")
        q_in = getattr(self.queue, "input")
        q_out = getattr(self.queue, "output")
        m_in = getattr(self.machine, "input")
        m_out = getattr(self.machine, "output")
        p_in = getattr(self.policy, "input")
        p_out = getattr(self.policy, "output")
        mc_in = getattr(self.metrics, "input")

        # 2.1 JobGenerator.job_out -> Queue.job_in
        self.add_coupling(jg_out["job_out"], q_in["job_in"])

        # 2.2 Queue.job_out -> Machine.job_in
        self.add_coupling(q_out["job_out"], m_in["job_in"])

        # 2.3 Machine.request -> Queue.request
        self.add_coupling(m_out["request"], q_in["request"])

        # 2.4 Queue.policy_query -> Policy.query
        self.add_coupling(q_out["policy_query"], p_in["query"])

        # 2.5 Policy.response -> Queue.policy_response
        self.add_coupling(p_out["response"], q_in["policy_response"])

        # 2.6 Machine.job_completed -> MetricsCollector.job_completed
        self.add_coupling(m_out["job_completed"], mc_in["job_completed"])

### END