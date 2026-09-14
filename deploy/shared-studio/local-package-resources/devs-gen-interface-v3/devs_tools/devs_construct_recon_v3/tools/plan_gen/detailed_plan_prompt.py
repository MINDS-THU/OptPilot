BASE_PROMPT = """
<SystemRole>
You are a DEVS System Architect.
</SystemRole>

<TargetContext>
Module Name: {target_name}
Module Type: {module_type}
Direct Children: {children_names_str}
</TargetContext>
"""

COUPLED_INSTRUCTION = """
<TaskInstruction>
Generate a detailed specification for the ROOT COUPLED module and canonical
interface specifications for its direct children.

[STEP 1: Design Coupled Wrapper (detailed_plan)]
- `function` summarizes what the complete subtree achieves. The coupled class
  itself contains components and couplings, not active state-transition logic.
- The coupled class does not call stdin, stdout, stderr, files, or services.
  Record each such operation on a direct child subtree; a leaf atomic model in
  that subtree will perform it.

[STEP 2: Design Children (children_plans)]
For each direct child, execute this workflow:
1. Copy `model_type` based on the global plan.
2. Briefly describe responsibility in `function`. Replace a coarse phrase such
   as "emits an event" with the concrete mechanism: sends a DEVS message to a
   named consumer, writes a record to an OS stream, or explicitly does both.
3. List the child's init args and preserve configuration needed below it,
   including runtime multiplicity.
4. Define ports needed to connect this child to a sibling or to the current
   coupled boundary. Give matching ports compatible payload types and protocols.
   If one child must wait for a sibling to become available or complete work,
   give the waiting child a feedback input and include the return route.
5. For an atomic child, `external_io` lists only OS operations that child
   directly performs. For a coupled child, it lists operations that must be
   delegated further inside that subtree.
6. If the requirements declare a consumable stdin stream, exactly one child
   subtree receives it and other children receive parsed data through DEVS
   ports. If the requirements say that no stdin is required, no child receives
   stdin; command-line configuration travels through constructor arguments.
7. For ordinary event JSONL, the atomic model that changes state writes that
   state record, and the atomic model that sends a DEVS message writes that
   message record. Emit each required record once. Do not introduce or use a
   central writer unless the requirements explicitly need cross-model
   aggregation, global ordering beyond simulation-time order, or one final
   combined report. Before returning, compare every explicitly required record
   type with the children's `external_io`; each must be covered where it is
   produced, even if the coarse global-plan description omitted stdout. Treat
   the stated record schemas as literal contracts: preserve every required
   envelope and payload field rather than shortening a repeated schema.
8. Populate each child's `related_requirement_ids` while its concrete
   constructor, ports, behavior, and external IO are in view. Include every
   ledger item relevant to planning or implementing that child subtree,
   including shared workflow, schema, timing, and parameter items. The same ID
   may appear on multiple cooperating children. Complete or correct the coarse
   global-plan associations rather than merely copying them.

Use the shared `<FieldContract>` below for the exact content required in each field.

[STEP 3: Design couplings]
- Finalize every child's input and output ports before writing this list. Then
  build each route by copying its endpoint names from those final port lists;
  do not invent, rename, or reverse a port while writing `coupling_rules`.
- Before connecting edges, distinguish the semantic roles of events received by
  each child. Different events such as a new arrival and completion feedback
  need separate input ports unless the port protocol explicitly defines a
  tagged union that distinguishes them.
- Return `coupling_rules` as a list with one complete routing rule per item.
  Ordinary language is allowed. A rule may describe one fixed connection or an
  indexed, repeated, conditional, one-to-many, or many-to-one family.
- List actual routes only; use an empty list when no coupling is needed rather
  than adding architectural commentary or notes about absent EIC, IC, or EOC
  routes.
- Use `parent.<input_port>` for an EIC from the current DEVS boundary and
  `parent.<output_port>` for an EOC to that boundary.
- `parent` exposes only the DEVS ports declared for the current coupled model.
  OS targets such as stdin, stdout, and files are `external_io`, not ports, and
  must never appear as coupling endpoints.
- Name the relevant model instances or families and ports clearly enough for
  code generation. Include required startup and busy/ready feedback routes.
  Configuration travels through constructor arguments, not invented ports.
- In a fixed route, a child source is one of that child's output ports and a
  child target is one of that child's input ports. Read the declared interfaces;
  do not infer direction from a port's name.
- Before returning, compare every fixed endpoint with the interfaces in this
  same response. Do not invent a self-route for logging; a planned aggregation
  sink is a target unless the requirements explicitly give it an outgoing DEVS
  message.
- A group rule must name its member set or family and a port declared by every
  member. Do not say "all modules" when that would include a sink or a child
  without the named output.
</TaskInstruction>
"""


RESPONSIBILITY_GUIDANCE = """
<PlanningRule>
The global plan gives starting roles, not complete behavior. Keep a sole stdin reader,
but add duties or ports needed for the workflow. One leaf atomic model directly
reads each external input stream; other models receive data through DEVS ports. If future arrivals
and a business model's timer would compete for one next internal event, use a
separate source atomic. Do not invent user-visible behavior.
</PlanningRule>
"""


COUPLING_EXAMPLE = """
<CouplingVariants>
The same rule list can describe a current DEVS boundary or a runtime-sized
family. Every endpoint below is a declared DEVS port; stdin, stdout, and files
would instead appear in a leaf atomic model's `external_io`:
[
  "Forward parent.request_in to Dispatcher.request_in.",
  "For every Worker instance created from worker_count, connect Dispatcher.job to Worker.job.",
  "Connect Server.available to Dispatcher.server_available so queued work is released only when the server is ready.",
  "Forward every Worker.result_out to parent.result_out."
]
Read each arrow literally: for `A.x -> B.y`, `x` is an output port of A and
`y` is an input port of B. An atomic model's internal state changes belong in
its transition logic and need no self-coupling.

A named output record is not automatically a DEVS message. For example, if a
Server writes a `job_completed` record when its own timed service finishes and
no other model is required to receive that event, do not invent a
`Server.job_completed -> Server.job_completed` route. If another model must use
the completion to release work, declare that consumer input and route to it.
Likewise, a rejected request that is logged and then leaves the system has no
route back to the input source unless the requirements name a recovery action.

For a real return message, put the port on the side that uses it. If Worker
finishes work and Queue consumes that fact, declare `completed_out` in Worker's
`output_ports`, declare `completion_in` in Queue's `input_ports`, and write
`Worker.completed_out -> Queue.completion_in`. Do not put `completion_in` in
Queue's `output_ports` merely because Queue owns the corresponding state.

At a nested boundary, a parent connects only to its direct child. For example,
the root rule `Gateway.packet_out -> Server.packet_in` uses a port declared on
the coupled Server; Server's own detail plan can then route
`parent.packet_in -> Receiver.packet_in`. The root must not address
`Server.Receiver.packet_in` directly.

One output coupled to several inputs broadcasts every message. If a Dispatcher
must select exactly one of Worker1 and Worker2, give it separate `to_worker_1`
and `to_worker_2` outputs, or state a tagged-message filter explicitly in each
receiver's input protocol; two plain couplings do not perform selection.
For a runtime-sized family, include a stable per-instance identifier such as
`worker_id` in the child blueprint constructor whenever routing, payloads, or
logs use that identifier. If a tagged assignment is broadcast to the family,
state explicitly that each worker ignores assignments for other IDs.

When an expiring Queue must retain work but a separate Coordinator owns resource
selection, use an explicit handshake instead of transferring the work early.
One valid pattern is: `Queue.work_available -> Coordinator.work_available` is
only a notice; `Coordinator.claim(resource_id) -> Queue.claim_in` offers a
selected resource; `Queue.claimed_work(work, resource_id) ->
Coordinator.claimed_work` atomically removes the current non-expired work; then
the Coordinator emits and records the assignment. A notice alone does not stop
the Queue's expiration timer.
</CouplingVariants>
"""


ROOT_COUPLED_EXAMPLE = r"""
<StageExample>
This is a complete ROOT COUPLED response for exclusive dispatch with no stdin.
It demonstrates three distinct boundaries: a port-only message has an internal
consumer, an external-only record has no invented port, and one semantic event
may require both a DEVS port emission and a separate stdout record. The internal
assignment payload preserves `arrival_time` even though the smaller public log
schema does not expose it. CLI configuration is propagated by constructors;
stdout never appears in `coupling_rules`.
The empty requirement-ID lists are specific to this example, which has no live
ledger. In a real response, use the relevant IDs shown in the current prompt.
{
  "detailed_plan": {
    "class_name": "TwoWorkerSystem",
    "model_type": "coupled",
    "function": "Queue arriving work and assign each item to exactly one available worker.",
    "model_init_args": [
      {"name":"name","type":"str","structure":"Model instance name"},
      {"name":"parent","type":"object","structure":"Framework parent reference or None"},
      {"name":"arrival_interval","type":"float","structure":"Seconds between jobs; supplied by the runner from CLI and passed to JobSource"},
      {"name":"service_time","type":"float","structure":"Seconds per job; explicit scenario configuration"}
    ],
    "input_ports": [],
    "output_ports": []
  },
  "children_plans": [
    {
      "class_name":"JobSource","model_type":"atomic",
      "function":"Generate sequential jobs from configured inter-arrival parameters. Each generation both sends the internal job payload through job_out and independently writes the required job_generated record to stdout.",
      "external_io":[
        {"target":"stdout","content":"At each generation write one JSONL record. Format: {'time': float, 'event': 'job_generated', 'payload': {'work_id': int, 'arrival_time': float}}."}
      ],
      "model_init_args":[
        {"name":"name","type":"str","structure":"Model instance name"},
        {"name":"parent","type":"object","structure":"Framework parent reference or None"},
        {"name":"arrival_interval","type":"float","structure":"Seconds between jobs; explicit scenario configuration"}
      ],
      "input_ports":[],
      "output_ports":[
        {"name":"job_out","type":"dict","structure":"{'work_id': int, 'arrival_time': float}","protocol":{"initial_signal":"at t=0","description":"Send each generated job to DispatchQueue."}}
      ],
      "related_requirement_ids":[]
    },
    {
      "class_name":"DispatchQueue","model_type":"atomic",
      "function":"Receive generated jobs, preserve FIFO arrival data, and assign each job to exactly one available worker. At each assignment, both send the richer internal payload on the selected worker port and independently write the smaller required job_assigned record to stdout; neither effect replaces the other.",
      "external_io":[
        {"target":"stdout","content":"At each assignment write one JSONL record. Format: {'time': float, 'event': 'job_assigned', 'payload': {'work_id': int, 'worker_id': 1|2, 'assigned_at': float}}. Do not include the internal arrival_time field."}
      ],
      "model_init_args":[
        {"name":"name","type":"str","structure":"Model instance name"},
        {"name":"parent","type":"object","structure":"Framework parent reference or None"}
      ],
      "input_ports":[
        {"name":"job_in","type":"dict","structure":"{'work_id': int, 'arrival_time': float}","protocol":{"initial_signal":"at t=0 from JobSource","description":"Receive generated jobs in arrival order."}},
        {"name":"worker_1_available","type":"dict","structure":"{'worker_id': 1}","protocol":{"initial_signal":"None","description":"Receive Worker1 availability."}},
        {"name":"worker_2_available","type":"dict","structure":"{'worker_id': 2}","protocol":{"initial_signal":"None","description":"Receive Worker2 availability."}}
      ],
      "output_ports":[
        {"name":"to_worker_1","type":"dict","structure":"{'work_id': int, 'arrival_time': float, 'worker_id': 1, 'assigned_at': float}","protocol":{"initial_signal":"None","description":"Send an assignment only when Worker1 was selected."}},
        {"name":"to_worker_2","type":"dict","structure":"{'work_id': int, 'arrival_time': float, 'worker_id': 2, 'assigned_at': float}","protocol":{"initial_signal":"None","description":"Send an assignment only when Worker2 was selected."}}
      ],
      "related_requirement_ids":[]
    },
    {
      "class_name":"Worker1","model_type":"atomic",
      "function":"At t=0 and after each completion, both emit availability to DispatchQueue through available_out and independently write worker_available to stdout. Process one assigned job at a time using the service_time as an internal DEVS timer; do not create timer-start/timer-expired ports or a self-coupling for that timer. Write work_completed to stdout; completion has no DEVS output port because no model consumes it.",
      "external_io":[
        {"target":"stdout","content":"At t=0 and after each completion write one JSONL record. Format: {'time': float, 'event': 'worker_available', 'payload': {'worker_id': 1}}."},
        {"target":"stdout","content":"After service write one JSONL record. Format: {'time': float, 'event': 'work_completed', 'payload': {'work_id': int, 'arrival_time': float}}."}
      ],
      "model_init_args":[
        {"name":"name","type":"str","structure":"Model instance name"},
        {"name":"parent","type":"object","structure":"Framework parent reference or None"},
        {"name":"service_time","type":"float","structure":"Seconds per job; supplied by the parent"}
      ],
      "input_ports":[{"name":"job_in","type":"dict","structure":"{'work_id': int, 'arrival_time': float, 'worker_id': 1, 'assigned_at': float}","protocol":{"initial_signal":"None","description":"Receive only jobs selected for Worker1."}}],
      "output_ports":[{"name":"available_out","type":"dict","structure":"{'worker_id': 1}","protocol":{"initial_signal":"at t=0 and after every completion","description":"Notify DispatchQueue that Worker1 can accept one job."}}],
      "related_requirement_ids":[]
    },
    {
      "class_name":"Worker2","model_type":"atomic",
      "function":"At t=0 and after each completion, both emit availability to DispatchQueue through available_out and independently write worker_available to stdout. Process one assigned job at a time using the service_time as an internal DEVS timer; do not create timer-start/timer-expired ports or a self-coupling for that timer. Write work_completed to stdout; completion has no DEVS output port because no model consumes it.",
      "external_io":[
        {"target":"stdout","content":"At t=0 and after each completion write one JSONL record. Format: {'time': float, 'event': 'worker_available', 'payload': {'worker_id': 2}}."},
        {"target":"stdout","content":"After service write one JSONL record. Format: {'time': float, 'event': 'work_completed', 'payload': {'work_id': int, 'arrival_time': float}}."}
      ],
      "model_init_args":[
        {"name":"name","type":"str","structure":"Model instance name"},
        {"name":"parent","type":"object","structure":"Framework parent reference or None"},
        {"name":"service_time","type":"float","structure":"Seconds per job; supplied by the parent"}
      ],
      "input_ports":[{"name":"job_in","type":"dict","structure":"{'work_id': int, 'arrival_time': float, 'worker_id': 2, 'assigned_at': float}","protocol":{"initial_signal":"None","description":"Receive only jobs selected for Worker2."}}],
      "output_ports":[{"name":"available_out","type":"dict","structure":"{'worker_id': 2}","protocol":{"initial_signal":"at t=0 and after every completion","description":"Notify DispatchQueue that Worker2 can accept one job."}}],
      "related_requirement_ids":[]
    }
  ],
  "coupling_rules":[
    "Connect JobSource.job_out to DispatchQueue.job_in.",
    "Connect DispatchQueue.to_worker_1 to Worker1.job_in.",
    "Connect DispatchQueue.to_worker_2 to Worker2.job_in.",
    "Connect Worker1.available_out to DispatchQueue.worker_1_available.",
    "Connect Worker2.available_out to DispatchQueue.worker_2_available."
  ]
}
</StageExample>
"""


ROOT_OWNERSHIP_HANDSHAKE_COUPLED_EXAMPLE = r"""
<StageExample>
This complete ROOT COUPLED example separates ownership of waiting work from
selection of an available resource. It is a mechanism example, not an
architecture to copy when the current requirements specify different flows.
The queue retains each item until a claim arrives; availability goes to the
allocator, the allocator offers a selected resource ID to the queue, the queue
returns one claimed item with that ID, and only then does the allocator emit an
assignment. Records written to stdout do not become DEVS ports unless another
model consumes the same event.
{
  "detailed_plan": {
    "class_name":"DispatchSystem","model_type":"coupled",
    "function":"Retain waiting work until an allocator matches it to an available resource, then deliver completed work to a sink.",
    "model_init_args":[
      {"name":"name","type":"str","structure":"Model instance name"},
      {"name":"parent","type":"object","structure":"Framework parent reference or None"},
      {"name":"resource_count","type":"int","structure":"Number of Resource instances"}
    ],
    "input_ports":[],"output_ports":[]
  },
  "children_plans":[
    {
      "class_name":"WorkSource","model_type":"atomic",
      "function":"Generate work items and send the complete internal payload to WaitingQueue; independently write each generated record to stdout.",
      "external_io":[{"target":"stdout","content":"For each generated item write one JSONL generated record."}],
      "model_init_args":[
        {"name":"name","type":"str","structure":"Model instance name"},
        {"name":"parent","type":"object","structure":"Framework parent reference or None"}
      ],
      "input_ports":[],
      "output_ports":[{"name":"work_out","type":"dict","structure":"{'work_id': int, 'created_at': float}","protocol":{"initial_signal":"at t=0","description":"Send generated work to WaitingQueue."}}],
      "related_requirement_ids":[]
    },
    {
      "class_name":"WaitingQueue","model_type":"atomic",
      "function":"Retain FIFO work and any expiration timers. On a claim containing a selected resource_id, atomically remove one eligible item and return both IDs; independently write queue and expiration records to stdout.",
      "external_io":[{"target":"stdout","content":"Write required queued and expired JSONL records when those state changes occur."}],
      "model_init_args":[
        {"name":"name","type":"str","structure":"Model instance name"},
        {"name":"parent","type":"object","structure":"Framework parent reference or None"}
      ],
      "input_ports":[
        {"name":"work_in","type":"dict","structure":"{'work_id': int, 'created_at': float}","protocol":{"initial_signal":"at t=0 from WorkSource","description":"Receive work that remains owned here until claimed."}},
        {"name":"claim_in","type":"dict","structure":"{'resource_id': int}","protocol":{"initial_signal":"None","description":"Receive the allocator's selected resource ID."}}
      ],
      "output_ports":[{"name":"claimed_out","type":"dict","structure":"{'resource_id': int, 'work_id': int, 'created_at': float}","protocol":{"initial_signal":"None","description":"Return one FIFO work item in response to claim_in."}}],
      "related_requirement_ids":[]
    },
    {
      "class_name":"Allocator","model_type":"atomic",
      "function":"Remember available resource IDs. Offer one selected ID to WaitingQueue; after receiving the claimed work, emit a tagged assignment and independently write the assignment record to stdout.",
      "external_io":[{"target":"stdout","content":"For each completed match write one JSONL assignment record."}],
      "model_init_args":[
        {"name":"name","type":"str","structure":"Model instance name"},
        {"name":"parent","type":"object","structure":"Framework parent reference or None"}
      ],
      "input_ports":[
        {"name":"available_in","type":"dict","structure":"{'resource_id': int}","protocol":{"initial_signal":"at t=0 and after completion","description":"Receive resource availability."}},
        {"name":"claimed_in","type":"dict","structure":"{'resource_id': int, 'work_id': int, 'created_at': float}","protocol":{"initial_signal":"None","description":"Receive the item claimed for the offered resource."}}
      ],
      "output_ports":[
        {"name":"claim_out","type":"dict","structure":"{'resource_id': int}","protocol":{"initial_signal":"None","description":"Offer an available resource ID to WaitingQueue."}},
        {"name":"assignment_out","type":"dict","structure":"{'resource_id': int, 'work_id': int, 'created_at': float}","protocol":{"initial_signal":"None","description":"Broadcast a tagged assignment; only the addressed Resource accepts it."}}
      ],
      "related_requirement_ids":[]
    },
    {
      "class_name":"Resource","model_type":"atomic",
      "function":"Runtime-sized resource blueprint. Emit its stable resource_id initially and after completing work; accept only tagged assignments for that ID; send a distinct completed event to CompletionSink.",
      "external_io":[],
      "model_init_args":[
        {"name":"name","type":"str","structure":"Model instance name"},
        {"name":"parent","type":"object","structure":"Framework parent reference or None"},
        {"name":"resource_id","type":"int","structure":"Stable 1-based runtime instance ID"}
      ],
      "input_ports":[{"name":"assignment_in","type":"dict","structure":"{'resource_id': int, 'work_id': int, 'created_at': float}","protocol":{"initial_signal":"None","description":"Accept only assignments whose resource_id matches this instance."}}],
      "output_ports":[
        {"name":"available_out","type":"dict","structure":"{'resource_id': int}","protocol":{"initial_signal":"at t=0 and after every completion","description":"Report availability to Allocator."}},
        {"name":"completed_out","type":"dict","structure":"{'resource_id': int, 'work_id': int, 'created_at': float}","protocol":{"initial_signal":"None","description":"Send the later completion event to CompletionSink."}}
      ],
      "related_requirement_ids":[]
    },
    {
      "class_name":"CompletionSink","model_type":"atomic",
      "function":"Consume completed work and write the required completion record; it has no output port because no model consumes a later event.",
      "external_io":[{"target":"stdout","content":"For each completed input write one JSONL completion record."}],
      "model_init_args":[
        {"name":"name","type":"str","structure":"Model instance name"},
        {"name":"parent","type":"object","structure":"Framework parent reference or None"}
      ],
      "input_ports":[{"name":"completed_in","type":"dict","structure":"{'resource_id': int, 'work_id': int, 'created_at': float}","protocol":{"initial_signal":"None","description":"Receive completed work from Resource."}}],
      "output_ports":[],"related_requirement_ids":[]
    }
  ],
  "coupling_rules":[
    "Connect WorkSource.work_out to WaitingQueue.work_in.",
    "Connect every Resource.available_out instance to Allocator.available_in.",
    "Connect Allocator.claim_out to WaitingQueue.claim_in.",
    "Connect WaitingQueue.claimed_out to Allocator.claimed_in.",
    "Connect Allocator.assignment_out to every Resource.assignment_in instance; each Resource accepts only its own resource_id.",
    "Connect every Resource.completed_out instance to CompletionSink.completed_in."
  ]
}
</StageExample>
"""


ROOT_TIMEOUT_FEEDBACK_COUPLED_EXAMPLE = r"""
<StageExample>
This is a complete ROOT COUPLED response for a stop-and-wait protocol with a
forward link and a feedback link. Only messages consumed by another model use
ports. Preparation delays, link delays, processing delays, and the sender's
ACK timeout are internal DEVS schedules: they need no timer ports or
self-couplings. JSONL records are independent external observations.
{
  "detailed_plan": {
    "class_name":"ReliableTransferSystem","model_type":"coupled",
    "function":"Route one stop-and-wait transfer through forward and feedback links.",
    "model_init_args":[
      {"name":"name","type":"str","structure":"Model instance name"},
      {"name":"parent","type":"object","structure":"Framework parent reference or None"},
      {"name":"item_count","type":"int","structure":"Number of items"},
      {"name":"preparation_delay","type":"float","structure":"Sender preparation delay"},
      {"name":"timeout","type":"float","structure":"Sender-internal feedback timeout"},
      {"name":"link_delay","type":"float","structure":"Delay within each link"}
    ],
    "input_ports":[],"output_ports":[]
  },
  "children_plans":[
    {
      "class_name":"TransferSender","model_type":"atomic",
      "function":"Start autonomously, prepare and send one numbered item, then wait internally for matching feedback. Schedule timeout inside this atomic model; on expiry prepare a retry, and on matching feedback cancel the timeout or pending retry and advance.",
      "external_io":[{"target":"stdout","content":"At each send write one JSONL record with time, item_id, and is_retry; at each feedback arrival write one JSONL validation record."}],
      "model_init_args":[
        {"name":"name","type":"str","structure":"Model instance name"},
        {"name":"parent","type":"object","structure":"Framework parent reference or None"},
        {"name":"item_count","type":"int","structure":"Parent init arg"},
        {"name":"preparation_delay","type":"float","structure":"Parent init arg"},
        {"name":"timeout","type":"float","structure":"Internal timeout; not a port"}
      ],
      "input_ports":[{"name":"feedback_in","type":"dict","structure":"{'item_id': int, 'accepted': bool}","protocol":{"initial_signal":"None","description":"Receive feedback from FeedbackLink."}}],
      "output_ports":[{"name":"item_out","type":"dict","structure":"{'item_id': int, 'is_retry': bool}","protocol":{"initial_signal":"after preparation","description":"Send the current item to ForwardLink."}}],
      "related_requirement_ids":[]
    },
    {
      "class_name":"ForwardLink","model_type":"atomic",
      "function":"Apply the required forward-link fate decision and, when passed, forward the retained item after link_delay.",
      "external_io":[],
      "model_init_args":[
        {"name":"name","type":"str","structure":"Model instance name"},
        {"name":"parent","type":"object","structure":"Framework parent reference or None"},
        {"name":"link_delay","type":"float","structure":"Parent init arg"}
      ],
      "input_ports":[{"name":"item_in","type":"dict","structure":"{'item_id': int, 'is_retry': bool}","protocol":{"initial_signal":"None","description":"Receive an item from TransferSender."}}],
      "output_ports":[{"name":"item_out","type":"dict","structure":"{'item_id': int, 'is_retry': bool}","protocol":{"initial_signal":"None","description":"Forward a passed item after link_delay."}}],
      "related_requirement_ids":[]
    },
    {
      "class_name":"TransferReceiver","model_type":"atomic",
      "function":"Process each delivered item for its internal delay and emit matching feedback.",
      "external_io":[{"target":"stdout","content":"At processing start and completion write the required receiver JSONL records."}],
      "model_init_args":[
        {"name":"name","type":"str","structure":"Model instance name"},
        {"name":"parent","type":"object","structure":"Framework parent reference or None"}
      ],
      "input_ports":[{"name":"item_in","type":"dict","structure":"{'item_id': int, 'is_retry': bool}","protocol":{"initial_signal":"None","description":"Receive items from ForwardLink."}}],
      "output_ports":[{"name":"feedback_out","type":"dict","structure":"{'item_id': int, 'accepted': bool}","protocol":{"initial_signal":"None","description":"Send feedback to FeedbackLink after processing."}}],
      "related_requirement_ids":[]
    },
    {
      "class_name":"FeedbackLink","model_type":"atomic",
      "function":"Apply the required feedback-link fate decision and, when passed, forward the retained feedback after link_delay.",
      "external_io":[],
      "model_init_args":[
        {"name":"name","type":"str","structure":"Model instance name"},
        {"name":"parent","type":"object","structure":"Framework parent reference or None"},
        {"name":"link_delay","type":"float","structure":"Parent init arg"}
      ],
      "input_ports":[{"name":"feedback_in","type":"dict","structure":"{'item_id': int, 'accepted': bool}","protocol":{"initial_signal":"None","description":"Receive feedback from TransferReceiver."}}],
      "output_ports":[{"name":"feedback_out","type":"dict","structure":"{'item_id': int, 'accepted': bool}","protocol":{"initial_signal":"None","description":"Forward passed feedback to TransferSender after link_delay."}}],
      "related_requirement_ids":[]
    }
  ],
  "coupling_rules":[
    "Connect TransferSender.item_out to ForwardLink.item_in.",
    "Connect ForwardLink.item_out to TransferReceiver.item_in.",
    "Connect TransferReceiver.feedback_out to FeedbackLink.feedback_in.",
    "Connect FeedbackLink.feedback_out to TransferSender.feedback_in."
  ]
}
</StageExample>
"""


ROOT_AGGREGATED_REPORT_COUPLED_EXAMPLE = r"""
<StageExample>
This is a complete ROOT COUPLED response for a timed request pipeline whose
observable output is exactly one combined JSON document. Producers send facts
through DEVS ports; they do not print JSONL. One collector retains those facts
and writes stdout once from `exit()` after the simulation has finished. Copy
every field of the current requirements' final document into the collector's
external_io contract; do not simplify that schema.
Keep the business request and its public input-event record on separate ports
when their field layouts differ. The processing model must also send one result
fact for every request, including ignored requests; a collector cannot infer an
ignored request or its original input time from later successful-stage events.
{
  "detailed_plan": {
    "class_name":"AccessSystem","model_type":"coupled",
    "function":"Route requests through one timed pipeline and collect all observable facts into one final report.",
    "model_init_args":[
      {"name":"name","type":"str","structure":"Model instance name"},
      {"name":"parent","type":"object","structure":"Framework parent reference or None"},
      {"name":"input_path","type":"str","structure":"Request file supplied by the runner"},
      {"name":"test_name","type":"str","structure":"Identifier copied into the final report"}
    ],
    "input_ports":[],"output_ports":[]
  },
  "children_plans":[
    {
      "class_name":"RequestSource","model_type":"atomic",
      "function":"Read the request file once, schedule each parsed request at its timestamp, send the business request to AccessPipeline, and send the corresponding input event fact to ReportCollector. It never writes stdout.",
      "external_io":[{"target":"file","content":"At initialization read every non-empty timestamped request line from input_path."}],
      "model_init_args":[
        {"name":"name","type":"str","structure":"Model instance name"},
        {"name":"parent","type":"object","structure":"Framework parent reference or None"},
        {"name":"input_path","type":"str","structure":"Parent init arg"}
      ],
      "input_ports":[],
      "output_ports":[
        {"name":"request_out","type":"dict","structure":"Business request. Format: {'input_time': float, 'port': int, 'value': int}.","protocol":{"initial_signal":"at each input timestamp","description":"Send every request to AccessPipeline without replacing its business fields by the public event-record fields."}},
        {"name":"input_fact_out","type":"dict","structure":"Public input event. Format: {'time': float, 'component': str, 'message': str}.","protocol":{"initial_signal":"at each input timestamp","description":"Send the separately constructed input observation to ReportCollector."}}
      ],
      "related_requirement_ids":[]
    },
    {
      "class_name":"AccessPipeline","model_type":"atomic",
      "function":"Apply the complete busy rule and ordered timed stages. Send each accepted or ignored operation fact and each stage event fact to ReportCollector; retain all business state internally and never write stdout.",
      "external_io":[],
      "model_init_args":[
        {"name":"name","type":"str","structure":"Model instance name"},
        {"name":"parent","type":"object","structure":"Framework parent reference or None"}
      ],
      "input_ports":[{"name":"request_in","type":"dict","structure":"Business request. Format: {'input_time': float, 'port': int, 'value': int}.","protocol":{"initial_signal":"None","description":"Receive every scheduled request with exactly the same fields as RequestSource.request_out."}}],
      "output_ports":[
        {"name":"event_fact_out","type":"dict","structure":"Public stage event. Format: {'time': float, 'component': str, 'message': str, 'state': str when required}.","protocol":{"initial_signal":"None","description":"Send each produced stage event to ReportCollector."}},
        {"name":"operation_fact_out","type":"dict","structure":"Operation result. Format: {'input_time': float, 'action': str, 'completed': bool, 'completion_time': float or None}.","protocol":{"initial_signal":"None","description":"Send one operation result for every input request, including ignored requests."}}
      ],
      "related_requirement_ids":[]
    },
    {
      "class_name":"ReportCollector","model_type":"atomic",
      "function":"Collect input and pipeline event facts plus one operation fact per request, sort them as required, derive the final state and simulation time, and write exactly one final JSON object when the simulation exits.",
      "external_io":[{"target":"stdout","content":"In exit() write exactly one JSON object and no JSONL. Preserve every required top-level field and every nested event/operation field exactly as stated in the current requirements."}],
      "model_init_args":[
        {"name":"name","type":"str","structure":"Model instance name"},
        {"name":"parent","type":"object","structure":"Framework parent reference or None"},
        {"name":"test_name","type":"str","structure":"Parent init arg copied into the final document"}
      ],
      "input_ports":[
        {"name":"input_fact_in","type":"dict","structure":"Public input event. Format: {'time': float, 'component': str, 'message': str}.","protocol":{"initial_signal":"None","description":"Receive every input observation from RequestSource."}},
        {"name":"event_fact_in","type":"dict","structure":"Public stage event. Format: {'time': float, 'component': str, 'message': str, 'state': str when required}.","protocol":{"initial_signal":"None","description":"Receive all later events from AccessPipeline."}},
        {"name":"operation_fact_in","type":"dict","structure":"Operation result. Format: {'input_time': float, 'action': str, 'completed': bool, 'completion_time': float or None}.","protocol":{"initial_signal":"None","description":"Receive one result for every request."}}
      ],
      "output_ports":[],
      "related_requirement_ids":[]
    }
  ],
  "coupling_rules":[
    "Connect RequestSource.request_out to AccessPipeline.request_in.",
    "Connect RequestSource.input_fact_out to ReportCollector.input_fact_in.",
    "Connect AccessPipeline.event_fact_out to ReportCollector.event_fact_in.",
    "Connect AccessPipeline.operation_fact_out to ReportCollector.operation_fact_in."
  ]
}
</StageExample>
"""


ROOT_FINAL_ONLY_COUPLED_EXAMPLE = r"""
<StageExample>
This is a complete ROOT COUPLED response for one tightly coupled state machine
that writes one final record. The root is only a structural container. CLI
values pass through constructors; the child needs no configuration port. The
child writes its own final state, so no OutputHandler, final-state port, or
coupling is needed.
{
  "detailed_plan": {
    "class_name": "PopulationSystem",
    "model_type": "coupled",
    "function": "Contain one autonomous population process.",
    "model_init_args": [
      {"name":"name","type":"str","structure":"Model instance name"},
      {"name":"parent","type":"object","structure":"Framework parent reference or None"},
      {"name":"step","type":"float","structure":"Positive simulation step supplied by the runner from CLI"},
      {"name":"simulation_time","type":"float","structure":"Final reporting horizon supplied by the runner from CLI"},
      {"name":"total_population","type":"int","structure":"Initial population supplied by the runner from CLI"}
    ],
    "input_ports": [],
    "output_ports": []
  },
  "children_plans": [
    {
      "class_name":"PopulationProcess","model_type":"atomic",
      "function":"Initialize the complete state from constructor parameters, advance at positive fixed steps strictly before simulation_time, and write exactly one final state with time=simulation_time.",
      "external_io":[{"target":"stdout","content":"At the end of simulation write exactly one JSON object. Format: {'time': float, 'active': float, 'inactive': float}. Values come from this model's final internal state."}],
      "model_init_args":[
        {"name":"name","type":"str","structure":"Model instance name"},
        {"name":"parent","type":"object","structure":"Framework parent reference or None"},
        {"name":"step","type":"float","structure":"Parent init arg"},
        {"name":"simulation_time","type":"float","structure":"Parent init arg"},
        {"name":"total_population","type":"int","structure":"Parent init arg"}
      ],
      "input_ports":[],"output_ports":[],
      "related_requirement_ids":[]
    }
  ],
  "coupling_rules":[]
}
</StageExample>
"""


ROOT_TIMED_STDIN_COUPLED_EXAMPLE = r"""
<StageExample>
This is a complete ROOT COUPLED response for a timestamped stdin schedule that
drives observations at whole simulation times. One source owns stdin and the
simulation clock. The state model reacts to each scheduled value, retains its
own previous state, and writes the observation it produces. A value remembered
by that same atomic model is internal state, not a feedback port or self-route.
{
  "detailed_plan": {
    "class_name":"ScheduledControlSystem","model_type":"coupled",
    "function":"Apply scheduled environment values to one state process and write its observations.",
    "model_init_args":[
      {"name":"name","type":"str","structure":"Model instance name"},
      {"name":"parent","type":"object","structure":"Framework parent reference or None"},
      {"name":"simulation_time","type":"float","structure":"Final whole-second observation time supplied by the runner"}
    ],
    "input_ports":[],"output_ports":[]
  },
  "children_plans":[
    {
      "class_name":"ScheduleSource","model_type":"atomic",
      "function":"Read the complete timestamped schedule from stdin once. At each integer time from 1 through int(simulation_time), select the latest value whose timestamp is not later than that time, or the specified default when none exists, and send it through value_out.",
      "external_io":[{"target":"stdin","content":"At initialization read every non-empty '<HH:MM:SS> <numeric value>' line from stdin; skip malformed lines."}],
      "model_init_args":[
        {"name":"name","type":"str","structure":"Model instance name"},
        {"name":"parent","type":"object","structure":"Framework parent reference or None"},
        {"name":"simulation_time","type":"float","structure":"Parent init arg controlling the last scheduled value"}
      ],
      "input_ports":[],
      "output_ports":[{"name":"value_out","type":"float","structure":"Selected scheduled value", "protocol":{"initial_signal":"at t=1","description":"Send one selected value at each integer time through int(simulation_time)."}}],
      "related_requirement_ids":[]
    },
    {
      "class_name":"StateProcess","model_type":"atomic",
      "function":"On each scheduled value, immediately advance the state once using the retained prior state, retain the new state for the next input, and write exactly one observation for the current simulation time.",
      "external_io":[{"target":"stdout","content":"For each received value write exactly one JSONL observation derived from the new state at that simulation time."}],
      "model_init_args":[
        {"name":"name","type":"str","structure":"Model instance name"},
        {"name":"parent","type":"object","structure":"Framework parent reference or None"}
      ],
      "input_ports":[{"name":"value_in","type":"float","structure":"Selected scheduled value", "protocol":{"initial_signal":"at t=1","description":"Receive one value from ScheduleSource at every observation time."}}],
      "output_ports":[],
      "related_requirement_ids":[]
    }
  ],
  "coupling_rules":[
    "Connect ScheduleSource.value_out to StateProcess.value_in."
  ]
}
</StageExample>
"""


INHERITED_COUPLED_EXAMPLE = r"""
<StageExample>
This is a complete INHERITED COUPLED response. It does not repeat the current
module's locked class name, constructor, ports, or external_io.
Assume its locked contract contains request_in, result_out, and worker_count.
The empty requirement-ID lists are specific to this example, which has no live
ledger. In a real response, use the relevant IDs shown in the current prompt.
{
      "function":"Broadcast every request to every member of a runtime-sized worker family.",
  "children_plans":[
    {
      "class_name":"Dispatcher","model_type":"atomic","function":"Replicate each request to every worker; this example intentionally broadcasts rather than selecting one recipient.","external_io":[],
      "model_init_args":[{"name":"name","type":"str","structure":"Model instance name"},{"name":"parent","type":"object","structure":"Framework parent reference or None"}],
      "input_ports":[{"name":"request_in","type":"dict","structure":"{'request_id': int}","protocol":{"initial_signal":"None","description":"Receive a request from the current boundary."}}],
      "output_ports":[{"name":"job","type":"dict","structure":"{'request_id': int}","protocol":{"initial_signal":"None","description":"Broadcast each request because every worker is an intended consumer."}}],
      "related_requirement_ids":[]
    },
    {
      "class_name":"Worker","model_type":"atomic","function":"Process one dispatched request.","external_io":[],
      "model_init_args":[{"name":"name","type":"str","structure":"Model instance name"},{"name":"parent","type":"object","structure":"Framework parent reference or None"},{"name":"worker_id","type":"int","structure":"Runtime worker index"}],
      "input_ports":[{"name":"job","type":"dict","structure":"{'request_id': int}","protocol":{"initial_signal":"None","description":"Receive a dispatched request."}}],
      "output_ports":[{"name":"result_out","type":"dict","structure":"{'request_id': int, 'worker_id': int}","protocol":{"initial_signal":"None","description":"Send one result after processing."}}],
      "related_requirement_ids":[]
    }
  ],
  "coupling_rules":[
    "Forward parent.request_in to Dispatcher.request_in.",
    "Broadcast Dispatcher.job to every Worker.job instance created from worker_count; every worker intentionally receives every request.",
    "Forward every Worker.result_out to parent.result_out."
  ],
  "interface_change_requests":[]
}
</StageExample>
"""


ROOT_ATOMIC_EXAMPLE = r"""
<StageExample>
This is a complete ROOT ATOMIC response, CLI-configured with no stdin. The entry runner parses CLI values and supplies the constructor; the atomic model writes
one final record after advancing its tightly coupled state.
{
  "class_name":"PopulationModel","model_type":"atomic",
  "function":"Initialize the complete population state from constructor parameters, advance it at positive fixed time steps strictly before simulation_time, and write exactly one final JSON object with time=simulation_time. Keep all mutually dependent state variables in this one model and produce no intermediate records.",
  "external_io":[{"target":"stdout","content":"At the end of simulation write exactly one JSON object. Format: {'time': float, 'active': float, 'inactive': float}. Values come from the final internal state."}],
  "model_init_args":[{"name":"name","type":"str","structure":"Model instance name"},{"name":"parent","type":"object","structure":"Framework parent reference or None"},{"name":"step","type":"float","structure":"Positive simulation step supplied by the runner from CLI"},{"name":"simulation_time","type":"float","structure":"Final reporting horizon supplied by the runner from CLI"},{"name":"total_population","type":"int","structure":"Initial population supplied by the runner from CLI"}],
  "input_ports":[],"output_ports":[],
  "implementation_example":"final_periodic_state"
}
</StageExample>
"""


INHERITED_ATOMIC_EXAMPLE = r"""
<StageExample>
These are complete INHERITED ATOMIC responses. The locked interface is not
repeated. Choose the reference by its complete behavior: a configured service
delay needs a timed-service pattern, while an input-driven state update does
not need an independent timer. The behaviors inside these examples belong only
to those examples; never copy a delay, state update, equation, or external
record that is absent from the current locked contract and requirements.

Delayed service:
{
  "function":"On each job, retain it for the configured service time, emit one completion, and become idle; define simultaneous-arrival behavior explicitly.",
  "implementation_example":"single_inflight_timed_service",
  "interface_change_requests":[]
}

Input-driven state update and external record:
{
  "function":"On each received measurement x, immediately update retained state using the request's exact equations and constants. For example, if the requirement states loss = 0.2 * (previous - min(x, previous)), explicitly preserve that equation and state whether a prior decision affects the current update. Write exactly one JSONL observation from the new state at the current simulation time. Wait passively between inputs; do not create an independent timer.",
  "implementation_example":"jsonl_stateful_input_processor",
  "interface_change_requests":[]
}
</StageExample>
"""

ATOMIC_INSTRUCTION = """
<TaskInstruction>
The parent-issued interface in `<LockedInheritedContract>` is immutable. Generate
only the missing behavioral expansion for this ATOMIC module.

[Generate these fields]
- `function`: a precise behavioral contract covering responsibility, observable
  behavior, simulation clock, startup, input handling, output timing, state that
  must be remembered, termination, and valid edge cases. Describe behavior, not
  callback names or scheduling APIs.
- `implementation_example`: select exactly one tested complete-file example
  from `<AtomicImplementationExampleCatalog>`. Match the example's complete
  behavior to this model; do not choose by a shared word in its name alone.
- `interface_change_requests`: normally empty. If the required behavior is
  impossible with the locked constructor/ports/external-IO assignment, describe
  the exact deficiency here. Compare the proposed change with the locked JSON
  first: never request an argument, port, or external-IO stream that is already
  present. Never silently rewrite the inherited interface.

Example: if `result_out` already sends results to another model, but the same
atomic model must also write a `job_completed` record to stdout and its locked
`external_io` is empty, request `external_io`, not a `job_completed` port,
because no model consumes the record.

Do NOT output class_name, model_type, model_init_args, input_ports, output_ports,
or external_io. The orchestrator copies those fields mechanically from the
locked contract.
</TaskInstruction>
"""


ROOT_ATOMIC_INSTRUCTION = """
<TaskInstruction>
Generate the complete plan for this ROOT ATOMIC model. Define its exact class
identity, constructor, DEVS ports, direct external IO, and behavioral contract.

- `function`: specify observable behavior, simulation clock, startup, input
  handling, output timing, remembered state, termination, and edge cases.
- `implementation_example`: select exactly one tested complete-file example
  from `<AtomicImplementationExampleCatalog>` whose complete behavior is the
  closest implementation pattern for this model.
- Keep the root interface minimal, but retain every explicit configuration value
  required by the behavior.
- Because this is atomic, it directly performs any required OS IO and has
  no children or coupling specification.
</TaskInstruction>
"""


INHERITED_COUPLED_INSTRUCTION = """
<TaskInstruction>
The current coupled module's constructor and boundary ports in
`<LockedInheritedContract>` are immutable. Expand only its missing subsystem
details and design canonical interfaces for its direct children.

[Generate these fields]
- `function`: the capability achieved by the subtree. The coupled wrapper itself
  remains a pure structural container.
- `children_plans`: one canonical simple plan for every direct child named in the
  global plan. These constructors and ports become each child's locked interface
  when that child is expanded later.
- `coupling_rules`: one complete natural-language routing rule per item, using
  the locked current boundary and the newly generated child interfaces.
- `interface_change_requests`: normally empty. Use it only if the locked current
  boundary makes the required decomposition impossible. Do not rewrite that
  boundary in this response.

The `external_io` in the locked simple plan lists OS operations delegated into
this subtree. Pass each operation to one child subtree at this level until a
leaf atomic model directly performs it. The coupled class itself performs none.

[Child interface design]
- Return exactly one `children_plans` entry for every direct child named in
  TargetContext, with its model type taken from GlobalPlanOverview.
- Define each child's constructor and all ports needed to connect to a current
  boundary port or communicate with a sibling. Preserve configuration needed by
  deeper descendants and any required runtime multiplicity.
- Make corresponding port protocols consistent. Assign each delegated external
  IO stream to exactly one child subtree; do not copy it to several siblings.
- If one final aggregate record is explicitly required, select one leaf atomic
  model to write it and route the needed data to that subtree through DEVS ports.
- Populate each child's `related_requirement_ids` from the current subtree's
  ledger slice. Include all items relevant to that child's concrete interface,
  behavior, external IO, shared schemas, timing, and parameters. IDs may be
  shared by cooperating siblings; refine rather than blindly copy the coarse
  global-plan associations.

[Coupling design]
- Finalize the direct children's input and output ports before writing this
  list. Then build each route by copying endpoint names from the locked current
  boundary and those final child port lists; do not invent, rename, or reverse
  a port while writing `coupling_rules`.
- Write one complete routing rule per list item. Fixed and runtime-sized,
  indexed, conditional, one-to-many, and many-to-one rules are all allowed.
- List actual routes only; use an empty list instead of architectural commentary
  or notes about routes that do not exist.
- Use `parent` only for the current boundary; otherwise name the relevant direct
  child instance or family and ports clearly. Configuration travels through
  constructor arguments, not ports invented solely for configuration.
- `parent` means the current model's declared DEVS boundary ports, never an OS
  target such as stdin, stdout, or a file; a leaf atomic model performs those
  operations directly.
- For a fixed route, read source and target direction from the locked/current
  interfaces rather than inferring it from port names.
- Check every fixed endpoint against the interfaces in this response. Do not
  invent a self-route for logging; an external-IO sink is a target unless the
  requirements explicitly give it an outgoing route.
- A group rule must name its member set or family and a port declared by every
  member; do not use "all modules" if the set includes a sink or a child without
  that output.
- Include routes required by genuine startup messages and busy/ready feedback.
  Every feedback loop needs a waiting or termination condition.

Use the shared FieldContract below for every child field. Do NOT output a second
copy of the current module's model_init_args, input_ports, output_ports, or
external_io.
</TaskInstruction>
"""

FIELD_GUIDANCE = """
<FieldContract>
[model_init_args]
- `name` and `parent` are framework-reserved. Include each exactly once as the first two args. Use `name.type="str"`. Use `parent.type="object"` and describe it as the framework parent reference or None. The primitive-type restriction below applies to business arguments and port payloads, not the framework parent reference.
- For each additional arg, state its source: parent init arg, explicit scenario constant, or local derivation. Preserve values through intermediate coupled models when deeper descendants need them. Never replace a missing propagated value with a placeholder such as `0`, `None`, or an unrelated default.

[input_ports / output_ports]
- Ports carry messages between DEVS models. Do not create a `parent` boundary
  port solely to print or save a record. If the requirements explicitly call
  for a separate aggregation model, route records to it through DEVS ports and
  list the actual stdout/file call only in that leaf model's `external_io`.
- Allowed port and init-arg types: int, float, bool, str, dict, list.
- For dict/list values, use a strict Python representation:
    - BAD (Vague Summary): "Information about the sent packet including sequence number and retry flag."
    - BAD (Vague List): "A list of jobs."
    - GOOD (Strict Dict): "Packet info. Format: {'sequence_number': int, 'control_bit': str, 'is_retry': bool}."
    - GOOD (Strict List): "List of jobs. Format: [{'job_id': int, 'priority': float}]."
    - GOOD (Nested): "Format: {'metadata': {'timestamp': float, 'source': str}, 'payload': list[int]}."
- Every port protocol contains `initial_signal` and `description`.
- Keep `initial_signal` conservative. Describe only the startup interaction on this port:
    - For an input port: whether the model expects to receive a startup message.
    - For an output port: whether the model actively sends a startup message.
    - Use `None` when no startup interaction is required.
- Do not predefine startup payload content, invent placeholder/default messages, or create startup messages merely to initialize a port or satisfy a protocol field. Put payload schema in `structure` and runtime protocol details in `description`.
- If startup needs a handshake, choose exactly one sender. If a sender must wait for downstream availability, define explicit feedback such as `ready`, `available`, `ack`, or completion.

[external_io]
- Use `external_io` for OS/environment interactions: stdin, stdout, stderr, files, external services, printed records, and final reports. Use DEVS output ports when another DEVS model needs the data to continue simulation.
- `target` must be exactly one of `stdin`, `stdout`, `stderr`, `file`, or `other`. Put path, direction, mode, and format details in `content`, not in `target`.
- `content` must state direction, exact schema or format, source or derivation logic, timing, multiplicity, and any resource/path details.
- Treat concrete values in a user-provided example record as illustrations
  unless the requirements explicitly declare them fixed constants. Preserve
  field names and types while stating how runtime values are derived.
- Do not claim that one child writes records produced only by a sibling unless
  those records are explicitly routed to it through DEVS ports.
- In the current model's detailed_plan, list only OS operations performed by
  that model. In children_plans, an atomic child lists operations it performs;
  a coupled child lists operations that must be passed farther down its subtree.
- For ordinary event output, the model that changes state writes its state
  record and the model that sends a DEVS message writes that message record.
  Emit each required record once. A separate writer is appropriate only when
  the requirements explicitly demand aggregation or a combined final record.

[behavior details]
- If a valid input range includes arithmetic edge cases such as a zero denominator, empty collection, zero duration, or missing optional record, define the exact behavior instead of leaving it implicit.
- State one simulation clock unit for each subsystem. If an external record uses a different unit, describe the exact conversion once. Do not apply a second conversion when the DEVS clock already uses the target unit.

[implementation_example]
- This field appears only in an atomic detailed plan. Select exactly one entry
  from AtomicImplementationExampleCatalog by comparing the full behavior:
  startup, timing, input consumption, buffering/state, output, and external IO.
- The selected file is an implementation pattern, not an extra requirement.
  Adapt its class, interface, payloads, and equations to the locked contract.

[related_requirement_ids]
- This field appears only on child simple plans. List valid IDs from
  RelevantRequirementsForCurrentSubtree that the child needs for its planning
  or implementation. It records relevance; it does not assign an OS operation.
</FieldContract>
"""
