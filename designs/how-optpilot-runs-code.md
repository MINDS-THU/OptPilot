# How OptPilot runs code — the target design

Written to be read without prior knowledge of OptPilot; every term is defined
where it first appears.

**What is described here.** Packages, the catalog, the archive and fingerprints
(§§1–3) exist today. The execution model — everything running in a container,
images named by fingerprint, and registration capturing an image (§§4–8) — is a
target design, not built. Anything else not yet built is marked where it
appears. Dated 2026-08-13.

## 1. What OptPilot does, and what it promises

OptPilot runs optimisation experiments. You give it something that proposes
candidate solutions and something that scores them, and it runs the two against
each other under an objective and a budget.

Its distinguishing promise concerns what happens *afterwards*. Every execution
writes a permanent copy of everything involved — the code, the settings, every
proposal, every score, the ordered sequence of events — into a private
**archive** on your machine. Nothing in it is ever modified. A record can be
deliberately deleted to reclaim space (§11), which leaves behind a note saying a
record existed and was removed — so a deleted record is never mistaken for one
that never happened.

Every stored item gets a **fingerprint**: a code computed from its contents,
such that changing a single byte changes the code completely, and anyone can
recompute it to verify. Fingerprints are how one stored item refers to another
unambiguously.

Together these support a claim: *this exact code, with these exact libraries,
produced this result* — and a third party can check it, because the record names
each ingredient by fingerprint and the ingredients themselves are kept.

## 2. The rule that governs execution

> **Anything whose output becomes part of the permanent record must identify
> its libraries by fingerprint, and that fingerprint must be part of the
> record.**

Two things produce such output: **environments**, which score proposed
solutions, and **methods**, which propose them.

The reasoning: if a library reaches the machine in a way OptPilot cannot
describe — someone installing it by hand — then the record describes an
execution while staying silent about something that may have determined its
result. It would be incomplete in a way that still looks complete, which is
worse than being obviously absent.

## 3. Packages and the catalog

A **package** is a folder holding related work. Inside it are:

- **Environments** — code that scores a proposed solution. A factory simulator,
  a scheduling model, a benchmark checker.
- **Methods** — code that proposes solutions. A genetic algorithm, a solver, a
  language model that writes policy code.
- **Resources** — supporting tools, such as a generator with a web interface.
- **Run setups** — a pairing of one environment with one method, plus an
  objective and a budget. (The settings files call these *studies*; this
  document says run setup throughout.) A run setup may also declare **inputs**
  whose values are supplied each time it is launched — a problem statement, a
  task identifier.

Executing a run setup is a **run**.

Each item is described by a settings file inside the folder, alongside the code
it refers to. A package may hold many of each.

The **catalog** is the set of packages available to you. Every entry is a folder
on disk: you can open it, read it, edit it, and put it in a source repository.
Some packages are **bundled** — they ship with OptPilot in its `catalog/`
folder. Two are used as examples below: `production_agv_scheduling`, a factory
and vehicle scheduling simulator with seven methods for it, and `or_solving`,
which solves an operations-research problem stated in plain language.

**A version is a snapshot.** Taking a version copies the folder's current
contents into the archive as an immutable, numbered record — a **snapshot**. The
folder stays editable; the snapshot never changes. This is the arrangement
source control uses: a working folder you edit, and an immutable history
captured from it.

Snapshots are cheap. Each records which files are present and each file's
fingerprint; contents already in the archive are reused. A snapshot of a 4 GB
package in which one file changed costs one file.

## 4. Containers and images

Code runs inside a **container**: an isolated environment created from an
**image**, which is a self-contained bundle holding an operating system, a
Python interpreter, and installed software. An image has a fingerprint.

Everything OptPilot executes runs this way: environments, methods, supporting
tools, **interactive views** (browser interfaces a package can offer for
inspecting or steering work), and the **workspace** — the editable project with
development tools around it that you build a package in, described in §5.

All of them resolve an image the same way — the component's own if it names
one, otherwise the package's, otherwise the default — and all pass the same
checks before starting (§6). An interactive view is long-lived rather than
per-piece-of-work: its port is reachable only from the machine it runs on, only
through OptPilot, and it is stopped when you close it or the run it belongs to
ends. Being interactive grants it nothing extra; it reaches the network or a
credential only by declaring so, exactly like anything else.

### How many images a package needs

**One image per distinct set of software the package requires — not one per
component.** Most packages need exactly one.

`production_agv_scheduling` shows why counting components is the wrong measure.
It holds fourteen of them — seven environments and seven methods — and needs
**one image**. Its seven environments are one simulator configured seven ways,
running the same evaluator code; its seven methods search that simulator in
different ways but need nothing beyond what OptPilot itself provides. Fourteen
images would be fourteen copies of the same thing.

`or_solving` shows when a second image is warranted. Its environment only checks
that a returned answer is well formed, so it needs nothing unusual, while its
method needs a native optimisation library, a solver program and a
language-model client. One image serving both would make the environment carry
gigabytes it never opens.

### The default image

A package declaring no image runs in the **default image**, which OptPilot
provides. It contains Python and the libraries OptPilot itself depends on.

This covers a package whose code uses only Python's standard library, and one
carrying pure-Python libraries inside its own folder — those travel with the
code and import directly. `production_agv_scheduling` carries its simulation
library this way.

The default image has a fingerprint like any other, so the record stays complete
without the author declaring anything. A run records the fingerprint of the
default image it actually used, so a later OptPilot release shipping a newer
default does not change what an existing run says it ran.

### A package's own image, and per-component overrides

Some software cannot travel inside a package folder as Python source:

- Libraries containing compiled machine code, such as `ortools`, `numpy` or
  PyTorch.
- Separate programs, such as the GLPK or IPOPT solvers.
- Other language runtimes, such as Java, R or Node.

A package needing any of these names an image, which every component in it uses
unless that component says otherwise:

```yaml
# or_solving package settings
runtime:
  container:
    image: ghcr.io/example/or-solving@sha256:<fingerprint>
    platform: linux/amd64
```

A single component may name a different one, using the same fields in its own
settings file:

```yaml
# coopa_solver method settings, inside that same package
runtime:
  container:
    image: ghcr.io/example/or-solving-solver@sha256:<fingerprint>
    platform: linux/amd64
```

This is the mechanism behind the `or_solving` split above: its method names an
image its environment does not use. An author may therefore set only the package
image and be done, or add an override where one earns its keep.

### What an image must provide

OptPilot supplies the command, the working directory and its own in-container
launcher, so an image is a place to run code rather than a self-starting
program. It must provide:

- A Python interpreter on the path as `python3`, at or above the minimum
  version OptPilot supports.
- A widely-used Linux base. OptPilot mounts its launcher and editing tools in
  as prebuilt binaries rather than installing them, and those binaries need a
  compatible system C library, so minimal distributions built on a different
  one are not usable.
- A writable temporary directory, and a default user that is not root.

Any start-up command the image declares is ignored. These requirements are
checked when an image is first named or captured, and again at approval, so a
mismatch is reported while the author can still fix it.

### Limits

Every container runs under a processor share, a memory cap, a wall-clock limit
per piece of work, and a cap on how much output it may produce. OptPilot
applies defaults; a component may raise them:

```yaml
runtime:
  container:
    image: ghcr.io/example/agv-scheduling@sha256:<fingerprint>
    platform: linux/amd64
    limits:
      memory: 8GiB
      timeoutSeconds: 900
```

A raised limit is shown next to the image when you approve it, because raising
one is part of what you are agreeing to.

### Architecture

`platform` names the architecture the image is for. A machine of a different
architecture stops the launch at the same point as a missing image. Emulation
is possible but must be asked for explicitly at launch, and the fact is
recorded, because an emulated run is roughly ten times slower and should never
be mistaken for a native one.

Where a reference covers several architectures, the run records the fingerprint
of the *one* that ran, not of the multi-architecture reference. Otherwise the
same recorded fingerprint would mean different executed bytes on different
machines.

An image must already be built when it is named, and is named by fingerprint.
OptPilot never builds an image while a run is in progress — a build fetches
software from the network, and what it fetches can differ between builds, so a
run's record could not state what it used. (Building an image *while authoring*
is different and is how images are made; see §5.)

Because references are by fingerprint, **tags are irrelevant to OptPilot**. A
package's settings contain a fingerprint and never a tag such as `latest`. Tags
exist for the convenience of whatever builds and pushes the image.

## 5. Building a package

**Write the code.** You work in a workspace: an editable project with
development tools around it, running in a container with network access so you
can install things and experiment freely.

**The workspace runs in the image the code will run in**, so if something works
while you are building it, it works in a run. Which image that is:

- *A package with no image yet* — the workspace starts from the default image.
  You install what the code needs as you go.
- *A package that has one* — the workspace starts from it, so everything the
  code depends on is already present and you can run and debug immediately.
- *A component with its own override* — editing that component uses its image
  rather than the package's.

OptPilot supplies the editing tools by **mounting** them into the container
from outside rather than adding them to the image. They are therefore never
part of the image's contents, so a package's image stays a description of what
its code needs and never contains an editor — and capturing (below) needs no
step to strip them out, because they were never there.

**Register each piece.** You declare "this code is an environment" (or a method,
or a resource), and which package it belongs to. Registering does four things
together:

1. Writes the code into that package's folder.
2. Writes its settings file.
3. Captures whatever you installed in the workspace as an image and records its
   fingerprint — as the package's image if the package has none, otherwise as an
   override on the component just registered.
4. Takes a snapshot of the package.

These belong together because splitting them lets you register code whose
software requirements are recorded nowhere. It would sit in the package looking
complete and fail when anyone ran it.

**Where a captured image goes.** Capture commits the stopped workspace
container into the container software's local image store on your machine and
records that store's fingerprint. The package's settings then name a local
image, which runs on your machine and nowhere else.

Making it shareable is a separate, explicit step: push the image to a
repository named in the package's settings, using whatever login the container
software already has — OptPilot never handles registry credentials — after
which the settings name `repository@<fingerprint>`. The launch check (§6)
accepts either form and compares it against the same form on the machine.

A package whose settings hold only local fingerprints is a package only its
author can run. That is the concrete meaning of sharing not being built yet
(§11).

**Detecting that nothing was installed.** If the author installed nothing, the
starting image's fingerprint is kept and no new image is produced.

Deciding this by looking at which files changed does not work: editing anything
leaves shell history, editor state, logs and caches behind, so the changed-file
list is never empty even when no software was installed.

The reliable signal is **what software is present**, not what files moved.
OptPilot records an inventory when the workspace starts and again at
registration: the installed Python packages with their versions, from every
interpreter environment on the image's path rather than only the default one,
and the installed system packages. Matching inventories mean nothing was
installed.

This is exact for software that arrived through a package manager, which is how
software normally arrives. It would miss a program placed by hand, such as a
binary downloaded straight into a system directory; catching that additionally
needs a file comparison narrowed to the few directories holding programs and
libraries, ignoring temporary files, caches and home directories. The inventory
comparison alone covers the ordinary case, and mounting the editor rather than
installing it (above) means the only thing that can change an inventory is the
author.

Producing a duplicate image would not waste much storage — images are stored as
layers, a registry stores each layer once, and an image capturing no changes
shares every layer with its starting image. The reason to detect it is clarity:
several fingerprints that are functionally identical, with nothing to say which
a package should name.

**Keeping a workspace aligned with its package.** A workspace records the image
it started from. Because a workspace starts from the package's image (above),
anything it captures is normally that image plus one more layer — cheap to store
and cheap to download, since the shared layers are already present.

That stops being true if the workspace's starting point drifts away from the
package's image, which happens two ways. Two workspaces opened before either has
registered both start from the standard image, so each captures its own
independent copy of whatever was installed in it. And a workspace left open
while another registers is building on a starting point the package has since
moved past.

Both are the same problem, and the cheap fix is preventative: when a package's
image changes while a workspace is open, the workspace says so and offers to
restart from the new one.

For what remains, the inventory comparison above is extended to consider the
package's current image as well as the workspace's starting image. If what you
ended up with matches what the package's image already holds, that image is used
and nothing is captured — even though the workspace began somewhere else. Two
people who installed the same thing in parallel therefore converge on one image
rather than two.

**Updating something already registered.** Registering again over the same
component replaces its files, re-runs the inventory comparison, and takes a new
snapshot. If software was added, the new image is recorded where the old one
was — as the package's image if that is what the component was using, or as its
override if it had one. Refreshing the package's image for every component that
shares it is the same action performed from a workspace that has no override.

Adding a second piece later writes different files into the same folder. Pieces
built at different times, in different workspaces, coexist because they occupy
different paths in one directory.

A run setup is created the same way, by naming an environment, a method, an
objective and a budget. It runs no code of its own, so it names no image.

**Share it.** Publishing the folder where others can obtain it, typically a
source repository. *Not built yet.*

## 6. Running a run setup

Four steps happen before any of your code executes. The checks come first, so a
failure leaves nothing behind.

**Obtain and check the images.** Each component's image — the package's or its
own override — must already be present on the machine. OptPilot does not fetch
one on its own: it reports which image is needed and how large it is, and you
fetch it, either through the container software directly or by accepting the
prompt. You then **approve** it for execution, which OptPilot remembers.

Approval covers the exact image fingerprint *together with the network and
credential grants declared against it*, because what you are agreeing to is an
exposure, not merely a set of bytes. Widening those grants, or changing the
image, asks again; narrowing them does not. Approvals are recorded per machine,
can be listed, and can be withdrawn — withdrawing does not stop a run already
under way but prevents the next container from starting. The default image that
ships with a release is approved already. A launch with nobody present to
answer, such as one in an automated build, uses an approval granted in advance
rather than being prompted. A missing engine, an image that is absent, unapproved, or
whose fingerprint does not match what the package names, stops the launch here.
Nothing has been written and no code has run.

**Network and credentials.** A container gets no outbound network and no
credentials unless the component asks for them. Each component declares, in its
own settings, the network access it needs and the named credentials it needs —
`or_solving`'s method declares both, because it calls a language model; its
environment declares neither.

Values are read from the launching user's settings and passed to the container
as environment variables when it starts, never on the command line where other
programs on the machine could read them. **The names of what was declared are
part of the run definition; the values are never written to the archive.** This
is the deliberate opposite of run-setup inputs, whose values *are* recorded,
because an input is part of the question being asked while a credential is not.

Granted network is ordinary outbound access. It carries no route to services
running on the host machine and none to other containers of the same run.

These grants are per component and never per package, because a component that
executes model-written code must be assumed capable of reading and sending
anything it has been given.

**Capture the code.** The package folder is snapshotted into the archive and
fingerprinted. Execution uses that snapshot, so editing the folder while a run
is in progress changes neither what runs nor what is recorded. A run setup may
pair an environment and a method that live in different packages; each is
snapshotted from its own package, and the run definition names both
fingerprints.

A launch snapshots the folder as it stands, whether or not you took a version
deliberately. If the contents are unchanged since the last snapshot, the
existing one is reused rather than duplicated.

**Write the run definition.** A record naming the environment and method code
fingerprints, the image fingerprints, the settings, the objective, the budget,
and the values supplied for any declared inputs. These combine into one
fingerprint identifying *what was run*. The same setup launched twice with
different input values produces two different fingerprints, because the inputs
are part of it.

**Execute.** Results accumulate into the record as the run proceeds (§9).

## 7. Where code physically runs

Three things come together at execution and stay separate:

| | What it is | Where it comes from |
| --- | --- | --- |
| Your code | The environment or method source | The archived snapshot, unpacked into a temporary folder |
| The software it needs | The component's image — the package's, or its override | Inside the image |
| The execution | The running program | A container |

OptPilot starts a container from that image and **makes the temporary folder
holding your code visible inside it** — the way plugging in an external drive
makes files visible to a program. The image supplies software; the folder
supplies your code.

**How long a container lives.** An environment gets a fresh container for each
proposed solution, so nothing one candidate leaves behind can affect how a later
one is scored.

A method gets **one container for the whole run**. It has to: a method
accumulates state between rounds — a population, a conversation history, the
position of a random number generator — and a fresh container each round would
discard it. The method's container is started once and receives each round of
proposals over a stream that stays open, answering on the same stream.

So a run starts one container per method, plus one per proposed solution. That
second number is what makes startup time matter (§10).

**How a container is invoked.** OptPilot supplies the command itself and
ignores any the image declares. The working directory is the mounted code
folder, and the component's code is placed on the interpreter's import path.
One request — the candidate and the settings — arrives as a single JSON
document, and one JSON document is expected back. Anything printed for a human
to read must go to the error stream, which is captured into the record; the
result stream carries only the response. Each exchange is bounded by the
component's time limit.

**What is mounted, and what comes back.** Exactly three things are visible
inside the container, and nothing else from the machine:

| Mount | Access |
| --- | --- |
| The unpacked snapshot of the component's code | read-only |
| An output directory, empty at the start of each piece of work | writable |
| A required capability provider's snapshot, when one is declared | read-only |

The archive, your home directory and every other path on the machine are not
mounted. The result of a piece of work is the JSON response together with
whatever was written into the output directory, both collected when the
container exits. Links pointing outside the output directory are not followed
when collecting, and the total collected size is capped.

**Your code stays outside the image.** Two reasons. Practically, an image
containing your code would need rebuilding on every edit, and builds take
minutes where edits take seconds. More importantly, the record states the run
used code with a particular fingerprint, referring to the archived snapshot. If
the container ran a different copy carried inside the image, the record would be
wrong while continuing to look right.

An image supplies software. It does not carry your code.

## 8. Why containers

Two reasons, and the second decided it.

**Any kind of dependency becomes expressible.** An image can hold compiled
libraries, solver binaries, a language runtime, GPU libraries or licensed
software. Nothing is installed on the user's machine when a run starts; it is
already inside the image, put there when the author built it. The question of
how OptPilot would install a particular kind of software never arises.

**Isolation becomes real.** Code in a package is not necessarily code the person
running it wrote or reviewed. Some methods have a language model write Python
while the run is in progress and then execute it, and in `or_solving` the text
that model responds to is the problem statement supplied at launch — so an
instruction embedded in a problem description reaches the running code.

Run as an ordinary program on the machine, that code would have the launching
user's full access: their home directory, their credentials, and OptPilot's own
archive. A container bounds it, and makes restrictions real rather than merely stated. A
component reaches the network only if it asked to, and the processor, memory,
time and output limits of §4 are applied by the container software rather than
trusted to the code. Exceeding one ends that piece of work; it is recorded as a
failure naming the limit, and the run continues under its failure policy (§9).

## 9. What the record contains

Two parts, written at different times.

**Before execution**, the run definition: the environment and method code
fingerprints, the image fingerprints, the settings, objective, budget and input
values, and the single fingerprint combining them. This identifies what was
run.

**During execution**, the results: every proposal, every score, the artifacts
produced, and the ordered sequence of events. These are appended as they happen
and refer back to the run definition.

So the run-definition fingerprint answers *what was run*, and the accumulated
results answer *what happened*. Two launches of the same setup with the same
inputs share a run-definition fingerprint, and each has its own results.

The record also notes the machine the run executed on: which container software
and version, the limits actually applied, and whether emulation was used. Two
runs sharing a run-definition fingerprint but differing in results can then be
compared on where they ran.

**When something fails.** A container that exits non-zero, returns something
unreadable, exceeds a limit, or fails to start ends that piece of work. It is
recorded as a failed trial carrying the exit status, the limit breached if any,
and the captured error output — never a silent gap. Nothing is retried
automatically. The run continues until the run setup's tolerance for consecutive
failures is reached.

If the container software or the machine itself dies, the record is left valid
and readable, marked as ended incomplete; the run definition already stands.
Containers are labelled with the run they belong to, and any left behind are
cleaned up at the next launch. The image check is repeated every time a
container starts, so an image removed part-way through a run fails loudly rather
than quietly resolving to something else. A run is not resumed; launching again
is a new execution of the same run definition.

**What the archive holds.** The archive stores code, results and fingerprints —
including the fingerprint of every image used. It does **not** store image
contents. So repeating a run exactly depends on the image still being obtainable
from wherever it came from; if it has been deleted there, the record remains
verifiable and readable but the run cannot be re-executed.

**Where the record stops.** A run records the code that produced it, not the
code that produced *that* code. The boundary is registration. If a simulator was
written by a generator, the simulator's source is captured and fully
inspectable, but the generator does not appear in the run's record. Recording
where code originally came from is a note attached to a package, unrelated to
how that code executes.

## 10. What this costs

- **Container software is required.** Every component runs in a container, so
  the machine must have Docker or Podman installed.
- **Each proposed solution pays container startup.** A method's container is
  started once for the whole run, so the cost scales with the number of
  candidates evaluated, not with rounds. Starting a container takes on the order
  of a second: a run of twenty-five trials gains well under a minute, one of
  many thousands gains hours. **This has not been measured on a real run, and
  should be before the design is committed to.**
- **The default image must be built, distributed and downloaded once.**
- **A package's own image is slow on first use** — such images run to a couple
  of gigabytes, and fetching one is a deliberate step.

## 11. What this does not address

- **Whether an image can be trusted.** A fingerprint proves the bytes are the
  ones someone published; it says nothing about whether those bytes are what
  they claim. Approving an image (§6) records a person's judgement; it does not
  establish one.
- **Repeatable image builds.** A run can be repeated exactly, because it names
  the image it used by fingerprint and that image still exists. Rebuilding "the
  same" image from scratch later can produce different contents, unless every
  version it installs is pinned during the build.
- **Sharing packages between people.** *Not built yet.*
- **Reclaiming space automatically.** Deletion is deliberate and manual (§12);
  nothing expires on its own, so an archive left alone still grows.
- **Components not written in Python.** An image can carry Java, R or CUDA and
  the rule covers them, but nothing has exercised that path.

## 12. Settled: storage, licensing, and privilege

**Records and images can be deleted deliberately.** The archive would otherwise
grow without limit on a laptop, and the realistic end of that is someone
deleting all of it by hand. So a person may remove chosen runs, reclaiming their
results and code snapshots.

Two rules keep this from undermining §1. A removal leaves a **note in place of
the record**, naming what was removed and when, so a deleted run is
distinguishable from one that never existed. And removal is always a person's
explicit act — nothing expires, and nothing is cleaned up automatically.

A run's image may be removed the same way, but **only once no remaining record
names it**. Images are shared: one image typically serves every run of a package
and often several packages. Deleting the image a run names because you deleted
*that* run would silently strip other runs of their ability to re-execute. So an
image is offered for removal when the last record referring to it goes, never
before.

The same applies to the standard image OptPilot supplies: past versions are kept
while any record still names one, and become removable when none does.

**Software that cannot be redistributed is not packaged.** A component needing a
commercially licensed library cannot have that library placed inside a published
image, so it cannot be shipped in a form anyone can run. Such a component is
removed from the package rather than shipped as a method that fails for everyone
without a licence. `production_agv_scheduling` lost its rolling-MILP baseline
for this reason on 2026-08-13; the simulator still accepts the kind of policy it
provided, so it can return if the licensing question is ever resolved.

**A container system running with administrator rights is accepted.** Container
software is commonly configured to run as a background service with
administrator rights over the machine; an arrangement exists that runs with only
the invoking user's rights, but it is not the default nearly anywhere. OptPilot
runs on either and does not warn.

This has an honest consequence for §8. On the common configuration, a container
bounds what code *ordinarily* reaches, and applies the processor, memory, time
and output limits — but code that breaks out of a container has administrator
access to the machine rather than only the user's. So the isolation §8 describes
is a strong boundary against ordinary behaviour, not a guarantee against
deliberate escape.

## 13. Relationship to what runs today

Today environments and methods run as ordinary processes on the machine, with
dependencies either carried inside the package or installed by hand, and the
bundled packages are written for that. Adopting this design means each of them
needs an image before it can run at all, and the code paths that prepare and
launch host processes are replaced rather than kept alongside. Whether that
happens in one step or package by package is part of the release decision
above, not of this design.

## 14. Open questions

- **Registering software the package's image does not have.** The alignment
  rules in §5 resolve the case where a workspace ends up with what the package
  already provides. They do not resolve the opposite: you installed something
  genuinely new. Two honest outcomes exist — capture it as an image for the one
  component you are registering, leaving every other component on the package's
  image, or move the package's image forward so everything gets it. The first
  keeps other components small and gives the package a second image to maintain;
  the second keeps one image and makes every component carry the addition.
  Neither is right in general: adding a solver one method needs argues for the
  first, upgrading a shared library argues for the second. This is a decision
  the author should be asked to make at registration, and the wording of that
  question matters more than the mechanism behind it.
- **Where images are hosted.** One source repository can publish several
  differently-named images, so a package needing one image plus an override is
  straightforward. Worth confirming for the intended registry: whether a person
  without an account can download a public image, and at what rate, since that
  sits directly in the path of someone using a package for the first time.
