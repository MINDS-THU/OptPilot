# How the OptPilot Assistant works — the target design

Written to be read without prior knowledge of OptPilot; every term is defined
where it first appears.

**What is described here.** The Assistant's architecture, its tool surface,
and its safety model (§§1–3, §6) exist today. The goal it serves — every
capability a package or OptPilot itself provides is exposed to the Assistant
as a tool, so a person can work through conversation as fully as through the
menus — is partly built; §§4–5 mark each gap where it appears, and §7 collects
them as the build order. Dated 2026-08-16.

## 1. What the Assistant is

OptPilot runs optimisation experiments: **packages** hold **environments**
(code that scores a proposed solution), **methods** (code that proposes
solutions), **resources** (supporting tools, such as a simulator generator
with a web interface), and **run setups** (a ready pairing of one environment
with one method, plus an objective and a budget). The **catalog** is the set
of packages available on this machine, and **Studio** is the local web
application for browsing and running all of it.

The Assistant is a conversation inside Studio. A person describes an outcome
— "optimize my factory layout", "generate a simulator of my clinic", "make
this method stop wasting trials" — and the Assistant finds the right pieces,
runs them, or builds new ones. It is the front door for people who would
rather say what they want than learn where every button is; the design goal
is that nothing reachable through the menus is out of its reach.

It is built from two halves that never blur:

- **The reasoning engine** is a language-model agent runtime (OpenHands),
  running as a separate local process called the agent server. It decides
  *what to do next* and writes the words the person reads.
- **The hands are Studio's.** The reasoning engine cannot touch files, the
  catalog, or a run directly. Every action is a named **tool** that Studio
  executes on the engine's behalf, under Studio's own permission checks, with
  the result recorded in the conversation. If a tool does not exist, the
  action cannot happen — which is exactly why the tool list *is* the
  Assistant's capability surface, and why this document is mostly about it.

## 2. How one exchange flows

1. The person types a message on any Studio page.
2. Studio wraps it with a **context packet**: which page is open, the selected
   run or catalog item, the workspaces attached to this conversation, and a
   catalog summary. The Assistant answers about what the person is looking
   at, not in a vacuum.
3. The agent server receives the message, packet, the **guidance file** (a
   system prompt Studio ships, telling the model how OptPilot wants it to
   behave — for example: answer a broad question after at most one catalog
   inspection, propose exactly one next action, never dive into building
   things unasked), and the **tool list** with each tool's parameters.
4. The model replies with words, tool calls, or both. Studio executes each
   tool call — asking the person for approval first when the tool requires it
   (§6) — and returns the results. This repeats until the model sends a plain
   final message.

Two operational facts worth knowing. The agent server caches the tool list
per process, so after Studio's tools change, the agent server must be
restarted or every message errors. And when the agent runtime is not running
at all, messages are stored and marked "queued locally" — honest, but a dead
end for the person; the composer should say plainly that the Assistant is off
and how to start it. *The plain-language off-state notice is not built yet.*

## 3. The tools, by verb

Every tool below exists and executes today. A test enforces the two honesty
rules this table depends on: every tool advertised to the model is executable
(an advertised-but-dead tool hangs the conversation), and the guidance file
teaches only tools that exist.

| Verb area | Tools | What they do |
| --- | --- | --- |
| Conversation | `optpilot_conversation_title` | Name the conversation. |
| Discovery | `optpilot_catalog_list`, `optpilot_catalog_detail`, `optpilot_docs_search`, `optpilot_capability_list`, `optpilot_capability_detail` | Search and inspect catalog entries, documentation, and platform capabilities. |
| Pairing | `optpilot_compatibility_check` | Whether an environment and a method can work together. |
| Workspaces | `optpilot_workspace_list/create/attach/detach/focus` | Manage the editable folders a conversation may read and write. |
| Files & code | `optpilot_file_tree/read/write/diff`, `optpilot_file_editor`, `optpilot_shell_run`, `optpilot_terminal`, `optpilot_workspace_preview_open` | Read, write, and run code inside attached workspaces. |
| Configuration | `optpilot_config_discover`, `optpilot_config_validate` | Find and check OptPilot settings files in a workspace. |
| Registration | `optpilot_package_plan_prepare/update/validate/smoke/apply` | The whole path from a workspace to a published catalog version (§5). |
| Run setups | `optpilot_study_draft`, `optpilot_study_save`, `optpilot_study_launch` | Draft, save, and launch a run setup. |
| Runs | `optpilot_run_list`, `optpilot_run_detail`, `optpilot_run_compare`, `optpilot_job_stop` | Inspect progress and results; stop a run. |
| Testing | `optpilot_smoke_test_study` | Run a bounded no-cost check of a run setup. |

**About "skills."** OptPilot has no separate skill system, and this design
deliberately does not add one. The equivalent notions already have homes: the
guidance file carries standing behaviour; the onboarding registry turns
installed capabilities into suggested opening actions on the welcome page;
and each package's own documentation is reachable through the documentation
search tool, so a package that ships a good walkthrough has, in effect,
taught the Assistant its recipe. Keeping knowledge in packages and behaviour
in one guidance file avoids a second registry that could drift from both.

## 4. Using what a package provides

A package's functionality is whatever its components declare, and each kind
of declaration maps to a conversation the Assistant should be able to carry:

**"What can I use for X?"** — catalog search plus detail. Works today, with
two known weaknesses that gate the seamless feeling: the listing tool returns
each entry's full raw settings (tens of kilobytes into the first exchange —
*the slim id/name/description/tags listing is not built yet*), and the
bundled packages lack the task vocabulary people actually type ("optimize",
"solver", "layout"), so first-attempt searches often return nothing.

**"Run it for me."** — draft or pick a run setup, fill its per-launch inputs
(a run setup may declare typed inputs such as a problem statement or a task
id), check the pairing, and launch — with the person approving the launch
(§6). Built and working. One correctness caveat inherited from Studio: the
pairing check currently fails to recognize one kind of environment
declaration (policy validation), so it wrongly reports the flagship
generate-and-optimize pairing as incompatible; the Assistant repeats that
wrong answer. *The pairing fix is queued.*

**"Generate something with the package's tool."** — resources declare
**actions**: named, typed operations such as the simulator generator's
"generate" (give it a description, get back a complete simulator bundle).
Studio's browser pages can run these; *the Assistant tool for resource
actions is not built yet*, so today the conversation dead-ends with "open the
Resource page and run it yourself." The design mirrors the launch tool
exactly: a listing tool (which actions does this resource declare, with what
inputs) and an approval-gated run tool. This single addition makes the
flagship scenario — "generate a simulator of my shop, then optimize it" —
carryable end to end in conversation.

**"Open its console."** — some components declare browser interfaces (the OR
solver's review-and-approve console). These are for humans by design; the
Assistant's job is to launch/point to them, not to drive them. No tool is
planned, deliberately.

## 5. Creating, updating, and registering packages

This is where the Assistant is a builder, not a librarian. The crucial design
property: **the Assistant writes through the same gates as a person.**
Everything it creates goes through the same Check, the same validation, the
same registration lineage — nothing it does can place unchecked bytes in the
catalog.

**Creating from scratch.** The path is: create a workspace (an editable
folder Studio manages) → write the component code and settings files with the
file tools → validate the configurations → prepare a **package plan** (which
components, which files, where they land in the catalog) → **Check** it
(validation seals the exact bytes and runs every static check) → optionally
smoke-test it (a bounded real execution) → **apply** (register the checked
version to the catalog). Every step has a tool today, and the destructive or
costly ones require approval. When Check detects that software was installed
in the workspace since it started, the capture question — record the captured
image on just the registered components, or the whole package — is exposed on
the plan tools, and the guidance file tells the model to ask the person
rather than choose silently.

**Writing code the user wants.** The same file and shell tools serve ordinary
programming: "write me an evaluator that scores my CSV of layouts", "add a
constraint to this method". The workspace is the sandbox; shell commands
require approval by default; and the validation loop (config validate → plan
Check → smoke) is the safety net that turns "the model wrote plausible code"
into "this exact code passed the same checks everything else in the catalog
passed."

**Updating an existing package.** *The safe front door is not built yet.*
Studio's browser has an edit-copy flow: it copies a catalog entry into a
workspace, remembers where the copy came from, and refuses re-registration if
the original moved meanwhile (so a stale edit can never silently overwrite
newer work). The Assistant cannot invoke that flow — and worse, its generic
workspace tool will happily mount the canonical catalog folder itself,
letting it edit published bytes in place with no lineage at all. The design
closes both: an edit-copy tool that returns the attached workspace, and a
refusal of canonical catalog paths in workspace creation.

## 6. Trust and safety

Per-conversation permissions govern the five side-effect domains — file
writes (allowed only in attached editable workspaces), shell commands,
catalog registration, run launches, and run stops. Each defaults to
requiring the person's explicit approval in the conversation, each can be
tightened to disabled, and the approvals are Studio's own: the reasoning
engine cannot grant itself anything.

Some acts are deliberately not tools at all. Deleting a run's record is a
person's typed act at a terminal, never an Assistant action. Approving a
container image for execution — the act that says "I am willing to run this
software" — is likewise reserved to the person; the Assistant's role is to
recognize the refusal and answer with the exact command to run
(`optpilot image approve <reference>`), which the guidance file now teaches.
*A read-only tool listing current image approvals — so the Assistant can show
trust state rather than only recite the command — is not built yet.*

## 7. What remains, in build order

1. **Resource-action tools** (list + approval-gated run) — unlocks the
   flagship generate-and-optimize conversation.
2. **Edit-copy tool + canonical-root refusal** — gives "update my package" a
   safe front door and closes the in-place-edit hole.
3. **Slim catalog listing + task-vocabulary tags** — makes first-attempt
   discovery work in conversation and in the browser alike.
4. **Read-only image-approvals tool** — the Assistant shows trust state.
5. **Plain off-state notice** when the agent runtime is not running.
6. Inherited from the Studio review, not Assistant-specific but felt through
   every conversation: the pairing-check fix and the bundled-package
   publish/launch story.
