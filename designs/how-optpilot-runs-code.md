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
**archive** on your machine. The archive is append-only: written once, never
modified.

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
and vehicle scheduling simulator with eight methods for it, and `or_solving`,
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

### How many images a package needs

**One image per distinct set of software the package requires — not one per
component.** Most packages need exactly one.

`production_agv_scheduling` shows why the distinction matters. Its seven
environments are one simulator configured seven ways: they run the same
evaluator code and need identical software, so seven images would be seven
copies of the same thing. Of its eight methods, seven need nothing beyond what
OptPilot itself provides; one, `rolling-milp-baselines`, needs a commercial
solver library. So that package needs at most two images.

### The default image

A package declaring no image runs in the **default image**, which OptPilot
provides. It contains Python and the libraries OptPilot itself depends on.

This covers a package whose code uses only Python's standard library, and one
carrying pure-Python libraries inside its own folder — those travel with the
code and import directly. `production_agv_scheduling` carries its simulation
library this way.

The default image has a fingerprint like any other, so the record stays complete
without the author declaring anything.

### A package's own image, and per-component overrides

Some software cannot travel inside a package folder as Python source:

- Libraries containing compiled machine code, such as `ortools`, `numpy` or
  PyTorch.
- Separate programs, such as the GLPK or IPOPT solvers.
- Other language runtimes, such as Java, R or Node.

A package needing any of these names an image, which every component in it uses
unless that component says otherwise:

```yaml
# package settings
runtime:
  container:
    image: ghcr.io/example/agv-scheduling@sha256:<fingerprint>
    platform: linux/amd64
```

A single component may name a different one, using the same fields in its own
settings file:

```yaml
# rolling_milp method settings
runtime:
  container:
    image: ghcr.io/example/agv-scheduling-milp@sha256:<fingerprint>
    platform: linux/amd64
```

The override exists because components within one package can differ sharply.
In `or_solving`, the environment needs only Python's standard library while the
method needs a native optimisation library, a solver program and a
language-model client. Forcing them to share would make the environment carry
gigabytes it never touches.

An author may therefore set only the package image and be done, or add overrides
where they earn their keep.

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

OptPilot adds the editing tools on top of whichever image is used, and excludes
them when capturing (below), so a package's image stays a description of what
its code needs and never contains an editor.

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

If you installed nothing beyond what the starting image already had, the
existing fingerprint is kept and no image is produced.

Adding a second piece later writes different files into the same folder. Pieces
built at different times, in different workspaces, coexist because they occupy
different paths in one directory.

**Share it.** Publishing the folder where others can obtain it, typically a
source repository. *Not built yet.*

## 6. Running a run setup

Four steps happen before any of your code executes. The checks come first, so a
failure leaves nothing behind.

**Obtain and check the images.** Each component's image — the package's or its
own override — must already be present on the machine. OptPilot does not fetch
one on its own: it reports which image is needed and how large it is, and you
fetch it, either through the container software directly or by accepting the
prompt. You then **approve** that exact fingerprint for execution, which
OptPilot remembers; approving is how you say you are willing to run software
someone else built. A missing engine, an image that is absent, unapproved, or
whose fingerprint does not match what the package names, stops the launch here.
Nothing has been written and no code has run.

**Capture the code.** The package folder is snapshotted into the archive and
fingerprinted. Execution uses that snapshot, so editing the folder while a run
is in progress changes neither what runs nor what is recorded.

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

A container is started for each piece of work: once per proposed solution for an
environment, once per round of proposals for a method. It is not one long-lived
container for the whole run, which is why startup time matters (§10).

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
archive. A container bounds it, and makes restrictions enforceable rather than
merely stated — a component declaring it needs no network can be held to that,
and processor, memory and time limits can be applied and relied upon.

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

**Where the record stops.** A run records the code that produced it, not the
code that produced *that* code. The boundary is registration. If a simulator was
written by a generator, the simulator's source is captured and fully
inspectable, but the generator does not appear in the run's record. Recording
where code originally came from is a note attached to a package, unrelated to
how that code executes.

## 10. What this costs

- **Container software is required.** Every component runs in a container, so
  the machine must have Docker or Podman installed.
- **Each piece of work pays container startup.** An environment is invoked once
  per proposed solution, and starting a container takes on the order of a
  second. A run of twenty-five trials gains well under a minute; one of many
  thousands gains hours. **This has not been measured on a real run, and should
  be before the design is committed to.**
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
- **Components not written in Python.** An image can carry Java, R or CUDA and
  the rule covers them, but nothing has exercised that path.

## 12. Open questions

- **Registering from a second workspace.** If a package's second component is
  built in a different workspace, that workspace captures its own image. When
  both components need much the same software, the package ends up with two
  large near-identical images. The options are to accept it, to let the author
  say "reuse the image already recorded for this package", or to detect that the
  captured environments match and share one fingerprint. The second is the most
  predictable.
- **Where images are hosted.** One source repository can publish several
  differently-named images, so a package needing one image plus an override is
  straightforward. Worth confirming for the intended registry: whether a person
  without an account can download a public image, and at what rate, since that
  sits directly in the path of someone using a package for the first time.
- **Redistribution of licensed software.** An image containing a commercial
  solver may not be freely publishable — which affects
  `production_agv_scheduling` directly, since its `rolling-milp-baselines`
  method needs one. This is a per-package legal question, not a packaging one.
