---
title: OptPilot Studio
description: The local GUI for browsing packages, launching studies, opening workspaces, and inspecting runs.
---

# OptPilot Studio

OptPilot Studio is the local web UI for OptPilot. It is included in the full
source checkout, not in the PyPI core package.

Use Studio when you want to:

- browse package environments, methods, resources, and studies
- inspect read-only catalog source
- create managed editable workspaces when you intend to change source
- configure and launch studies from forms
- inspect run metrics, trials, candidates, events, runtime logs, and artifacts
- manage declared local environment values used by later launches; these are
  stored as local settings, not in a secret vault
- use the optional OpenHands-backed OptPilot Assistant

## Start Studio

First install the full source checkout from [Installation](installation.md).

Then run:

```bash
uv run optpilot ui --open-browser
```

The default URL is usually:

```text
http://127.0.0.1:8765/
```

Studio scans packages under `catalog/` by default. The bundled tutorial package
is `catalog/example_package/`.

## First 10 Minutes In Studio

Start with this path:

1. Open **Catalog** and inspect the bundled job-shop environments and methods.
2. Open **Studies** and select `job_shop_rule_parameters_baseline.yaml`.
3. Inspect the environment, method, and study forms.
4. Launch the dependency-free baseline study.
5. Studio opens the new **Run** directly. Use its Overview and Candidates pages
   to monitor progress, metrics, constraints, and the best eligible Candidate;
   technical trials, attempts, observations, artifacts, and the evidence
   timeline are under **More**.
6. Open a Candidate. Use **Inspect** or **View files** without creating a
   Workspace, and choose **Run headless** or **Open interactive interface** when eligible.
7. Compare Candidates and use **Save to Shortlist** to keep promising choices,
   notes, and an optional completed inspection result inside this Run.
8. Choose **Edit in Workspace** only when you want an eligible complete project
   to become durable editable work. For Catalog source, use **Edit in
   Workspace**; for a configured mutable source, use **Open local folder**.
9. In the Workspace's **Setup** view, follow **Check files to register**,
   **Run optional test** or **Run required test** when shown, and **Register
   checked version**.
10. Use **Settings** to configure the assistant runtime and declared local
    environment values. Values are stored on this machine and are not copied
    into Run evidence; Studio Settings is not a secret vault.

![Studio catalog workflow](assets/studio-catalog.png)

_Captured from the current Studio source checkout with `catalog/example_package/` loaded._

## Main Views

| View | What it is for |
| --- | --- |
| Catalog | Browse reusable environments, methods, and resources from packages. |
| Studies | Edit and launch concrete study plans, including study YAML files shipped with packages. |
| Runs | Inspect completed and running study evidence. |
| Workspaces | Open editable projects and connected local folders. Read-only Catalog source and Runs do not appear here. |
| Settings | Configure assistant settings and declared local environment values for future launches. |

Configured filesystem sources are mutable imports. Realm catalog revisions are
immutable. Studio creates an editable workspace only when the user asks to edit
or preserve a project; inspection and interface launch use read-only source plus
transient runtime state.

## Catalog And Workspace Actions

Catalog entries may expose these actions:

| Action | Behavior |
| --- | --- |
| View source | Opens the registered version read-only without adding it to Workspaces. |
| Edit in Workspace | Creates or reopens one editable Workspace for intentional changes. |
| Open interface | Starts the declared interface over read-only source and temporary launch storage. Prepared dependencies may be reused. |

To use a registered Environment or Method, open **Studies**, choose compatible
components, set the goal and budget, then choose **Launch run**.

A configured source card is labeled **Local source · Mutable**. **Open local
folder** connects that existing folder as one editable Workspace without
copying it and opens the same Workspace Setup flow used everywhere else.

Selecting a Run opens its recorded evidence directly; there is no public
“Open as Workspace” step. Candidate **Inspect** reads semantic inputs, and
**View files** is a short-lived bounded file view. **Edit in Workspace** creates
or reopens durable editable work only for an eligible complete project.
**Save to Shortlist** keeps a decision and its note inside the source Run
without creating a Workspace or runtime.

When comparing file candidates, Studio first shows the sealed manifest changes.
Use **View text diff** on one added, removed, or changed file to inspect its
contents. This is a direct, read-only operation over the two exact retained
candidate trees: it does not copy either candidate into a workspace. Text diffs
are intentionally bounded to strict UTF-8 files no larger than 48 KiB and 4,000
lines per side. If a complete diff cannot be returned, Studio explains why; use
**View files** for broader manual inspection.

The **Shortlist** tab appears after its first Candidate is saved. Edit the
Shortlist name, Candidate notes, membership, and order, then choose **Save
changes**. **More** contains saved history, export, and **Delete Shortlist**.
Viewing an earlier saved version does not discard unsaved edits to the current
one, and deleting a Shortlist never deletes its source Run.

When **Run headless** or **Open interactive interface** finishes, the result offers **Save
inspection to Shortlist** for an already-saved Candidate or **Save Candidate
and inspection** otherwise. This preserves bounded terminal results, not the
live process or interface.

For the package model behind these entries, see
[Packages and Catalogs](catalog.md).

## Platform Status

The Studio sidebar reports the local services it can see:

| Service | Meaning |
| --- | --- |
| Studio | The local UI server is reachable. |
| Code Server | The embedded editor for the selected workspace is reachable. |
| OpenHands | The assistant runtime is configured and reachable. |
| Sandbox | The workspace container runtime is available. |

Not every workflow requires every service. Browsing the catalog and launching
CLI-style studies only require Studio and the core runner. Editing workspaces in
the embedded editor requires Code Server. Assistant tool execution requires
OpenHands and a workspace runtime.

## Related Pages

- [Workspace Management](studio-workspaces.md): editable copies, Code Server,
  previews, and workspace containers.
- [OptPilot Assistant](assistant.md): OpenHands setup, assistant settings,
  tools, approvals, and local credential handling.
- [Installation](installation.md): why Studio is source-checkout only.
