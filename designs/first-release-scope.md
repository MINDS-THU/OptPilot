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
| B3 | Add a declared `tasks:` vocabulary and expand queries through a synonym table | M | "optimization", "solver", "layout" currently match nothing anywhere. Additive schema field; see rule 8 in §5 for why this shape and not tags. |
| B4 | Make the bundled packages launchable out of the box | L | **The single largest unlock.** On a fresh install every bundled package is a local folder, so pairing and launching are blocked behind a per-package publish ceremony nothing explains. Either auto-register the bundled packages on first start, or let local-folder entries launch directly. Needs a decision (§4). |
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

**How bundled packages become launchable (B4).** Auto-registering them on
first start is the smoother experience and keeps one code path, but it writes
to the archive before the person has asked for anything. Allowing local
folders to launch directly avoids that but creates a second path that must
stay consistent with the registered one. Recommendation: **auto-register on
first start**, because it also gives every bundled package a version and
lineage from day one, which the target design assumes anyway.

**Whether the COOPA leg of the flagship story ships.** Nothing today connects
a generated simulator to the operations-research solver — no extraction, no
recipe, not even a cross-link between the two documentation pages. Either
descope the claim for this release, or add a short documented bridge (**S**:
a walkthrough section showing how to describe the generated system as the
solver's problem input, cross-linked both ways). Recommendation: **ship the
documented bridge, descope the automation.**

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

**In parallel, owner-only:** A1 before any tag; the two decisions in §4
before the third pass.

A release could ship after the second pass. It would be honest, navigable,
and useful, with an Assistant that answers and launches but does not yet
generate. Shipping after the third pass is the recommendation, because that
is where the product's distinguishing story — describe an outcome, get a
simulator, optimize it — becomes true.
