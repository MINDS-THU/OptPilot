# The first official release — what ships, what waits

This is a **scope document**, not an architecture. Two target designs already
exist and remain the destination: `designs/how-the-assistant-works.md` and
`designs/how-the-assistant-works-codex.md`. This document answers a different
question: *what is the smallest amount of work that produces a release we are
not embarrassed by, and does not have to be undone later?* Dated 2026-08-16.

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

**Recommendation: set them up automatically on first start.**

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

**Recommendation: write the instructions.** They cost hours, and without them
half of the headline story is a claim with nothing behind it.

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

**In parallel, owner-only:** the git-history purge before any release tag;
the two decisions above before the third pass.

A release could ship after the second pass. It would be honest, navigable,
and useful, with an Assistant that answers and launches but does not yet
generate. Shipping after the third pass is the recommendation, because that
is where the product's distinguishing story — describe an outcome, get a
simulator, optimize it — becomes true.
