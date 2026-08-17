# How the Assistant works — the target design

Written to be read without prior knowledge of OptPilot; every term is defined
where it first appears.

**What is described here.** The architecture (§§1–2), today's tool surface
(§3), and the approval gates it runs under exist. The rest — the task loop
(§4), the knowledge model (§5), standing grants and the audit trail (§6) — is
a target design, not built; it defines what would make the Assistant able to
carry a real task ("generate a simulator of my clinic, then find a staffing
policy that minimizes waiting time") from the first sentence to a result the
person can trust, without the person steering every step. Anything already
built is marked as such. Dated 2026-08-16.

## 1. What the Assistant is

OptPilot runs optimisation experiments. **Packages** hold **environments**
(code that scores a proposed solution), **methods** (code that proposes
solutions), **resources** (supporting tools — for example a simulator
generator — that declare typed **actions**: named operations with declared
inputs), and **run setups** (a ready pairing of one environment with one
method, plus an objective and a budget, launchable with typed per-launch
inputs). Executing a run setup is a **run**; runs execute in containers, take
minutes to hours, and leave a permanent evidence record. The **catalog** is
the set of packages on this machine, and **Studio** is the local web
application for all of it.

The Assistant is a conversation inside Studio, built from two halves that
never blur:

- **The reasoning engine** — a language-model agent runtime (OpenHands),
  running as a separate local process — decides what to do next and writes
  the words the person reads.
- **The hands are Studio's.** Every action is a named **tool** that Studio
  executes on the engine's behalf under Studio's own permission checks, with
  the result recorded in the conversation. If a tool does not exist, the
  action cannot happen. The tool list *is* the Assistant's capability
  surface.

### The design goal

**A person should be able to describe the result they want, walk away, and
come back to find real work done toward it.**

Each part of that sentence rules something out, so it is worth spelling out
in full.

*Describe the result they want* — the person says what they want to be true
("find a staffing policy that keeps average patient waiting under ten
minutes"), not the sequence of steps that would achieve it ("generate a
simulator, register it as an environment, pair it with the policy-search
method, launch with these inputs"). Requiring the sequence means the person
must already know which pieces exist, which fit together, and in what order —
which is the knowledge they came to the Assistant to avoid needing.

*Walk away* — the person is not sitting there watching. This is the demanding
part, because runs take minutes to hours: they will close the laptop, go to
lunch, come back tomorrow. Three consequences follow, and most of this
document exists to handle them. The Assistant must have been given enough
authority *before* the person left, or the work stalls overnight on an
unclicked approval. The goal must survive Studio being restarted, because a
goal that lives only in the language model's memory is gone by morning. And a
finished run must reach the conversation by itself, because nobody is there
to notice it finished.

*Come back to find real work done* — what they see on return. Not a spinner.
Not "waiting for your approval since 9pm." Not even a finished run that
nothing has looked at. It means one of exactly four things, and §4 turns this
list into the rules the Assistant follows when a run finishes: the goal was
met, and here is the result; the first attempt fell short, so here is the
second, already running; it stopped for a reason the Assistant can name, with
what was learned; or the next move is genuinely the person's call, and here
are the findings they need to make it.

**What this deliberately does not mean.** It is not a promise of unattended
autonomy. The person's involvement is not removed; it is *moved* — gathered
into a few decisions they can judge well (approve this plan, this spending,
this publication) instead of scattered across ten mechanical confirmations
they will click through without reading. The autonomy budget (§4), the
counted grants (§6), and the list of acts that are never tools (§6) exist so
that coming back to work done never means coming back to a surprise.

This goal is also a fair description of what is broken today, which is why it
leads. In a walkthrough on 2026-08-16, four runs displayed a live "running"
badge; one had been silent for 135 hours while still reporting that the
method "may still be preparing another Candidate." That person did not come
back to progress. They came back to a false statement.

Everything below serves this goal: the task loop (§4) makes a goal survive
hours and restarts, the knowledge model (§5) makes the first attempt land on
the right pieces, and the authority model (§6) makes a ten-step task cost one
decision instead of ten interruptions.

## 2. How one exchange flows *(built)*

1. The person types a message on any Studio page.
2. Studio wraps it with a **context packet**: the open page, the selected run
   or catalog item, attached workspaces, a catalog summary — so the Assistant
   answers about what the person is looking at.
3. The agent server receives the message, the packet, the **guidance file**
   (a system prompt telling the model how OptPilot wants it to behave), and
   the tool list with parameters.
4. The model replies with words, tool calls, or both. Studio executes each
   call — asking the person for approval when the tool's permission requires
   it — and returns results, repeating until a plain final message.

Two operational facts. The agent server caches the tool list per process and
must be restarted after the tools change. And when the agent runtime is not
running, messages queue locally — honest, but the composer must say plainly
that the Assistant is off and how to start it *(the plain off-state notice is
not built yet)*.

## 3. The tools, by verb

A test enforces two honesty rules: every tool advertised to the model is
executable (an advertised-but-dead tool hangs the conversation), and the
guidance file teaches only tools that exist. *(Built.)* Registration-time
validation of playbooks (§5) extends the same idea to package-declared
knowledge: a package may only teach steps whose tools exist.

| Verb area | Tools | Status |
| --- | --- | --- |
| Conversation | `optpilot_conversation_title` | built |
| Discovery | `optpilot_catalog_list/detail`, `optpilot_docs_search`, `optpilot_capability_list/detail` | built; slim listing + package docs in §5 |
| Pairing | `optpilot_compatibility_check` | built |
| Workspaces | `optpilot_workspace_list/create/attach/detach/focus` | built; §5 adds the edit-copy front door |
| Files & code | `optpilot_file_tree/read/write/diff`, `optpilot_file_editor`, `optpilot_shell_run`, `optpilot_terminal`, `optpilot_workspace_preview_open` | built |
| Configuration | `optpilot_config_discover/validate` | built |
| Registration | `optpilot_package_plan_prepare/update/validate/smoke/apply` | built, incl. the image-placement question |
| Run setups | `optpilot_study_draft/save/launch` | built |
| Runs | `optpilot_run_list/detail/compare`, `optpilot_job_stop` | built |
| Testing | `optpilot_smoke_test_study` | built |
| Resource actions | `optpilot_resource_action_list/run/status` | **not built** (§5) |
| Task loop | `optpilot_task_update`, `optpilot_task_plan_propose` | **not built** (§§4, 6) |
| Package updating | catalog edit-copy front door | **not built** (§5) |
| Trust visibility | image-approvals listing | **not built** (§6) |

## 4. Carrying a task to completion *(target)*

The hard part of "actually solving tasks" is not any single tool call; it is
that a real task spans several calls, an approval, a two-hour run, possibly a
Studio restart, and a person who walked away. Five mechanisms make the goal
durable and the loop close.

**The conversation owns the task.** A standing goal is one bounded JSON file
in the conversation's own storage — beside the transcript and approvals that
already survive every restart — holding the goal in one sentence, the success
criterion (metric, direction, target), an ordered step list, and a status.
Steps carry references to real platform objects only (workspace, plan,
catalog entry, launch, run), never paths or prose, so every reference can be
re-resolved through existing readers after any restart. The conversation *is*
the task's identity: its title is the goal's name; archiving it abandons the
task. Deliberately **not** a new entity in the permanent record: a goal is
conversation state, not reproducibility evidence — and deliberately **not** a
workflow engine that executes the steps, because between every stage sits a
judgment call (is the generated simulator right? is 4.5 minutes of waiting
good enough?), which is exactly the reasoning engine's job.

**The model owns intent; Studio owns facts.** The model maintains the task
file through a free, ungated tool (updating conversation-private state costs
no approval). But Studio does not trust the model's bookkeeping: at the
success return of every side-effectful tool, Studio itself stamps the
resulting identifiers — the launch and run ids, the registered entry, the
action run — into the matching step. The resumable record is complete even on
the model's most forgetful day. Two details make this survive a crash between
the side effect and the stamp. First, the recovery source is the approvals
record, not the event log: launches are approval-gated, and on that path the
full tool result (carrying the launch) is stored with the approval, while the
event carries only an outcome flag. Second, only genuinely long-running acts
become steps worth settling — launches and resource actions. A smoke test
completes inside its own tool call, so it has nothing to settle later.

**The success criterion is typed, so Studio can check it.** "Minimize mean
waiting time, target 5.0" is a metric, a direction, and a number — not a
sentence. This matters beyond tidiness: the loop's most important stop
condition is "the goal is met, report instead of spending more", and Studio
can only enforce that mechanically by comparing the run's best metric against
a typed target. A prose goal would push that stop back onto model restraint,
which is exactly what the autonomy budget exists to avoid relying on. Prose
intent is still welcome alongside it, as a note the person reads.

**Watches, and three ways to notice.** When a launch succeeds, Studio records
a watch: this conversation cares about that run. Terminal detection has to be
belt-and-braces, and the reason is worth stating because the obvious design
is wrong. Studio's per-run execution thread does check for terminal state and
would be the natural single hook — but that thread also has silent exits (a
summary read that raises, the runtime closing mid-flight, the outer catch-all),
and a run that ends across one of those exits would never be noticed by a
long-lived Studio process. So the design uses three observation points, all
firing the same idempotent settlement: the execution thread's terminal branch
(live, the common case), startup reconciliation (for runs that ended while
Studio was down), and a low-frequency sweep of active watches on the delivery
thread (for the silent-exit gap). The sweep reads records only — it never
wakes the model — so it costs nothing when nothing finished.

**Completion wakes the conversation through the front door.** A fired watch
appends an item to the conversation's agenda; a dispatcher delivers it as a
system-authored turn through the exact same path a human message takes — same
tool loop, same approval gates, same transcript. The turn is labeled as
system-originated in the timeline, and it is included when the agent server's
evaporated state has to be rebuilt, so a recreated conversation knows what
woke it. Agenda items never interrupt: they wait until the conversation is
idle with no approval pending, and a person's message always outranks them.

**What the woken Assistant does** is written in the guidance file as
prescriptively as its opening moves: read the run's results, compare them
against the task's success criterion, then take exactly one of four exits.
These are the four things §1 promises a returning person can find:

- ***Report and finish*** — the criterion is met. Summarize the result and
  mark the task done.
- ***Iterate*** — the criterion is not met, but a clearly better next attempt
  exists. Propose it, and run it if existing authority covers it (§6);
  otherwise ask.
- ***Stop*** — the run failed for a reason the Assistant can name. Explain
  it, with the remedy if the failure carries one (§5).
- ***Park*** — the outcome is ambiguous, or the next move is genuinely the
  person's call. State the findings and mark the task as waiting on them.

An **autonomy budget** on the task — a set number of unattended turns and
launches — caps how far this loop can run with nobody watching; using it up
parks the task. Iterating unsupervised spends money on paid model calls, so
the budget is a number the owner sets and can see, never a judgment the model
makes for itself.

## 5. What the Assistant knows *(target)*

Four layers, from cheapest to richest, each with a validation story.

**A slim catalog view.** The listing tool returns, per entry: id, kind, a
short description, tags, the task slugs below, and a compact per-kind summary
— not the full settings document it returns today (tens of kilobytes into the
first exchange). Full detail stays one call away on the detail tool. The
ladder is: counts in every context packet → slim list on demand → full detail
for a shortlisted entry.

**A task vocabulary.** Components and run setups gain an optional `tasks`
list of verb-object slugs — `generate-simulator`, `optimize-policy`,
`solve-or-problem`, `tune-parameters`, `evaluate-design` — validated for
shape at registration, with a core-maintained synonym table ("staffing",
"scheduling", "improve" → `optimize-policy`). One declared field then drives
catalog search expansion, the welcome page's suggested actions, and playbook
matching identically — so "find a staffing policy that minimizes waiting"
lands on the right entries on the first attempt, which today it does not.

**Package playbooks.** This revises an earlier position honestly. The
previous version of this document argued for no skill system, on the theory
that a package's walkthrough teaches the Assistant its recipe through
documentation search. That theory was factually broken — documentation search
never covered package trees — and prose can neither be validated against the
entries it names nor found at the exact moment of need. The revised position
keeps the original rejection's *structure* (no separate registry, no second
lifecycle) while fixing its conclusion: **a playbook is package content**, a
fifth settings kind beside environments, methods, resources, and run setups.
It declares: which task slugs it serves, one sentence of when it applies,
which entries outside its package it requires, and an ordered list of
advisory steps — each either something to tell the person or a reference to a
tool-backed action (run this resource action, register the output, pair with
that method, launch). Registration validates it like everything else: schema,
every in-package reference must resolve, cross-package references must be
declared, and — the registration-time twin of the tool-drift test — every
step's action kind must map to a tool the Assistant actually has. Steps are
deliberately advisory: no conditionals, no loops, no auto-execution. The
reasoning model is the sequencer; the person still approves every gated step.
This is where "DEVS-Gen output can feed the policy search" lives as validated,
retrievable knowledge instead of tribal prose.

**Typed bridges and remedies.** Two smaller contracts complete the layer.
Resource actions may declare what they produce (`produces: {kind,
contract}`, e.g. the simulator generator produces a bundle satisfying the
`devs.simulation.v2` contract) — so the Assistant can assert *before running
anything* that the output will be registerable as an environment, and the
existing compatibility check answers the rest after registration. No
inference engine joins these edges automatically; the playbook carries the
sequencing. And every coded refusal the Assistant can encounter carries its
fix as data — a `remedy` object with a closed set of kinds: *a command the
person must run* (image approval, with the actual reference filled in), *a
set of missing inputs* (with their declarations, ready to collect), *a
settings location to edit* (for validation failures), *the right tool to use
instead* (the canonical-catalog-root refusal answers with the edit-copy
tool), or *an approval to wait for*. The guidance file stops hand-teaching
remediations; refusals teach themselves.

**Resource actions as tools.** Three tools mirroring the launch pattern:
list an action's declared inputs and grants; run it approval-gated (the card
shows the validated input values — the person approves the exact generate
request that will run); poll its status. One deliberate choice closes the
composition loop: the Assistant's action runs root their output inside an
attached workspace, so a generated simulator is immediately readable,
editable, and feedable to registration without a copy step. Docs search also
gains the published package trees, so a package's README and walkthroughs are
retrievable by the tool that already exists.

## 6. Authority: grants, plans, and the audit trail *(target)*

Today five permission domains — file writes, shell, registration, launches,
stops — each default to asking the person per action *(built)*. That is the
right floor and the wrong ceiling: a ten-step task should not cost ten
interruptions, and the fix must not be a bypass.

**Standing grants.** An approval card may offer, beside "Approve once", one
wider choice computed by Studio from the card's own concrete targets: *"…and
allow safe shell commands in this workspace for this conversation"*, *"…and
allow up to N more launches of this run setup (at most M trials each)"*.
Accepting mints a **grant**: a small record in the conversation's storage
with a typed scope, a use count, an expiry, and a link to the approval that
created it. The permission gate consults grants at its single existing choke
point: a matching call proceeds, decrements the count, and writes a
consumption event into the transcript exactly where the card would have
appeared. Everything else still asks.

**What a grant matches on is the design's load-bearing detail.** The gate
sees the arguments the *model* wrote — a run-setup reference it typed, a path
it chose. Matching a grant against those would let the model widen its own
authority by writing the right string. So a grant never matches model text:
each grantable call site hands the gate the identities *Studio itself
resolved* — the catalog revision it looked up, the compiled trial budget, the
workspace root it validated — and matching happens only on those. The
consumption check also runs after the existing rejection of approval-bypass
fields (a call trying to smuggle one is still refused, never grant-consumed),
under the same per-conversation lock approvals use, with the event log as the
one authoritative record of consumption and the grant file holding only
counters.

The remaining invariants: **the reasoning engine can never mint, extend, or
reference a grant** (grant-shaped fields in tool arguments are rejected the
same way approval-bypass fields are); the Studio-wide permission setting is
the outer ceiling (a domain set to "disabled" beats any grant instantly, and
grants are only offered where the setting allows them); grants die with the
conversation, on expiry, on exhaustion, or on one click in a visible "active
grants" strip. They deliberately survive a restart, as approvals already do —
a count-bounded, expiring, click-minted record is not made safer by
forgetting it.

Scopes are typed per domain, not a policy language. **Shell** grants cover
only commands the existing safety classifier already deems safe, in one named
workspace, time-bounded — the risky list still produces a card every time.
**Launch** grants are count-bounded to one run setup with a trial ceiling
(for a catalog run setup the scope pins the exact saved revision; for a
workspace file it pins the path, which is edited in place by nature).
**Smoke tests get their own permission domain**, split off from launches:
today they borrow the launch gate, yet a smoke test is bounded by
construction — throwaway record, clamped timeout, forced trial cap — and the
write → validate → smoke → fix loop is precisely where the ten interruptions
land. This split is the single largest ergonomic win in the design.
**Stopping** a run the conversation started is offered as a default-checked
line on that launch's own card, minting a one-use grant scoped to the launch
Studio just created — never derived implicitly, because "the Assistant may
stop this" deserves the person's eyes exactly once, on the card that started
it. **Registration is never grantable** — every publication asks, always.

**Plan approval is a grant-minting card, not a second executor.** The model
may propose a plan: a typed step list where each step's scope *is* a grant
shape, with cost lines where money is involved. Studio renders it as one
approval card showing exactly the authority it would mint; approving mints
the grant bundle atomically; execution then proceeds through the completely
unchanged tool loop, consuming grants. A step executed with different
arguments matches no grant and falls back to an ordinary card labeled
"outside the approved plan" — deviation can only *add* friction, never widen
authority. Steps that are never grantable appear on the card as "will ask
again at this step".

**What is never a tool.** Confirmed and extended: run deletion (a person's
typed act at a terminal); container-image approval (an operator trust
decision with durable-versus-session semantics a conversation click would
flatten); permission and grant mutation (grants are minted and revoked only
by human clicks on Studio's own surfaces); secret and host-variable
management; and driving a package's browser console. Each exclusion gets a
read-only complement so the Assistant can *show* what it cannot change — the
image-approvals listing, and the conversation's active grants in the context
packet, so the model knows "two launches left" instead of guessing.

**Money is visible before it is spent.** Three layers, cheapest first: a
spend marker derived from declarations Studio already has (a method that
requests a known paid credential gets a card line: "calls a paid model;
budget max 25 exchanges"); an optional author-declared per-exchange cost
estimate, labeled as the author's claim; and method-reported actual usage
carried in the exchange envelope into the run's permanent record, summed on
the run page. A launch grant over a spend-marked method is only mintable with
an explicit launch count, and its card multiplies out the worst case.

**The audit walks backward.** Every side effect the Assistant causes carries
an initiator stamp — conversation, and the approval or grant (with its
minting approval) that authorized it — recorded in Studio's durable launch
and registration records (not in the run's reproducibility evidence, which
describes what ran, not who asked). From any run or published version, a
person can walk to the conversation, to the exact card a human clicked, to
what that card displayed. Grant lifecycle events (created, consumed, revoked,
exhausted) live in the conversation's event stream, and a decisions view
projects them across conversations — "everything the Assistant did this
week" — from records that already exist.

## 7. The flagship conversation, end to end

How the pieces compose. *"Generate a discrete-event simulator of my clinic,
then find a staffing policy that minimizes waiting time."*

1. **Intent → plan.** The task vocabulary maps the sentence to
   `generate-simulator` and `optimize-policy`; the devs-gallery playbook for
   that pair surfaces, naming the generator's action and the policy-search
   method it requires. The Assistant writes the task file (goal, success
   criterion: minimize mean waiting) and proposes a plan card: run the
   generate action with this clinic description (cost line: none), register
   the output, pair and launch the policy search (cost line: paid model,
   max 25 exchanges × N launches). One approval mints the grants.
2. **Generate.** The action runs with its output rooted in the conversation's
   workspace; its declared `produces` contract tells the Assistant the bundle
   is registerable before it looks. Studio stamps the action run into the
   task file.
3. **Register.** The plan tools compose the environment from the bundle;
   Check validates; the publication step asks — always. If software was
   installed meanwhile, the placement question rides the same card.
4. **Pair and launch.** The compatibility check (fixed to recognize policy
   validation) confirms the pairing; the launch consumes a grant; Studio
   stamps the run id and records the watch.
5. **The person leaves.** Two hours later the run ends; the watch fires; the
   agenda wakes the conversation; the Assistant reads results, compares
   against the criterion, and either reports success, spends its remaining
   granted launch on a refined attempt, or parks with findings. Either way,
   the person returns to a conversation that moved — with every action
   traceable to the card they clicked before they left.

## 8. Build order

1. **Resource-action tools** with workspace-rooted output — unlocks the
   flagship's first leg; designed against the grant model from day one.
2. **Remedy contract** on the refusals that exist today (image trust, missing
   inputs, validation failures) — cheapest large win for conversation flow.
3. **Slim listing + task vocabulary + docs over packages** — first-attempt
   discovery.
4. **Split the smoke-test permission domain from launches** — smallest change
   with the largest measured friction relief; ship it before the grant
   machinery. Then **standing grants** (server-resolved matching from day
   one) and **plan approval** as the card that mints them.
5. **Task file + Studio ref-stamping + run watches + agenda turns + the
   completion playbook** — the loop that makes runs come back to the
   conversation.
6. **Playbooks** as the fifth settings kind, with the flagship pair
   (devs-gallery × policy search) as the first two shipped.
7. **Initiator stamps + the decisions view**; spend actuals in the exchange
   envelope.
8. Inherited prerequisites tracked in the release review: the pairing-check
   fix, the bundled-package launch story, the edit-copy front door with the
   canonical-root refusal.

**How this design was checked.** Each part was drafted independently and then
read back against the code by a reviewer whose job was to disprove it. That
pass corrected three things now folded in above: the run-completion hook
needed a third observation point (the execution thread has silent exits, so
two seams do not in fact catch every ending); crash recovery reads the
approvals record rather than the event log (the event carries only an
outcome flag on the approval path); and grant matching must use
Studio-resolved identities rather than the model's own arguments, or the
model could widen its authority by writing the right string. The knowledge
model (§5) has had one such adversarial pass fewer than the other two
sections; treat its seam claims as the least verified part of this document.
