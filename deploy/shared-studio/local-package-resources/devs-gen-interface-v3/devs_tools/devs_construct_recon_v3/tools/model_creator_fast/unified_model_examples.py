"""Direct selection and legacy fallback for complete-file examples."""

from dataclasses import dataclass

from ...base_types import PlanResult
from .unified_model_skill import (
    _file_access_modes,
    _has_active_initial_signal,
    _has_runtime_multiplicity,
)


@dataclass(frozen=True)
class CreatorExample:
    name: str
    filename: str
    model_type: str
    mechanisms: frozenset[str]

    @property
    def category(self) -> str:
        return EXAMPLE_CATALOG_DETAILS[self.name][0]

    @property
    def description(self) -> str:
        return EXAMPLE_CATALOG_DETAILS[self.name][1]


EXAMPLE_CATALOG_DETAILS: dict[str, tuple[str, str]] = {
    "final_periodic_state": (
        "periodic and final output",
        "Advance tightly coupled state at fixed intervals, emit no intermediate records, and print exactly one final JSON state at the simulation horizon.",
    ),
    "logged_exclusive_dispatch_queue": (
        "reactive queues and dispatch",
        "Queue generic requests and resource-ready notices, consume one of each per zero-delay assignment, route an assignment carrying the selected resource to exactly one destination, and record it as JSONL.",
    ),
    "logged_available_worker": (
        "timed work and availability",
        "Announce availability at startup and after each fixed-duration job; the same availability event is sent through a DEVS port and recorded as JSONL.",
    ),
    "reactive_multiport_batch": (
        "reactive queues and dispatch",
        "Accumulate inputs, form every currently possible match, and emit a zero-delay batch across several output ports in one internal event.",
    ),
    "timed_payload_channel": (
        "fixed-duration processing",
        "Retain one complete in-flight payload and forward every field unchanged after a fixed channel delay, preserving identifiers and origin timestamps needed downstream; there is no queue or processing transformation.",
    ),
    "timed_then_zero_delay_reply": (
        "multi-stage timing",
        "After timed processing, emit a downstream request and then turn later downstream feedback into a separate zero-delay reply without confusing the two output phases.",
    ),
    "multi_stage_timed_processor": (
        "multi-stage timing",
        "Advance one accepted request through several consecutive internal delays, emit each stage fact at the actual firing time from lambdaf(), and keep a busy request from resetting the active stage timer.",
    ),
    "autonomous_timed_worker": (
        "timed work and availability",
        "Start autonomously by announcing availability at time zero, process one job for a fixed duration, emit completion, and announce availability again.",
    ),
    "logged_delayed_channel": (
        "delayed delivery with logging",
        "Retain independent deadlines for overlapping payloads, forward each unchanged after a fixed delay, and demonstrate JSONL hooks at both acceptance and delivery.",
    ),
    "logged_buffered_server": (
        "buffered timed processing",
        "Process items sequentially for a fixed delay, retain a waiting item while busy, emit results, and write JSONL records at semantic event times.",
    ),
    "logged_timeout_feedback_protocol": (
        "timeout and retry",
        "Autonomously prepare and send numbered items, wait for matching feedback, retry after timeout, handle late feedback, and write timestamped JSONL events.",
    ),
    "autonomous_periodic_source": (
        "autonomous sources",
        "Emit once at time zero and then periodically forever, stamping each payload with the actual simulation time when it is emitted.",
    ),
    "periodic_state_with_async_parameter_updates": (
        "periodic state evolution",
        "Start an autonomous periodic clock during initialization, advance state and emit it through a DEVS port at each tick, and let optional asynchronous inputs update parameters without moving that clock; there is no external IO.",
    ),
    "logged_periodic_state_with_async_updates": (
        "periodic state evolution",
        "Start an autonomous periodic clock during initialization, accept optional asynchronous parameter updates without moving it, and report each newly computed state through both a DEVS port and one JSONL record.",
    ),
    "periodic_controller_with_async_setpoint": (
        "periodic control",
        "Sample the latest setpoint at each established periodic tick; asynchronous setpoint changes affect future control outputs but do not reset the cadence.",
    ),
    "reactive_zero_delay_router": (
        "immediate port response",
        "Turn each received dictionary request into a DEVS output at the same simulation time using a zero-delay internal event.",
    ),
    "reactive_scalar_transform": (
        "immediate port response",
        "Apply a stateless scalar transformation to every value currently received and emit all transformed values at zero delay.",
    ),
    "fifo_buffered_timed_service": (
        "timed service buffering choices",
        "Retain every arrival in an unbounded FIFO, process one item at a time for a fixed duration, preserve only a timer that was already active before the external transition, and never subtract elapsed time from a timer just started for the new arrival.",
    ),
    "single_inflight_timed_service": (
        "timed service buffering choices",
        "Accept one item only while idle, retain and forward it unchanged after a fixed delay, and deliberately ignore arrivals while busy; there is no waiting buffer.",
    ),
    "timeout_feedback_protocol": (
        "timeout and retry",
        "Autonomously send sequential jobs, wait for matching acknowledgements, retry on timeout, and ignore or handle stale feedback without external logging.",
    ),
    "jsonl_event_sink": (
        "external output sinks",
        "Remain passive until a business event arrives, then write that received event as one compact JSONL record to stdout; it sends no DEVS output.",
    ),
    "jsonl_state_sink": (
        "external output sinks",
        "Remain passive and print each received state snapshot as JSON Lines without changing the state or forwarding it through a DEVS port.",
    ),
    "jsonl_stateful_input_processor": (
        "input-driven state and logging",
        "For every received value, update retained state immediately and write one JSONL observation derived from the new state; there is no independent timer.",
    ),
    "final_aggregate_sink": (
        "external output sinks",
        "Collect structured facts from multiple input ports throughout a run, retain and sort them, and write exactly one combined JSON document from exit() instead of printing JSONL per event.",
    ),
    "stdin_jsonl_timestamped_event_source": (
        "standard-input sources",
        "Use only when every stdin line is a complete JSON object: parse each timestamped JSONL record with json.loads(), schedule it at its own nondecreasing timestamp, and advance only after emission. Do not choose this for whitespace-delimited fields.",
    ),
    "stdin_plaintext_timestamped_event_source": (
        "standard-input sources",
        "Read nonblank whitespace-delimited timestamped text lines such as '<HH:MM:SS[:fraction]> <event> [value ...]' exactly once, split without assuming JSON or a fixed four-token line, and emit each parsed record at its scheduled time.",
    ),
    "stdin_preloaded_schedule_periodic_lookup": (
        "standard-input sources",
        "Read the complete value schedule from stdin during initialization, then at each integer simulation time emit the latest value whose timestamp is not in the future.",
    ),
    "stdin_startup_batch_source": (
        "standard-input sources",
        "Consume all stdin once at initialization and publish every parsed record together at time zero; timestamps inside records remain data rather than schedules.",
    ),
    "file_input_source": (
        "file adapters",
        "Read an immutable JSONL file once during initialization and publish all valid records at time zero through a DEVS output port.",
    ),
    "file_record_sink": (
        "file adapters",
        "Create parent directories as needed and append one compact JSON record to a configured file for every received event.",
    ),
    "coupled_composition": (
        "coupled composition",
        "Build a pure coupled wrapper with boundary ports, fixed child instances, constructor propagation, and ordinary EIC, IC, and EOC wiring.",
    ),
    "coupled_internal_composition": (
        "coupled composition",
        "Build a portless coupled root whose children communicate only through an internal coupling, without inventing boundary ports.",
    ),
    "coupled_single_child_container": (
        "coupled composition",
        "Build a portless coupled wrapper around one child, pass constructor values through, and add no coupling when the plan declares no message route.",
    ),
    "coupled_runtime_family": (
        "runtime-sized composition",
        "Create a constructor-sized family of children and connect every concrete member to a fixed coordinator inside the construction loop; never use a model class or a fabricated placeholder instance as a coupling endpoint.",
    ),
}


def _required_stdin_example(
    model_plan: PlanResult,
    mechanisms: set[str],
    original_requirements: str = "",
) -> CreatorExample | None:
    """Choose one concrete stdin pattern whenever the model owns stdin."""
    specification = model_plan.model_info.specification
    stdin_streams = [
        stream for stream in specification.external_io if stream.target == "stdin"
    ]
    if not stdin_streams:
        return None

    text = " ".join(
        [
            specification.function,
            *(stream.content for stream in stdin_streams),
            original_requirements,
        ]
    ).casefold()
    if "stdin_startup_batch" in mechanisms:
        name = "stdin_startup_batch_source"
    elif "periodic_schedule_lookup" in mechanisms:
        name = "stdin_preloaded_schedule_periodic_lookup"
    elif any(
        marker in text
        for marker in (
            "whitespace",
            "plain text",
            "plaintext",
            "space-separated",
            "colon-separated",
            "hh:mm:ss",
        )
    ):
        name = "stdin_plaintext_timestamped_event_source"
    else:
        name = "stdin_jsonl_timestamped_event_source"
    return next(item for item in EXAMPLE_REGISTRY if item.name == name)


def _ensure_stdin_example(
    model_plan: PlanResult,
    selected: list[CreatorExample],
    mechanisms: set[str],
    *,
    max_examples: int | None = None,
    original_requirements: str = "",
) -> list[CreatorExample]:
    required = _required_stdin_example(
        model_plan, mechanisms, original_requirements=original_requirements
    )
    if required is None:
        return selected
    selected_stdin = [
        item
        for item in selected
        if item.mechanisms & {"stdin_schedule", "stdin_startup_batch"}
    ]
    if any(item.name == required.name for item in selected_stdin):
        return selected
    # The planner may choose the wrong serialization-specific stdin example.
    # Replace that choice from the concrete external_io contract instead of
    # showing two contradictory parsers to code generation.
    selected = [item for item in selected if item not in selected_stdin]
    if max_examples is not None and len(selected) >= max_examples:
        selected = selected[: max(0, max_examples - 1)]
    # stdin is intentionally first: stream consumption is a special ownership
    # mechanism, while the locked contract still controls domain behavior.
    return [required, *selected]


EXAMPLE_REGISTRY = (
    CreatorExample(
        name="final_periodic_state",
        filename="final_periodic_state.py",
        model_type="atomic",
        mechanisms=frozenset(
            {"periodic_internal_event", "external_jsonl", "final_only_output"}
        ),
    ),
    CreatorExample(
        name="logged_exclusive_dispatch_queue",
        filename="logged_exclusive_dispatch_queue.py",
        model_type="atomic",
        mechanisms=frozenset(
            {"zero_delay_response", "buffering", "external_jsonl", "dual_output"}
        ),
    ),
    CreatorExample(
        name="logged_available_worker",
        filename="logged_available_worker.py",
        model_type="atomic",
        mechanisms=frozenset(
            {"autonomous_start", "timed_processing", "external_jsonl", "dual_output"}
        ),
    ),
    CreatorExample(
        name="reactive_multiport_batch",
        filename="reactive_multiport_batch.py",
        model_type="atomic",
        mechanisms=frozenset({"zero_delay_response", "buffering"}),
    ),
    CreatorExample(
        name="fifo_buffered_timed_service",
        filename="fifo_buffered_timed_service.py",
        model_type="atomic",
        mechanisms=frozenset({"timed_processing", "buffering"}),
    ),
    CreatorExample(
        name="single_inflight_timed_service",
        filename="single_inflight_timed_service.py",
        model_type="atomic",
        mechanisms=frozenset({"timed_processing"}),
    ),
    CreatorExample(
        name="timed_payload_channel",
        filename="timed_payload_channel.py",
        model_type="atomic",
        mechanisms=frozenset({"timed_processing"}),
    ),
    CreatorExample(
        name="timed_then_zero_delay_reply",
        filename="timed_then_zero_delay_reply.py",
        model_type="atomic",
        mechanisms=frozenset({"timed_processing", "zero_delay_response"}),
    ),
    CreatorExample(
        name="multi_stage_timed_processor",
        filename="multi_stage_timed_processor.py",
        model_type="atomic",
        mechanisms=frozenset({"timed_processing"}),
    ),
    CreatorExample(
        name="autonomous_timed_worker",
        filename="autonomous_timed_worker.py",
        model_type="atomic",
        mechanisms=frozenset({"autonomous_start", "timed_processing"}),
    ),
    CreatorExample(
        name="logged_delayed_channel",
        filename="logged_delayed_channel.py",
        model_type="atomic",
        mechanisms=frozenset({"timed_processing", "external_jsonl"}),
    ),
    CreatorExample(
        name="logged_buffered_server",
        filename="logged_buffered_server.py",
        model_type="atomic",
        mechanisms=frozenset({"timed_processing", "buffering", "external_jsonl"}),
    ),
    CreatorExample(
        name="logged_timeout_feedback_protocol",
        filename="logged_timeout_feedback_protocol.py",
        model_type="atomic",
        mechanisms=frozenset(
            {
                "autonomous_start",
                "timed_processing",
                "timeout_feedback",
                "external_jsonl",
                "dual_output",
            }
        ),
    ),
    CreatorExample(
        name="autonomous_periodic_source",
        filename="autonomous_periodic_source.py",
        model_type="atomic",
        mechanisms=frozenset({"autonomous_start", "periodic_internal_event"}),
    ),
    CreatorExample(
        name="logged_periodic_state_with_async_updates",
        filename="logged_periodic_state_with_async_updates.py",
        model_type="atomic",
        mechanisms=frozenset(
            {
                "periodic_internal_event",
                "periodic_input_updates",
                "external_jsonl",
                "dual_output",
            }
        ),
    ),
    CreatorExample(
        name="periodic_state_with_async_parameter_updates",
        filename="periodic_state_with_async_parameter_updates.py",
        model_type="atomic",
        mechanisms=frozenset({"periodic_internal_event", "periodic_input_updates"}),
    ),
    CreatorExample(
        name="periodic_controller_with_async_setpoint",
        filename="periodic_controller_with_async_setpoint.py",
        model_type="atomic",
        mechanisms=frozenset({"periodic_internal_event", "periodic_input_updates"}),
    ),
    CreatorExample(
        name="reactive_zero_delay_router",
        filename="reactive_zero_delay_router.py",
        model_type="atomic",
        mechanisms=frozenset({"zero_delay_response"}),
    ),
    CreatorExample(
        name="reactive_scalar_transform",
        filename="reactive_scalar_transform.py",
        model_type="atomic",
        mechanisms=frozenset({"zero_delay_response"}),
    ),
    CreatorExample(
        name="timeout_feedback_protocol",
        filename="timeout_feedback_protocol.py",
        model_type="atomic",
        mechanisms=frozenset(
            {"autonomous_start", "timed_processing", "timeout_feedback"}
        ),
    ),
    CreatorExample(
        name="jsonl_event_sink",
        filename="jsonl_event_sink.py",
        model_type="atomic",
        mechanisms=frozenset({"external_jsonl"}),
    ),
    CreatorExample(
        name="jsonl_state_sink",
        filename="jsonl_state_sink.py",
        model_type="atomic",
        mechanisms=frozenset({"external_jsonl"}),
    ),
    CreatorExample(
        name="jsonl_stateful_input_processor",
        filename="jsonl_stateful_input_processor.py",
        model_type="atomic",
        mechanisms=frozenset(
            {"external_jsonl", "stateful_input_processing"}
        ),
    ),
    CreatorExample(
        name="final_aggregate_sink",
        filename="final_aggregate_sink.py",
        model_type="atomic",
        mechanisms=frozenset({"external_jsonl", "final_only_output"}),
    ),
    CreatorExample(
        name="stdin_jsonl_timestamped_event_source",
        filename="stdin_lazy_timestamped_event_source.py",
        model_type="atomic",
        mechanisms=frozenset({"stdin_schedule", "autonomous_start"}),
    ),
    CreatorExample(
        name="stdin_plaintext_timestamped_event_source",
        filename="stdin_plaintext_timestamped_event_source.py",
        model_type="atomic",
        mechanisms=frozenset({"stdin_schedule", "autonomous_start"}),
    ),
    CreatorExample(
        name="stdin_preloaded_schedule_periodic_lookup",
        filename="stdin_preloaded_schedule_periodic_lookup.py",
        model_type="atomic",
        mechanisms=frozenset(
            {
                "stdin_schedule",
                "periodic_internal_event",
                "periodic_schedule_lookup",
                "autonomous_start",
            }
        ),
    ),
    CreatorExample(
        name="stdin_startup_batch_source",
        filename="stdin_startup_batch_source.py",
        model_type="atomic",
        mechanisms=frozenset({"stdin_startup_batch"}),
    ),
    CreatorExample(
        name="file_input_source",
        filename="file_input_source.py",
        model_type="atomic",
        mechanisms=frozenset({"file_read"}),
    ),
    CreatorExample(
        name="file_record_sink",
        filename="file_record_sink.py",
        model_type="atomic",
        mechanisms=frozenset({"file_write"}),
    ),
    CreatorExample(
        name="coupled_composition",
        filename="coupled_composition.py",
        model_type="coupled",
        mechanisms=frozenset({"fixed_composition"}),
    ),
    CreatorExample(
        name="coupled_internal_composition",
        filename="coupled_internal_composition.py",
        model_type="coupled",
        mechanisms=frozenset({"internal_composition"}),
    ),
    CreatorExample(
        name="coupled_single_child_container",
        filename="coupled_single_child_container.py",
        model_type="coupled",
        mechanisms=frozenset({"fixed_composition"}),
    ),
    CreatorExample(
        name="coupled_runtime_family",
        filename="coupled_runtime_family.py",
        model_type="coupled",
        mechanisms=frozenset({"runtime_multiplicity"}),
    ),
)


def format_atomic_example_catalog() -> str:
    """Return the compact behavior catalog shown to atomic detail planners."""
    grouped: dict[str, list[CreatorExample]] = {}
    for item in EXAMPLE_REGISTRY:
        if item.model_type == "atomic":
            grouped.setdefault(item.category, []).append(item)

    lines: list[str] = []
    for category, items in grouped.items():
        lines.append(f"\n{category}:")
        lines.extend(f"- {item.name}: {item.description}" for item in items)
    return "\n".join(lines).strip()


def infer_implementation_mechanisms(model_plan: PlanResult) -> set[str]:
    """Combine declared tags with conservative evidence from the full contract.

    Tags guide retrieval, but an omitted or inaccurate tag must not suppress
    clear timing, IO, startup, or buffering evidence in the plan itself.
    """
    specification = model_plan.model_info.specification
    declared = set(specification.implementation_mechanisms)

    behavior_text = specification.function.casefold()
    protocol_text = " ".join(
        text_part
        for port in (*specification.input_ports, *specification.output_ports)
        for text_part in (
            port.protocol.initial_signal,
            port.protocol.description,
        )
    ).casefold()
    timing_text = f"{behavior_text} {protocol_text}"
    inferred: set[str] = set(declared)

    # Mechanism tags are retrieval hints, not authority over the concrete
    # interface. Ignore stale IO tags that the detailed expansion attached to
    # a model without the corresponding external stream.
    io_targets = {stream.target for stream in specification.external_io}
    if "stdin" not in io_targets:
        inferred.difference_update({"stdin_schedule", "stdin_startup_batch"})

    if model_plan.type == "coupled" and _has_runtime_multiplicity(model_plan):
        inferred.add("runtime_multiplicity")

    if any(
        _has_active_initial_signal(port.protocol.initial_signal)
        for port in specification.output_ports
    ) or any(
        marker in timing_text
        for marker in (
            "autonomous",
            "autonomously",
            "emits at t=0",
            "emit at t=0",
            "sends at t=0",
        )
    ):
        inferred.add("autonomous_start")
    periodic = any(
        marker in timing_text
        for marker in (
            "periodic",
            "every second",
            "each second",
            "every integer second",
            "each integer second",
            "each integer time",
            "at each integer time",
            "inter-arrival",
            "interarrival",
            "1 hz",
            "1-hz",
            "one update per integer simulation second",
            "each simulation time step",
            "each time step",
        )
    )
    input_driven = bool(specification.input_ports) and any(
        marker in behavior_text
        for marker in (
            "on each scheduled value",
            "on each received value",
            "on each input",
            "for each received",
            "based on the received",
        )
    )
    if input_driven and "periodic_internal_event" not in declared:
        # A sibling may own the periodic clock. Repeated observations do not by
        # themselves require this receiver to schedule a competing timer.
        periodic = False
    if periodic:
        inferred.add("periodic_internal_event")
        if specification.input_ports:
            inferred.add("periodic_input_updates")
    if specification.input_ports and specification.output_ports and any(
        marker in behavior_text
        for marker in (
            "instantaneous (zero-simulation-time)",
            "no positive delay is introduced",
            "zero-delay",
            "zero-simulation-time output",
            "same simulation time",
            "immediate handoff",
            "time advance to 0",
        )
    ):
        inferred.add("zero_delay_response")
    if (
        "zero_delay_response" in inferred
        and not specification.output_ports
        and "stdout" in io_targets
    ):
        inferred.discard("zero_delay_response")
    stateful_input = any(
        marker in behavior_text
        for marker in (
            "advance the internal state",
            "advance the state",
            "update the internal state",
            "update the retained state",
            "update state",
            "retained state",
            "retains the previous",
            "retain the new state",
            "based on the received",
        )
    ) or ("previous" in behavior_text and "update" in behavior_text)
    if specification.input_ports and "stdout" in io_targets and stateful_input:
        inferred.add("stateful_input_processing")
    has_timed_behavior = any(
        marker in behavior_text
        for marker in (
            "processing delay",
            "service duration",
            "service time",
            "transmission delay",
            "channel delay",
            "latency",
            "after a delay",
            "after exactly",
            "duration",
            "time-based behavior",
        )
    )
    if stateful_input and not has_timed_behavior:
        inferred.discard("timed_processing")
    if has_timed_behavior:
        inferred.add("timed_processing")
    class_name = model_plan.model_info.class_name.casefold()
    if (
        any(marker in class_name for marker in ("queue", "buffer"))
        or any(
            marker in behavior_text
            for marker in (
                "buffers incoming",
                "buffers jobs",
                "buffer incoming",
                "buffered jobs",
                "maintains a fifo",
                "maintain a fifo",
                "fifo ordering",
                "fifo queue",
                "waiting queue",
                "waiting slot",
            )
        )
    ):
        inferred.add("buffering")
    if any(
        marker in behavior_text
        for marker in (
            "timeout tracking",
            "timeout behavior:",
            "timeout window",
            "timeout expires",
            "on timeout",
            "retry decisions",
            "retransmit after",
        )
    ):
        inferred.add("timeout_feedback")
    if any(
        stream.target == "stdout" and "json" in stream.content.casefold()
        for stream in specification.external_io
    ):
        inferred.add("external_jsonl")
    final_output_text = " ".join(
        stream.content.casefold()
        for stream in specification.external_io
        if stream.target == "stdout"
    )
    if final_output_text and any(
        marker in final_output_text
        for marker in (
            "at the end",
            "at end",
            "final record",
            "final state",
            "once after",
            "exactly one final",
        )
    ):
        inferred.add("final_only_output")
    if specification.output_ports and any(
        stream.target in {"stdout", "stderr", "file"}
        for stream in specification.external_io
    ):
        inferred.add("dual_output")
    stdin_streams = [
        stream for stream in specification.external_io if stream.target == "stdin"
    ]
    if stdin_streams:
        stdin_text = " ".join(
            [behavior_text, *(stream.content.casefold() for stream in stdin_streams)]
        )
        if any(
            marker in stdin_text
            for marker in (
                "read once at startup",
                "all parsed updates at initialization",
                "publish all parsed records at startup",
                "emit all parsed records at time zero",
                "emit all parsed records at t=0",
            )
        ):
            inferred.add("stdin_startup_batch")
        else:
            inferred.add("stdin_schedule")
        if periodic:
            inferred.add("periodic_schedule_lookup")
    file_streams = [
        stream for stream in specification.external_io if stream.target == "file"
    ]
    if file_streams or "file_io" in inferred:
        inferred.discard("file_io")
        modes = set().union(
            *(_file_access_modes(stream.content) for stream in file_streams)
        ) if file_streams else set()
        if modes == {"read"}:
            inferred.add("file_read")
        elif modes == {"write"}:
            inferred.add("file_write")
        else:
            # An ambiguous or genuinely read/write contract receives both
            # complete patterns instead of silently assuming a writer.
            inferred.update({"file_read", "file_write"})
    return inferred


def select_examples(
    model_plan: PlanResult,
    min_atomic_examples: int = 2,
    max_atomic_examples: int = 3,
    original_requirements: str = "",
) -> list[CreatorExample]:
    """Return the planner's exact example, or infer examples for an old plan.

    New atomic detailed plans carry one explicit selection and bypass all
    keyword inference. The remaining multi-example logic is retained only for
    plans created before ``implementation_example`` existed. Coupled examples
    remain structurally selected because coupled detail planning does not use
    the atomic behavior catalog.
    """
    if model_plan.type == "coupled":
        required = infer_implementation_mechanisms(model_plan)
        if "runtime_multiplicity" in required:
            example_name = "coupled_runtime_family"
        elif (
            len(model_plan.children_plan) == 1
            and not model_plan.coupling_rules
            and not model_plan.model_info.specification.input_ports
            and not model_plan.model_info.specification.output_ports
        ):
            example_name = "coupled_single_child_container"
        elif not (
            model_plan.model_info.specification.input_ports
            or model_plan.model_info.specification.output_ports
        ):
            example_name = "coupled_internal_composition"
        else:
            example_name = "coupled_composition"
        return [next(item for item in EXAMPLE_REGISTRY if item.name == example_name)]

    selected_name = model_plan.model_info.specification.implementation_example
    required = infer_implementation_mechanisms(model_plan)
    if selected_name is not None:
        selected = [
            next(
                item
                for item in EXAMPLE_REGISTRY
                if item.model_type == "atomic" and item.name == selected_name
            )
        ]
        return _ensure_stdin_example(
            model_plan,
            selected,
            required,
            original_requirements=original_requirements,
        )

    candidates = [item for item in EXAMPLE_REGISTRY if item.model_type == "atomic"]
    if "timeout_feedback" not in required:
        candidates = [
            item
            for item in candidates
            if item.name
            not in {"timeout_feedback_protocol", "logged_timeout_feedback_protocol"}
        ]
    selected: list[CreatorExample] = []
    uncovered = set(required)
    while uncovered and len(selected) < max_atomic_examples:
        best = max(
            candidates,
            key=lambda item: (
                len(item.mechanisms & uncovered),
                -len(item.mechanisms - required),
                -candidates.index(item),
            ),
        )
        if not (best.mechanisms & uncovered):
            break
        selected.append(best)
        candidates.remove(best)
        uncovered -= best.mechanisms

    if not selected:
        return _ensure_stdin_example(
            model_plan,
            [
                next(
                    item
                    for item in EXAMPLE_REGISTRY
                    if item.name == "reactive_zero_delay_router"
                )
            ],
            required,
            max_examples=max_atomic_examples,
            original_requirements=original_requirements,
        )

    # A dual-boundary example is deliberately a complete integration pattern.
    # Adding a second state-machine example after it has covered the contract
    # encourages models to splice incompatible transition shapes and duplicate
    # the same semantic output.
    if not uncovered and any(
        example.mechanisms
        & {
            "dual_output",
            "final_only_output",
            "periodic_schedule_lookup",
            "stateful_input_processing",
        }
        for example in selected
    ):
        return _ensure_stdin_example(
            model_plan,
            selected,
            required,
            max_examples=max_atomic_examples,
            original_requirements=original_requirements,
        )

    related = [
        item
        for item in candidates
        if item.mechanisms & required and not (item.mechanisms - required)
    ]
    target_count = min_atomic_examples
    target_count = min(max_atomic_examples, target_count)

    # After covering the contract, add alternate implementations only when all
    # of their mechanisms are already required.  A periodic example with an
    # unrelated autonomous t=0 output, for example, is not safe diversity.
    while related and len(selected) < target_count:
        def fill_score(item: CreatorExample) -> tuple[float, int, int, int]:
            overlap = len(item.mechanisms & required)
            extras = len(item.mechanisms - required)
            max_similarity = max(
                (
                    len(item.mechanisms & prior.mechanisms)
                    / len(item.mechanisms | prior.mechanisms)
                )
                for prior in selected
            )
            return (
                overlap - 0.25 * extras - 0.5 * max_similarity,
                overlap,
                -extras,
                -candidates.index(item),
            )

        alternate = max(related, key=fill_score)
        selected.append(alternate)
        candidates.remove(alternate)
        related.remove(alternate)
    return _ensure_stdin_example(
        model_plan,
        selected,
        required,
        max_examples=max_atomic_examples,
        original_requirements=original_requirements,
    )
