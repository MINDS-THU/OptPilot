# How OptPilot runs code — the target design

**Status: design, 2026-08-13.** A complete description of how a codebase is
declared, installed, executed and recorded. Written to be read cold, without
prior knowledge of the codebase.

## 1. The pieces

**A package** is a folder of related work. It may contain:

- **Environments** — something that takes a proposed solution and scores it. A
  factory simulator, a scheduling model, a benchmark checker.
- **Methods** — something that proposes solutions. A genetic algorithm, an LLM
  that writes policy code, a solver.
- **Resources** — supporting tools. A generator with a web interface, a dataset
  browser.
- **Run setups** — a pairing of one environment with one method, plus an
  objective and a budget. ("Minimise waiting time, 25 attempts.")

**The catalog** is the set of packages available to you. A package becomes
available by being **registered** into it.

**The permanent store** is OptPilot's private, append-only archive. Every time
something runs, a complete copy of what was involved — the code, the settings,
the proposals, the scores, the sequence of events — is written there and never
modified. It is not a cache that can be cleared and rebuilt; it is the evidence.
(In the source it is called the *Realm*.)

**A fingerprint** is a long code computed from the contents of something. Change
one byte and the fingerprint changes completely. Two things with the same
fingerprint are guaranteed byte-identical, and anyone can recompute it to check.
(In the source, a *digest*.)

**The promise** these serve: after a run, you can state precisely what produced
a result, and someone else can verify it. That is what OptPilot is for, and
every rule below exists to keep that statement true.

## 2. The rule that governs execution

> **Anything whose output becomes part of the permanent record must identify its
> libraries by fingerprint, and that fingerprint must be part of the record.**

Two things produce output that becomes part of the record: **environments** (their
scores are the results) and **methods** (their proposals are recorded and
evaluated). Everything else — one-shot tools, interactive views, editing
workspaces — does not, and is not bound by the rule.

## 3. Declaring what a package needs

An author declares one of three situations.

### Situation A — nothing beyond what OptPilot already has

The code uses only Python's built-in libraries, or libraries OptPilot itself
depends on. Nothing is declared. Nothing is installed.

### Situation B — pure Python libraries, carried inside the package

Libraries written entirely in Python can simply be included in the package
folder. They travel with the code and are fingerprinted along with it.

*Example:* the production-and-AGV scheduling package carries its own copy of the
simulation library it uses. Nothing is installed on the user's machine and the
libraries are covered by the record automatically.

An author may also list pure-Python libraries as files to be assembled into a
prepared folder at first use. OptPilot verifies each file against a recorded
fingerprint and never contacts the internet while doing so.

### Situation C — compiled libraries, or separate programs

Some libraries contain compiled machine code (`ortools`, `numpy`, PyTorch), and
some dependencies are separate programs entirely (the GLPK solver). Neither can
be carried as Python source.

For these, the author builds a **container image**: a self-contained bundle
holding an operating system, an interpreter, and the compiled software. The image
gets a fingerprint. The package then names it:

```yaml
runtime:
  sandbox: container
  container:
    image: ghcr.io/example/or-solving@sha256:<fingerprint>
    platform: linux/amd64
```

Only an already-built image may be named, identified by fingerprint. A package
cannot ask OptPilot to build one on the fly, because a build is only repeatable
if everything it fetches is pinned — and a name like `latest` points at
different contents over time, which would make the record meaningless.

## 4. Publishing a package

Each package lives in its own source repository.

If it needs an image, that repository's automated build produces it, publishes it
to the registry attached to the repository, and reports the fingerprint. The
author writes that fingerprint into the package.

The consequence worth stating plainly: **installing moves from the user to the
author, and happens once.** The author's build machine resolves and installs the
compiled software. Every user afterwards downloads the finished result and checks
its fingerprint.

## 5. Registering a package

Registration is the moment a package enters your catalog. OptPilot validates it —
the configuration is well-formed, declared files exist, declared libraries are
accounted for — and copies its contents into the permanent store, fingerprinted.

**Registration is also the boundary of the record.** A run records the code that
produced it. It does not record whatever produced *that* code. If a simulator was
written by a generator, the simulator's source is captured and fingerprinted at
registration and is fully inspectable, but the generator is not part of the run's
record. The boundary has to stop somewhere, and "registered in the catalog" is a
line that can be checked.

## 6. Launching a run

When a run setup is launched, OptPilot works through four steps before any code
executes.

**Capture.** The environment's code and the method's code are copied out of the
package into the permanent store and fingerprinted. From here on, execution uses
these copies, not the folder on disk — so editing the package mid-run cannot
change what is running or what is recorded.

**Resolve the runtime.** For each of the environment and the method:

- *Situation A:* nothing to do.
- *Situation B:* the pure-Python libraries are assembled into a prepared folder,
  each file checked against its recorded fingerprint. The result is stored and
  reused on later runs.
- *Situation C:* the named image must already be present locally. OptPilot does
  not download it silently. It verifies the image's fingerprint matches the one
  the package names, and checks that image has been approved for execution.

**Check before anything is written.** Missing container software, an
unapproved image, an absent image, or a fingerprint mismatch all stop the launch
here, with a message naming the problem. Nothing is written to the permanent
store and no code runs.

**Write the run definition.** A record is created naming: the environment code
fingerprint, the method code fingerprint, the runtime fingerprints, the settings,
the objective, the budget. These combine into a single fingerprint for the run
definition. Same inputs give the same fingerprint; any difference gives a
different one.

## 7. Where code actually executes

Three things come together at execution, and they stay separate deliberately:

| | What it is | Where it comes from |
| --- | --- | --- |
| Your code | The environment or method source | Copied from the permanent store into a temporary folder |
| The libraries | Pure-Python folder, or a container image | Prepared folder on disk, or inside the image |
| The execution | The running program | A normal process, or a container |

### Situation A and B — a normal process

OptPilot starts an ordinary program on your machine. It points the interpreter at
the temporary folder holding your code, and at the prepared library folder if
there is one. No container software is involved or required.

### Situation C — a container

OptPilot starts a container from the named image and **makes the temporary folder
holding your code visible inside it** — the same way plugging in an external drive
makes files visible to a program. The image supplies the libraries; the folder
supplies your code.

**Your code is never built into the image.** Two reasons:

- Practical: you would rebuild the image on every edit. Builds take minutes,
  edits take seconds.
- Correctness: the record says the run used the code with a particular
  fingerprint, pointing at the copy in the permanent store. If the container ran
  a *different* copy baked into the image, the record would be wrong — and wrong
  invisibly, since it would still look right.

So the container exists to supply libraries. It does not hold your code.

### A worked example

The natural-language OR solving package:

- The **image** holds `ortools`, `pymoo`, `smolagents` and the GLPK solver —
  third-party software only.
- The **method's code**, including the solving engine bundled into the package,
  stays in the permanent store and is shown to the container from a temporary
  folder.
- The **environment** needs only Python's built-in libraries, so it runs as an
  ordinary process on your machine, with no container at all.

One study, two different execution modes, chosen independently by what each half
declares.

## 8. Everything else that executes

Three other things run code. None produces output that becomes part of the
record, so none is bound by the rule.

**One-shot tools** (a generator that turns a description into a simulator) run as
ordinary processes. They hand you files; you decide whether to register them. The
record begins at registration.

**Interactive views** (a 3D factory visualisation, a solver console) may run in a
container, for isolation rather than record-keeping: a web application that opens
ports and reaches the network is worth keeping away from the rest of your
machine. Looking at one produces no result.

**Editing workspaces** always run in a container, and are the most permissive
thing here by design. A development environment needs to be writable and needs
network access so you can install things while experimenting. That is what it is
for. Whatever you write becomes subject to the rule only when you register it.

## 9. What the record ends up containing

For a completed run:

- The environment code, byte-for-byte, with its fingerprint.
- The method code, byte-for-byte, with its fingerprint.
- The runtime fingerprint — the prepared library folder, or the image.
- The settings, objective, budget, and any values supplied at launch.
- Every proposal, every score, and the ordered sequence of events.
- A single run-definition fingerprint covering all of the above.

Which supports the statement the whole design exists for: *this exact code, with
these exact libraries, produced this result* — and anyone can check it.

## 10. What this does not do

- **It does not judge an image's trustworthiness.** A fingerprint proves you have
  the exact bytes someone published. Whether those bytes are what they claim is a
  separate question, answered by a human approving the image before it may run.
- **It does not make image builds repeatable.** The fingerprint makes *replay*
  exact. Rebuilding "the same" image later can produce different contents unless
  every library version is pinned during the build.
- **It does not make the first use fast.** These images run to a couple of
  gigabytes and are not downloaded silently. First use is a deliberate step.
- **It is untested outside Python.** The rule covers a package built on Java, R
  or CUDA, and an image can carry them. Nothing has exercised that yet.
