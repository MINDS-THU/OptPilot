# How OptPilot runs code — the target design

**Status: design, 2026-08-13. Not implemented.** Written to be read without
prior knowledge of OptPilot; every term is defined where it first appears.

## 1. What OptPilot does, and what it promises

OptPilot runs optimisation experiments. You give it something that proposes
candidate solutions and something that scores them, and it runs the two against
each other under an objective and a budget.

Its distinguishing promise is about what happens *afterwards*. Every run writes
a permanent copy of everything involved — the code, the settings, every
proposal, every score, the ordered sequence of events — into a private
**archive** on your machine. The archive is append-only: written once, never
modified.

Every stored item gets a **fingerprint**: a code computed from its contents,
such that changing a single byte changes the code completely, and anyone can
recompute it to verify. Fingerprints are how one stored item refers to another
unambiguously.

Together these support a claim: *this exact code, with these exact libraries,
produced this result* — and a third party can check it.

## 2. The rule that governs execution

> **Anything whose output becomes part of the permanent record must identify
> its libraries by fingerprint, and that fingerprint must be part of the
> record.**

Two things produce such output: **environments**, which score proposed
solutions, and **methods**, which propose them.

The reasoning: if a library reaches the machine some way OptPilot cannot
describe — someone installing it by hand — then the record describes a run while
staying silent about something that may have determined its result. The record
would be incomplete in a way that still looks complete, which is worse than
being obviously absent.

## 3. Packages and the catalog

A **package** is a folder holding related work. Inside it are:

- **Environments** — code that scores a proposed solution. A factory simulator,
  a scheduling model, a benchmark checker.
- **Methods** — code that proposes solutions. A genetic algorithm, a solver, a
  language model that writes policy code.
- **Resources** — supporting tools, such as a generator with a web interface.
- **Run setups** — a pairing of one environment with one method, plus an
  objective and a budget.

Each of these is described by a settings file inside the folder, alongside the
code it refers to.

The **catalog** is the set of packages available to you. Every entry in it is a
folder on disk: you can open it, read it, edit it, and put it in a source
repository.

**A version is a snapshot.** Taking a version copies the folder's current
contents into the archive as an immutable, numbered record — a **snapshot**. The folder stays
editable; the snapshot never changes. This is the arrangement source control
uses — a working folder you edit, and an immutable history captured from it.

Snapshots are cheap. Each records which files are present and each file's
fingerprint; contents already in the archive are reused. A snapshot of a 4 GB
package in which one file changed costs one file.

## 4. Building a package

**Write the code.** You work in a **workspace** — an editable project with a
development environment around it. A workspace has network access so you can
install things and experiment freely.

**Register each piece.** You declare "this code is an environment" (or a method,
or a resource), and which package it belongs to. Registering writes the code and
its settings file into that package's folder and takes a snapshot. The piece is
part of the package from that moment.

Adding a second piece later writes different files into the same folder. Two
pieces built at different times, in different workspaces, coexist because they
occupy different paths in one directory.

**Share it.** Publishing the folder somewhere others can obtain it, typically a
source repository. This is not built yet.

## 5. What a package declares about its dependencies

Code runs inside a **container**: an isolated environment created from an
**image**, which is a self-contained bundle holding an operating system, a
Python interpreter, and installed software. An image has a fingerprint.

Every component of a package runs this way, so the only question an author
answers is *which image*.

### The default image

A package that declares no image runs in the **default image**, which OptPilot
provides. It contains Python and the libraries OptPilot itself depends on.

This covers a package whose code uses only Python's standard library, and a
package that carries pure-Python libraries inside its own folder — those travel
with the code and can be imported directly.

The default image has a fingerprint like any other, so the record is complete
without the author declaring anything.

### A package's own image

Some software cannot travel inside a package folder as Python source:

- Libraries containing compiled machine code, such as `ortools`, `numpy` or
  PyTorch.
- Separate programs, such as the GLPK or IPOPT solvers.
- Other language runtimes, such as Java, R or Node.

A package needing any of these names its own image, identified by fingerprint:

```yaml
runtime:
  container:
    image: ghcr.io/example/or-solving@sha256:<fingerprint>
    platform: linux/amd64
```

An image must already be built when it is named. OptPilot will not build one
during a run, because a build is only repeatable if everything it fetches is
pinned to an exact version, and a moving name such as `latest` refers to
different contents at different times.

### How an author obtains an image

Through the workspace they were already working in:

1. Build and debug there, installing whatever the code needs.
2. Capture the workspace as an image. It receives a fingerprint.
3. Name that fingerprint in the package's settings file.

The author's machine does the installing, once. Everyone afterwards downloads
the finished image and verifies its fingerprint, installing nothing.

Replacing an image is ordinary: capture a new one, update the settings, take a
new snapshot. Earlier runs continue to name the earlier image and remain
replayable. Naming an image by fingerprint does not freeze a package — it makes
each run state exactly what it used.

## 6. Running a study

Four steps happen before any of your code executes.

**Capture.** The environment's and the method's code are copied from the package
folder into the archive and fingerprinted. Execution uses those copies, so
editing the folder while a run is in progress changes neither what runs nor what
is recorded.

**Resolve images.** Each component's image must already be present on the
machine; OptPilot does not download one silently. It verifies the image's
fingerprint matches what the package names, and that the image has been approved
for execution.

**Check before writing.** Absent container software, an unapproved image, a
missing image, or a fingerprint mismatch stops the launch at this point with a
message naming the problem. Nothing is written to the archive and no code runs.

**Write the run definition.** A record naming the code fingerprints, the image
fingerprints, the settings, the objective and the budget, combined into a single
fingerprint identifying the run.

## 7. Where code physically runs

Three things come together at execution and stay separate:

| | What it is | Where it comes from |
| --- | --- | --- |
| Your code | The environment or method source | Copied from the archive into a temporary folder |
| The software it needs | The default image, or the package's own | Inside the image |
| The execution | The running program | A container |

OptPilot starts a container from the image and **makes the temporary folder
holding your code visible inside it** — the way plugging in an external drive
makes files visible to a program. The image supplies software; the folder
supplies your code.

**Your code stays outside the image.** Two reasons. Practically, an image
containing your code would need rebuilding on every edit, and builds take
minutes where edits take seconds. More importantly, the record states that the
run used code with a particular fingerprint, referring to the archived copy. If
the container ran a different copy carried inside the image, the record would be
wrong while continuing to look right.

An image supplies software. It does not carry your code.

Everything OptPilot executes works this way — environments, methods, one-shot
tools, interactive views, and the development environment of a workspace. One
execution path, one place where isolation is applied.

## 8. Why containers

Two reasons, and the second is the one that decided it.

**Any kind of dependency becomes expressible.** An image can hold compiled
libraries, solver binaries, a Java runtime, GPU libraries or licensed software.
Nothing is installed on the user's machine at run time; it is already inside the
image, put there when the author built it. So the question of how OptPilot would
install a particular kind of software never arises.

**Isolation becomes real.** Code from a package is not necessarily code the
person running it wrote or reviewed. Some methods have a language model write
Python at run time and then execute it, and in at least one case the text that
model responds to is supplied by whoever launched the study — so an instruction
embedded in a problem description reaches the running code.

Running that as an ordinary program on the machine would give it the launching
user's full access: their home directory, their credentials, and OptPilot's own
archive. A container bounds it. It also makes restrictions enforceable rather
than merely stated — a package that declares it needs no network can be held to
that, and processor, memory and time limits can be applied and relied upon.

## 9. What the record contains

For a completed run: the environment code with its fingerprint, the method code
with its fingerprint, the image fingerprints, the settings, objective, budget
and any values supplied at launch, every proposal and score, the ordered
sequence of events, and one fingerprint covering all of it.

**Where the record stops.** A run records the code that produced it, not the
code that produced *that* code. The boundary is registration. If a simulator was
written by a generator, the simulator's source is captured and fully
inspectable, but the generator does not appear in the run's record. Recording
where code originally came from is a note attached to a package, and is
unrelated to how that code executes.

## 10. What this costs

- **Container software is required.** Every component runs in a container, so
  the machine must have Docker or Podman installed.
- **Each evaluation pays container startup.** An environment is invoked once per
  proposed solution, and starting a container takes on the order of a second. A
  study of twenty-five trials gains well under a minute; a study of many
  thousands gains hours. **This has not been measured on a real study, and
  should be before the design is committed to.**
- **The default image must be built, distributed, and downloaded once.**
- **A custom image is slow on first use** — such images run to a couple of
  gigabytes, and nothing is downloaded silently.

## 11. What this does not address

- **Whether an image can be trusted.** A fingerprint proves the bytes are the
  ones someone published; it says nothing about whether those bytes are what
  they claim to be. A person approves an image before it may run.
- **Repeatable image builds.** Replaying a run is exact, but rebuilding "the
  same" image later can produce different contents unless every version is
  pinned during the build.
- **Sharing packages between people.** Not built yet.
- **Components not written in Python.** An image can carry Java, R or CUDA and
  the rule covers them, but nothing has exercised that path.
