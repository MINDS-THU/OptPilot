# The first official release — what ships, what waits

This is a **scope document**, not an architecture. Two target designs already
exist and remain the destination: `designs/how-the-assistant-works.md` and
`designs/how-the-assistant-works-codex.md`. This document answers a different
question: *what is the smallest amount of work that produces a release we are
not embarrassed by, and does not have to be undone later?* Dated 2026-08-16.

**Revised 2026-08-17 after grounding every item against the code and running
Studio against a fresh install.** Section 0 records what that changed. Several
items were wrong, and — more importantly — the plan was missing an entire
category of release blocker. **On the evidence below, the plan as first
written would NOT have produced a releasable product.**

---

## 0. What grounding changed

**Four items were misdiagnosed.**

*The catalog cache already exists* and is tested (a five-second reuse window).
The real costs are elsewhere: two requests that miss the cold cache both do
the full build because the lock is released before the work starts; the
compatibility endpoint has no cache at all and pays about a second on every
cold call; and building it re-reads each method's settings file once per
environment — 182 file parses where 13 would do. Separately, the 5–20 second
figure is **not reproducible from computation** (measured 2.2 s on a local
disk). The multiplier is almost certainly that this checkout lives in a
cloud-synced folder, since the scan calls `resolve()` on ~13,000 paths. Move
the checkout off the synced folder before optimizing anything.

*Giving components human names is not content-only.* The environment and
method settings schemas forbid unknown fields and have no name field, so this
needs a schema change first. Worth doing anyway: every shipped run setup's
name is its own identifier, which is why run titles read as slugs.

*The resource-action tool's real work is not the tool.* Action output
currently lands in Studio's private folder, not in the person's workspace, so
the "generate then register without copying files" promise needs that root
changed — that is the substantive part.

*Splitting the smoke-test permission changes nothing on its own*: the
permitted values would have to include "allowed without asking", or the
prompts remain.

**And the plan missed a whole category: the product cannot be installed.**
See §0.1. Every first-hour improvement in this plan assumes the person has
the five ready-made packages, and an installed user has none of them.

### 0.1 The installation story does not work end to end

| What was assumed | What is actually true |
| --- | --- |
| `pip install optpilot` gets you the product | It gets you **version 0.1.0**, the previous release. |
| Studio can be installed | **`optpilot-studio` is not published at all.** There is no supported way to get the web app except cloning the repository. |
| The five ready-made packages come with it | They are **deliberately excluded from every distribution** — the packaging rules group `catalog/` with research scratch. An installed user's catalog is empty, so nothing can be browsed, launched, or auto-set-up. |
| The install is quick | The documented source install downloads **about 900 MB, 392 MB of it PyTorch**, for two dependencies nothing in the repository uses any more. |

This is the largest single finding of the review. Until it is answered, the
release has no delivery mechanism, and the decision already taken (set the
ready-made packages up automatically on first start) applies only to people
who clone the repository.

### 0.2 Blockers found by running it, that the plan did not contain

Each was reproduced live against a fresh install.

| # | What a new user meets | Fix |
| --- | --- | --- |
| N1 | **Every shipped run setup is unlaunchable and the product refuses to fix it.** All 18 carry a red "Publish first" badge with Launch disabled. Worse, the code *actively refuses*: registering a shipped package raises "ships with OptPilot and cannot be registered into." | several days; part of making bundled packages work |
| N2 | **The operations-research package cannot be registered at all** — sealing rejects macOS extended attributes, which every downloaded (rather than cloned) copy carries. | hours |
| N3 | **The operations-research flagship cannot be launched** — Studio treats an optional setting (`COOPA_HOME`) as required, and the documentation correctly says it is optional. Studio has no concept of an optional declared value. | about a day |
| N4 | **The home page's main invitation is a chat box that cannot answer.** It says "Queued locally — the OpenHands runtime is disabled", then sits at "Working" forever, across restarts. The README never mentions the Assistant or how to enable it. | about a day |
| N5 | **The publish panel says "Package validation found blockers" before any validation has run.** All five packages are clean. A cautious user concludes the product ships broken packages. | hours |
| N6 | **One click mounts the product's own package folder as a read-write project** — and it is the button Studio itself offers as the remedy for "Publish first". The catalog page's equivalent button is disabled, so two screens disagree. | about a day |
| N7 | **After a Studio crash, a run shows a live "running" badge with no sign of recovery** for minutes, while no worker exists. Recovery does work; nothing says so. | about a day |
| N8 | **The trial map silently caps at 114 chips**, so on a 135-trial run 18 finished trials are simply absent, with no "and more". | hours |
| N9 | **The runs list contradicts the run page it links to** — 15/135 beside 68/135, "not available" beside a real result. | hours |
| N10 | **The image-approval command Studio offers assumes a source checkout** (`uv run`) and omits the archive location, so it can silently approve into the wrong place. | hours |

### 0.4 Decision: the example packages ship inside the install, as ordinary packages

**Decided 2026-08-17.** The five example packages travel with the install, but
they are not the install's property. On first run OptPilot copies them into a
per-user packages folder — beside the permanent store it already keeps, which
already has a correct location on each operating system — and from that moment
they are ordinary packages: writable, carrying their own identity, registering
through exactly the same path any contributed package uses.

**Why this shape rather than the two obvious ones.** Leaving the packages
inside the installed software would be cheapest to *"the packages are
present"*, which is not the same as *"a person can use them"*: that location is
read-only and is overwritten by the next upgrade, so adapting an example — the
most natural thing an interested person does — means copying it out by hand
into something OptPilot no longer recognises as related. It would also keep the
special case that causes the worst visible defect today, where OptPilot refuses
to register its own packages and every shipped run setup is therefore
unlaunchable.

Distributing each package from its own repository was the other candidate and
is the right long-term shape, but it was measured at three to four and a half
weeks, over half of that being repository and testing logistics rather than
OptPilot code, with no offline installation at all until an archive route is
also built. Critically, **choosing this does not give that up.** The reason to
want separate repositories is to avoid shipped packages behaving differently
from contributed ones — and that difference is caused by special-casing in
code, not by how the packages travelled. Once every package lives in the same
per-user folder with the same identity and the same registration path, adding
a download command later is a pure addition: fetch a folder into a place that
already works. Because identities are fixed, a later download of a package that
shipped with the install *updates* it rather than colliding with it.

**What this decision requires**, in the order it has to happen:

| Step | Note |
| --- | --- |
| Include the packages in what gets published | Reverses today's exclusion, which groups them with research scratch. **Decided: include the three-dimensional animation** — the distribution carries all five packages at about 31 MB. |
| Define the per-user packages folder | A location beside the existing permanent store, with an environment variable to override it. Small. |
| Copy the shipped packages there once, on first run | Idempotent, so a repeat start does nothing. Never overwrites a package the person has edited. |
| Teach Studio to look there | Studio currently looks only beside the directory it was started from; this is the one genuine code change, and it is needed under every option. |
| Give each package an identity file | Now required, not optional: this is what lets a package be recognised as itself after it moves, so re-installing updates rather than collides. None of the five has one today. |
| Remove the refusal to register shipped packages | The special case that makes all 18 shipped run setups unlaunchable. |
| Ignore or strip macOS file marks when sealing | Without this the operations-research package cannot be registered at all, so the automatic setup would fail on it. |

Together this replaces the earlier item "make the bundled packages launchable
out of the box" and absorbs most of the installation problem in §0.1. Estimated
at about a week. It leaves untouched the separate problem that the published
release is stale and the web application was never published at all.

### 0.5 The remaining release decisions, settled 2026-08-17

**The first release contains both the command-line tool and Studio.** The
command-line path already works as documented; Studio's does not, so the
blockers found by running it are on the critical path rather than deferred to a
later release. Everything in §0.2 is therefore in scope.

**A package does not declare which OptPilot version it needs.** The field is
not worth its cost now; if it becomes necessary, versions released before it
existed will not understand it, which is accepted.

**Rough shape of the work**, with everything now decided: about a week to make
the packages ship and set themselves up; three to four days for the
correctness blockers; about a week for the first-hour experience; two days for
the documentation truth pass; about a week for the smallest honest Assistant;
plus publishing the software, which has never been done for Studio. Call it
four to five weeks of focused work, with the git-history purge as the only
strictly owner-side task.

### 0.3 What was verified as genuinely good

Worth stating, because it bounds the problem. All five packages validate
clean. **The documented command-line first run works exactly as written** on
a fresh archive, with no container engine and no model key: 2.9 seconds to a
successful result with the documented summary. Distribution hygiene is
correct — the published core contains no catalog, docs, or Studio content.
The compatibility fix flips exactly the eight intended pairings and nothing
else. The problem is not the engine. It is everything between a stranger and
the engine.

Every item below passed three tests:

1. **Would a first-time user meet it in their first hour?** If not, it waits.
2. **Is it small?** Marked **S** (hours), **M** (about a day), **L** (several
   days), or **owner** (only the owner can do it).
3. **Does it move toward the target, or would we rewrite it?** Anything that
   would be rewritten was cut, even when it looked cheap.

Section 5 states the rules that keep the fast path compatible with the slow
one. Those rules are the reason this scope can be trusted.

---

## 1. Gate A — nothing ships before these

These are not improvements. Each one either states something false to the
user, loses their work, or blocks the flagship path outright.

| # | Item | Size | Note |
| --- | --- | --- | --- |
| A1 | Purge the leaked credential/research tarballs from pushed git history, force-push, and request fork-object collection | owner | Blocks tagging only, not development. Everything else can proceed in parallel. |
| A2 | Cache the catalog listing | S | Measured live at 5–20 s cold, past the browser's 20 s timeout, so the catalog shows an error on first open. The ledger connection pooling and run-list caching that were feared missing **have already landed**; this is the one remaining piece. Measure before optimizing further. |
| A3 | Rebuild the run-setup list whenever the catalog loads, and show a retry notice on failure | S | Today one slow first load leaves "No Run setups yet." permanently — all 28 shipped run setups invisible, with no error. |
| A4 | Teach the compatibility check the policy-validation context | S | Studio's own context enumeration is missing what the core compiler has, so **every** flagship policy-search pairing reports "incompatible" and cannot be launched from the UI. Delegate to the core's enumeration and add a test that the reference pairing pairs. |
| A5 | Route "Open Candidate details" from the trial map through the loader | S | Currently renders a blank page. |
| A6 | Terminalize unrecoverable runs at startup; show a failed run's reason | M | Four runs displayed a live "running" badge; one had been silent 135 hours while still claiming the method "may still be preparing another Candidate." A failed run says "needs attention" without ever naming the failure. |
| A7 | Documentation truth pass | M | README's quick-start commands fail on a clean clone (they name a package that moved); installation says containers and command methods "are not yet executable" (both shipped); four pages still reference the retired job-shop tutorial; the site contradicts itself on whether COOPA is bundled (it is, Apache-2.0); nothing mentions approving an image. |
| A8 | Three security minimums | M | (a) refuse canonical catalog and archive folders as editable workspaces — today the Assistant can mount one and edit published bytes in place, with no registration lineage; (b) deny reads of known secret files through the file tools; (c) require encrypted transport when the model provider's address is not on this machine — today a raw provider key can be sent to a configured remote address in the clear. |

**A8 is deliberately small.** The larger hardening in the codex design — a
credential proxy, removing the agent runtime's direct filesystem access,
session and cross-site defenses on Studio's own endpoints — is real and
belongs in the roadmap, but it is not first-release-blocking for a
single-user tool bound to this machine. These three are, because each is a
credential or published-bytes exposure that a normal session can reach.

---

## 2. Gate B — the first hour

Gate A makes the release honest. Gate B makes it good. If schedule pressure
forces cuts, cut from the bottom of this table, not the top.

| # | Item | Size | Note |
| --- | --- | --- | --- |
| B1 | Give every component and run setup a human `name:` | S | Pure content, about 19 files, no code: the server already prefers `name` over the id. Today users browse "production-agv-scheduling-baselines" and the welcome page offers "Solve with coopa-solver". |
| B2 | Make search read the description, and match word by word | S | The search box reads a field the entries do not fill, so "simulator" matches 0 of the 11 entries whose descriptions contain it; and whole-phrase matching means "factory design" matches nothing. The server already implements the correct word-by-word semantics — mirror it. |
| B3 | Add a declared `tasks:` vocabulary and expand queries through a synonym table | M | "optimization", "solver", "layout" currently match nothing anywhere. An added settings field, not reused tags — the last rule in §5 says why. |
| B4 | Make the bundled packages launchable out of the box | L | **The single largest unlock.** On a fresh install nothing has taken the permanent copy of each shipped package that OptPilot requires before it will run anything, so every bundled package is unrunnable and nothing on screen says why. Needs the first decision in §4. |
| B5 | De-emphasize test fixtures and near-duplicate variants in the catalog list | S | Eight CI smoke configs and seven near-identical variants currently sit beside the flagships as equals. |
| B6 | Name the image and the exact command when a launch is refused for trust | S | The refusal is correct but arrives without the image reference or the remedy. Add the same line as a pre-launch warning. |
| B7 | Rebuild the Run page around the trace | L | Promote the trial map to the page's spine; selecting a trial fills the page below it with that trial's evidence; add a live panel for the running trial; fold the eight parallel views into the per-trial pane plus one record view; fix the silent 50-trial truncation and the "Planned" mislabel. |

**B7 can be staged.** A useful two-thirds — promote the map, fix truncation
and labels, add the live panel — is **M** and delivers most of the felt
improvement. The full fold-in of the eight views is the remaining **L** and
can follow the release.

---

## 3. Gate C — the smallest honest Assistant

Not the target Assistant. The smallest one that does not dead-end.

| # | Item | Size | Note |
| --- | --- | --- | --- |
| C1 | Resource-action tools: list, approval-gated run, status | M | The flagship conversation's first leg. Output is rooted in an attached workspace so the generated bundle is immediately registerable (rule 5). |
| C2 | Give existing refusals a machine-readable remedy | M | One shape carried by the refusals that already exist: image approval (with the command), missing launch inputs (with their declarations), validation failures (with the location), wrong front door (with the right tool). Refusals then teach their own fix instead of the guidance file reciting them. |
| C3 | An edit-copy tool for updating a catalog entry | M | The safe counterpart to A8(a): the Assistant gets the front door at the same moment the unsafe path closes. |
| C4 | Slim catalog listing for the Assistant | S | Today a bare listing call ships tens of kilobytes of raw settings into the first exchange. |
| C5 | Say plainly when the Assistant is off | S | Messages currently queue "locally" with no explanation and no path forward. |
| C6 | Give smoke tests their own permission, split from launches | S | They borrow the launch gate today, yet a smoke test is bounded by construction, and write → validate → smoke → fix is exactly where the repeated interruptions land. Highest friction relief per line of code in the whole plan. |

---

## 4. The two decisions only the owner can make

**Both were decided on 2026-08-16: set the ready-made packages up
automatically on first start, and ship the written instructions for the
mathematical-solver route.** The reasoning is kept below because the costs
named there are real and will matter again.

### Decision one: should OptPilot set up its own ready-made packages the first time it starts?

**The situation.** OptPilot ships with five ready-made packages — a factory
and vehicle scheduling simulator, a simulator generator, a solver for
operations-research problems, a design benchmark, and a policy-search method.
Each arrives as an ordinary folder of files.

Before OptPilot will run anything from a folder, it takes a numbered,
permanent copy of that folder's exact contents and keeps it forever. That
permanent copy is what lets someone say later, with proof, *this exact code
produced this exact result* — the copy never changes, even as the folder is
edited. Making that copy is a deliberate step; nothing does it by itself.

Nothing currently makes that copy for the five packages that ship with the
product. So a person installs OptPilot, opens it, picks the factory
simulator, and finds they cannot run it — with nothing on screen saying a
setup step is missing or how to do it. This is the largest single reason a
first session fails.

**The two ways to fix it.**

*Set them up automatically the first time OptPilot starts.* Everything works
immediately and the person never has to learn that this step exists. Packages
that ship with the product then behave exactly like packages the person makes
themselves, so there is only one way things work. The cost: OptPilot spends a
few seconds and some disk space storing those permanent copies before the
person has asked for anything in particular.

*Let a package run straight from its folder, with no permanent copy.* Nothing
is stored until the person asks for it. The cost: there are now two different
ways to run things, which have to be kept behaving identically forever; and a
result produced this way cannot point back to a permanent copy of the code
that made it — which weakens the product's central promise for precisely the
packages we ship as the examples of it.

**Decided: set them up automatically on first start.**

### Decision two: does the "solve it as a mathematical problem" route ship in the first release?

**The situation.** The product's headline story is: describe a system you want
to study, get a working simulator of it, then improve how that system runs.
There are two quite different ways to do the improving, and the product
contains both.

The first is search by trial and error: a language model writes a candidate
operating rule (for example, which patient a clinic serves next), the
simulator scores it, and better rules survive. This route works today, from
one end to the other.

The second is to restate the situation as a mathematical optimisation problem
and solve it exactly. This route does not connect to the first. There is no
way to turn a generated simulator into the problem statement the solver
expects, no written instructions for a person to do it by hand, and the two
documentation pages never mention each other.

**The two ways forward.**

*Write the missing instructions.* A short walkthrough showing a person how to
describe their generated system in the terms the solver expects, plus links
between the two pages. Costs a few hours. The route becomes possible by hand;
nothing becomes automatic.

*Do not claim the route yet.* Present trial-and-error search as the supported
way to improve a simulated system, and add the solver route in a later
release once it can be done properly.

**Decided: write the instructions.** They cost hours, and without them half
of the headline story is a claim with nothing behind it. Making the
connection automatic is a later feature, not a release blocker.

---

## 5. The forward-compatibility contract

These eight rules are what let the fast path stay compatible with the target
designs. Each one costs nearly nothing now and prevents a rewrite later.

1. **Add no durable Assistant state in v1.** No task file, no goal object.
   Nothing to migrate is the cheapest form of compatibility. When durable
   task state arrives, it extends Studio's existing coordination database —
   never a second store beside it.
2. **Every new tool returns the same envelope**: status, one-sentence
   summary, typed data, and an optional remedy (C2). Ad-hoc result shapes are
   the thing that gets rewritten.
3. **Approval cards show only values Studio resolved**, never strings the
   language model supplied. Standing grants will later match on exactly those
   values; a card built on model text cannot be upgraded safely.
4. **Every new tool joins the drift test.** Already enforced: advertised
   equals executable, and the guidance file teaches only real tools.
5. **Resource-action output is workspace-rooted from the first release**, so
   generate → register never needs a copy step and never has to be redone.
6. **The Assistant never writes a canonical catalog folder** — enforced by
   refusal (A8a), not by prompt wording. A habit built on the unsafe path
   becomes load-bearing.
7. **No skills registry in v1.** Package know-how ships as documentation.
   Playbooks arrive later as a validated settings kind, reusing registration
   and search rather than a parallel system.
8. **Task vocabulary is a declared field, not reused tags** (B3). One field
   then feeds search, the welcome page, and later playbooks; tags-as-magic
   would have to be migrated.

---

## 6. Suggested order

The order is chosen so the flagship demo path works as early as possible.

**First pass — all small, unblocks the demo:** A2, A3, A4, A5, B1, B2, B6.
After this, the catalog opens, run setups are visible, the flagship pairing
can be launched, names are readable, search works, and a trust refusal
explains itself.

**Second pass — honesty and safety:** A6, A7, A8, plus C4, C5, C6.

**Third pass — the unlock and the Assistant's first leg:** B4, B3, C1.

**Fourth pass — depth:** B7 (staged), C2, C3.

**In parallel, owner-only:** the git-history purge, before any release tag.
(Both product decisions in §4 are now settled.)

A release could ship after the second pass. It would be honest, navigable,
and useful, with an Assistant that answers and launches but does not yet
generate. Shipping after the third pass is the recommendation, because that
is where the product's distinguishing story — describe an outcome, get a
simulator, optimize it — becomes true.
