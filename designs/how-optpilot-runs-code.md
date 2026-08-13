# How OptPilot runs code — the target design

**Status: design, 2026-08-13. Not implemented.** Written to be read without
prior knowledge of the codebase; every term is defined where it first appears.

Two decisions shape this document, both taken 2026-08-13:

- **Everything runs in a container.** There is no second execution mode.
  Packages that need nothing special use a default image OptPilot provides.
- **A package is a folder.** There is one kind, editable and inspectable, and
  versions are immutable snapshots taken of it.

## 1. What OptPilot is for

When you run a study, OptPilot saves a permanent copy of everything involved —
the code, the settings, every proposal, every score, the sequence of events —
in a private archive on your machine. The archive is append-only: written once,
never modified.

Everything saved gets a **fingerprint**: a code computed from the contents,
such that changing one byte changes the code completely, and anyone can
recompute it to check.

The purpose is that afterwards you can say precisely *"this run used this code
with these libraries and produced this result"*, and someone else can verify it.

## 2. The rule everything follows

> **Anything whose output becomes part of the permanent record must identify
> its libraries by fingerprint, and that fingerprint must be part of the
> record.**

Two things produce such output: **environments**, which score proposed
solutions, and **methods**, which propose them.

Why this rather than "make installing easier": if a library is installed by
hand, OptPilot cannot say what arrived. The record then describes a run while
staying silent about something that may have determined the answer. That is not
an inconvenience; it is the record being wrong while still looking right.

## 3. A package is a folder

A **package** is a folder holding related work — the code for a simulator, the
code for an algorithm, and settings files describing them. The folder is
yours: readable, editable, and exactly what you would put in a source
repository.

There is no second, hidden kind of package. Everything in the catalog is a
folder you can open.

**Versions are snapshots of that folder.** Taking a version copies the folder's
current contents into the archive as an immutable, numbered snapshot. The
folder stays editable; the snapshot never changes.

This is the arrangement source control uses: a working folder you edit, and an
immutable history captured from it. Snapshots are cheap — they record which
files are present and each one's fingerprint, and contents already stored are
reused. A new snapshot of a 4 GB package where one file changed costs one file.

## 4. Building a package

**Write code.** You work in a **workspace** — an editable project with a
development environment around it. It runs in a container with network access,
so you can install and experiment freely. That is what it is for.

**Register each piece.** You declare "this code is an environment" (or a method,
or a resource), and which package it belongs to. Registering writes the code
and its settings file into that package's folder, and takes a new snapshot.
Registering is not a draft step — it is the act that adds the piece.

**Share it.** Putting the folder where others can obtain it, typically a source
repository. This does not exist yet.

Because a package is a folder, adding a second piece later is unremarkable:
it writes different files into the same folder. Two pieces built in two
different workspaces coexist without special handling.

## 5. What a package declares about its dependencies

Every component runs in a container, so the only question is **which image**.

An **image** is a self-contained bundle holding an operating system, a Python
interpreter, and installed software. It has a fingerprint.

### The default image — most packages

If a package declares nothing, its components run in the **default image**
OptPilot provides. It contains Python and OptPilot's own dependencies. It has a
fingerprint like any other, so the record stays complete without the author
doing anything.

This covers two cases that used to be distinct:

- Code using only Python's built-in libraries.
- Code carrying pure-Python libraries inside the package folder. Those travel
  with the code, are bind-mounted in with it, and import normally.

### A package's own image — when more is needed

Some software cannot travel as Python source: libraries containing compiled
machine code (`ortools`, `numpy`, PyTorch), separate programs (the GLPK or
IPOPT solvers), or other language runtimes (Java, R, Node).

For these the author builds an image and names it by fingerprint:

```yaml
runtime:
  container:
    image: ghcr.io/example/or-solving@sha256:<fingerprint>
    platform: linux/amd64
```

Only an already-built image may be named, and only by fingerprint. A package
cannot ask OptPilot to build one, because a build is repeatable only if
everything it fetches is pinned, and a moving name like `latest` points at
different contents over time.

### How an author obtains an image

The intended path uses the workspace they were already working in:

1. Build and debug in the workspace, installing whatever is needed.
2. **Capture the workspace as an image.** It gets a fingerprint.
3. Name that fingerprint in the package.

The author's machine does the installing, once. Every user afterwards downloads
the finished image and verifies its fingerprint, installing nothing.

An image can be replaced whenever new code needs new software: capture a new
one, update the settings, take a new snapshot. Earlier runs keep naming the
earlier image and remain replayable. Pinning does not freeze a package; it makes
each run state exactly what it used.

## 6. Running a study

**Capture.** The environment's and the method's code are copied from the
package folder into the archive and fingerprinted. Execution uses those copies,
so editing the folder mid-run cannot change what runs or what is recorded.

**Resolve images.** Each component's image — the default one or its own — must
already be present locally. OptPilot does not download silently. It checks the
image's fingerprint matches what is named, and that the image is approved for
execution.

**Check before writing anything.** Missing container software, an unapproved
image, an absent image, or a fingerprint mismatch stops the launch here, naming
the problem. Nothing is written to the archive and no code runs.

**Write the run definition.** A record naming the code fingerprints, the image
fingerprints, the settings, the objective and the budget — combining into one
fingerprint for the whole definition.

## 7. Where code physically runs

Three things come together, and they stay separate deliberately:

| | What it is | Where it comes from |
| --- | --- | --- |
| Your code | The environment or method source | Copied from the archive into a temporary folder |
| The software it needs | The default image, or the package's own | Inside the image |
| The execution | The running program | A container |

OptPilot starts a container from the named image and **makes the temporary
folder holding your code visible inside it** — as plugging in an external drive
makes files visible to a program. The image supplies software; the folder
supplies your code.

**Your code is never built into the image.** Two reasons. Practically, you would
rebuild the image on every edit; builds take minutes and edits take seconds.
More importantly, the record says the run used code with a particular
fingerprint, pointing at the archived copy. If the container ran a *different*
copy baked into the image, the record would be wrong — and wrong invisibly,
because it would still look right.

The container supplies software. It does not hold your code.

### Everything runs this way

| What runs | Image |
| --- | --- |
| Environment (scores solutions) | Default, or its own |
| Method (proposes solutions) | Default, or its own |
| One-shot tool | Default, or its own |
| Interactive view | Default, or its own |
| Editing workspace | A development image |

There is no local-process mode. A single execution path, one set of rules, one
place where isolation is enforced.

## 8. Why there is no local-process mode

A local process provided no isolation at all. Between starting one and running
your code, OptPilot performed two operations: change directory, then execute.
There was no sandbox, no privilege drop, and no resource limit anywhere.

Package code therefore ran as you, with your access: your home directory, your
keys, and OptPilot's own archive, which is writable by the same user.

Several declarations *looked* like boundaries and were not enforced — a
disabled-network setting, read-only source, storage quotas, processor and memory
limits, and the evaluator timeout. A study run even recorded that network access
was "denied, enforced", which was false.

This mattered concretely: the OR-solving package runs LLM-written Python with
an API key present, and its problem statement is free text, so an instruction
embedded in a problem description reached the host. The LLM policy-search
packages have the same shape.

Containers give real isolation, and they give it uniformly. Once every
component runs in one, a declared network restriction can actually be enforced,
resource limits can actually be applied, and the record can state the truth.

## 9. What the record contains

For a completed run: the environment code with its fingerprint, the method code
with its fingerprint, the image fingerprints, the settings, objective, budget
and launch values, every proposal and score, the ordered sequence of events,
and one fingerprint covering all of it.

**Where the record stops.** A run records the code that produced it, not the
code that produced *that* code. The boundary is registration. If a simulator was
written by a generator, the simulator's source is captured and fully
inspectable, but the generator is not part of the run's record. Recording where
code came from is a note attached to a package, unrelated to how it executes.

## 10. What this costs

Stated plainly, because these are real:

- **Container software becomes required.** There is no path that runs without
  it. Previously the command line worked without any.
- **Each evaluation pays container startup.** An environment is invoked once per
  proposed solution. Startup is on the order of a second. A twenty-five-trial
  study gains well under a minute; a study with many thousands of trials gains
  hours. **This has not been measured on a real study and should be, before
  the design is committed to.**
- **The default image must be built, distributed and downloaded once.**
- **First use of any custom image is slow** — these run to a couple of
  gigabytes, and nothing is fetched silently.

## 11. What this does not solve

- **Whether an image is trustworthy.** A fingerprint proves you have the exact
  bytes someone published, not that those bytes are what they claim. A person
  approves an image before it may run.
- **Repeatable image builds.** Replay is exact; rebuilding "the same" image
  later can differ unless every version is pinned during the build.
- **Sharing packages.** Distribution does not exist yet.
- **Non-Python components.** An image can carry Java, R or CUDA and the rule
  covers them, but nothing has exercised that.
