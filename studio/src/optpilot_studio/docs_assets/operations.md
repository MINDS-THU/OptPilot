---
title: Local Operations and Security
description: Trust boundaries, storage locations, filesystem support, cache integrity, and safe cleanup for local OptPilot use.
---

# Local Operations and Security

This page describes the current local implementation. It is intentionally not
a deployment guide for a shared or internet-facing service.

## Trust boundary

OptPilot Studio is a **single-user, local-host application**. It derives its
Realm actor from the OS user that started Studio. It does not implement user
accounts, tenant isolation, or a remote authorization boundary.

Studio and Code Server bind to `127.0.0.1` by default. Keep those defaults for
normal use. In particular:

- the main Studio HTTP server has no login or session authentication
- the main server does not validate `Origin`, `Referer`, or `Host` headers and
  does not issue or require a CSRF token
- not returning cross-origin-readable responses is not a CSRF defense; another
  browser origin may still be able to send a state-changing request
- Code Server also defaults to `auth: none`; enabling its password mode does
  not add authentication to the main Studio server
- Preview URLs use launch-scoped routing controls, but those controls are not a
  login layer for Studio

Use Studio only from a browser profile and local machine you trust. Do not bind
Studio or Code Server to `0.0.0.0`, publish their ports, or place them behind a
shared reverse proxy. A multi-user or remote deployment needs authentication,
request-origin/CSRF enforcement, TLS, and a separate authorization review; the
current server does not provide those controls.

The local settings file may contain Assistant credentials and values entered
under **Local environment variables**. Values are plaintext in that file.
Studio attempts to store it with mode `0600`, but it is not a secret vault.
Other processes running as the same OS user remain inside the trust boundary.
For retained Runs, the durable launch records only a variable name and opaque
Settings revision. The plaintext value is not copied into the Run, process
registry, or launch manifest; it is sent to a new Method worker over a
one-shot inherited channel.

Only one Studio process may supervise a given Studio start directory at a
time. Studio holds a crash-released POSIX lock for the lifetime of the process;
a second Studio started from the same directory exits before opening Realm,
recovering interface runtimes, or binding another HTTP port. This prevents one
process from treating another process's in-progress setup as orphaned work.
During shutdown, Studio keeps that claim and its Realm/coordination descriptors
open until launch workers, output watchers, and active Preview HTTP/WebSocket
handlers have actually returned. If a bounded shutdown wait expires, the
process retains ownership until exit instead of closing storage beneath a live
request.

Laptop sleep or another temporary process suspension can outlast the short
heartbeat lease used while an interface watches for generated outputs. When
the same Studio process resumes, it may replace that expired lease with a
higher fencing token only after rechecking its process-lifetime supervisor
claim and proving that the exact launch runtime is still live. The old writer
remains expired, and any capture interrupted by the suspension is recorded as
failed before the control file is retried. A Stop request, a different Studio
process, a missing ownership record, or a terminal runtime cannot use this
recovery path. Transient heartbeat and control-file metadata errors are retried
without silently ending output monitoring.

## Prepared-runtime cache integrity

Prepared caches improve repeat launches; they are not canonical source or Run
evidence. Cache keys include the exact source, setup declaration, provider and
platform identity, and cache format.

On every acquisition, OptPilot verifies the entry type, directory identity,
manifest, complete tree digest, file counts, and byte counts before reuse. An
invalid or incomplete entry is removed and rebuilt when no active lease owns
it. Active leases protect an entry from automatic eviction. Exact retained
Environment and Method dependencies are additionally captured into the Realm
and checked against the expected retained tree before a Run uses them.

Cache directories are private and cached payloads are made read-only. These
controls catch stale or accidental mutation; they do not isolate OptPilot from
a malicious process running as the same OS user. The current contract trusts
same-user host processes not to race a cache mutation after verification and
before or during use. A read-only container mount does not close that host-side
race.

Each prepared cache currently keeps at most 16 entries and 4 GiB by default.
It prunes least-recently-used, unleased entries during normal acquisition and
release and recovers incomplete entries when reopened. There is no supported
**Clear cache** button or public cache-maintenance command.

## Supported platform and filesystem boundary

| Area | Current release boundary |
| --- | --- |
| Python | Package metadata declares Python 3.10 or newer. Automated CI currently exercises 3.10, 3.11, and 3.12; newer interpreters are not in that matrix. |
| Operating system | The retained local Realm, local process runner, prepared cache, and Studio runtime require POSIX filesystem/process primitives. Linux is exercised in CI. macOS code paths exist but still require the release manual gates. Windows is not supported by this retained local runtime slice. |
| Realm/cache filesystem | Use one local filesystem with working POSIX `flock`, hard links, private permission modes, directory `fsync`, and atomic same-filesystem rename. Device/inode identity must remain stable during each live attachment or operation, but may change across a normal unmounted/remounted launch boundary. The root must be a real directory, not a symlink. |
| Container features | Workspace Code Server and container-backed Preview require a working Docker/Podman-compatible engine. Candidate **Environment Preview** additionally runs only an explicitly trusted image. |
| Network, distributed, or synchronized storage | NFS/SMB-style mounts and cloud/file-provider synchronized folders are not supported locations for the Realm, prepared caches, or Studio runtime state. Their lock, inode, permission, and rename behavior is not covered by the release tests. |

On macOS, OptPilot narrowly ignores two known OS-maintained file-provider
metadata attributes while sealing content, and conditionally accepts Docker
Desktop ownership metadata after validating its full permission mode. This
does **not** make a synchronized folder a supported operational-data location.

Studio keeps its high-write coordination SQLite database, Workspace index, and
process-lifetime supervisor lock in a deterministic per-project directory under
the OS-local Realm. The Studio start path is used only to derive an opaque
project key. A small checkout-local compatibility lock is also held while
Studio runs so a pre-upgrade process cannot remain active during migration; it
does not contain coordination state.
Workspace runtimes, interface output folders, logs, and prepared runtime caches
also live under the Realm. Settings, job and Assistant-session records, and
Studio-owned draft or editable-copy folders remain below `.optpilot-ui/` in the
Studio start directory, and Workspace or project content remains at its existing
owned location.

This split avoids putting SQLite and lock traffic into a synchronized checkout,
but it does **not** make synchronized source folders universally supported. A
configured Catalog source or externally connected folder may point elsewhere,
but OptPilot treats it as mutable input and may reject capture if it changes.
A synchronized source is not a durability or concurrency guarantee; prefer a
local checkout for Check, registration, and setup work. For the strongest
supported boundary, keep the remaining checkout-local Studio state on a local,
non-synchronized filesystem too.

Realm projection and writable-volume roots and namespaces, Realm-managed
editable Workspace checkouts, and each Studio workspace-runtime directory carry
a private nonce-bound claim marker. That marker is the durable cross-launch
ownership identity. Device and inode observations are recaptured when a process
attaches and remain strict fences for that live attachment and for cleanup. A
missing, malformed, or mismatched claim is rejected and left untouched;
OptPilot does not silently adopt or migrate unclaimed runtime state.

### Filesystem identity classification

OptPilot uses filesystem observations in four deliberately different ways:

| Use | Cross-launch rule |
| --- | --- |
| Durable provider ownership | Realm projection and writable-volume roots and namespaces, editable Workspace checkouts, Studio workspace runtimes, and retained Environment Preview control layouts are selected by exact nonce-bound claims. A new service or manager validates the claim and captures current descriptor observations. |
| Active transaction fences | Local-process launch locks and UNIX sockets, plus namespace retirement and cleanup proofs, retain device/inode facts only to finish the exact process or cleanup transaction without following a replacement path. They are not Workspace or Realm-object identities and fail closed if the filesystem changes while that transaction is active. |
| Rebuildable cache validation | A prepared-runtime cache manifest records its payload observation as one cache-integrity input. A mismatch makes the entry a cache miss; it may be rebuilt once no active lease remains. |
| In-process path safety | Content-store handles, the Studio coordination database, and interface-output enumeration/capture compare descriptor observations only during the current process or operation. Those observations are not persisted as ownership authority. |

This distinction is intentional: a claim proves *which managed namespace* a
new process may attach to, while device/inode observations prove that a path
did not change *during that attachment or transaction*.

## Where local data lives

### Realm-owned data

Unless `OPTPILOT_REALM_ROOT` or the CLI `--realm-root` selects another private
root, Core and Studio use:

- macOS: `~/Library/Application Support/OptPilot/realm`
- Linux: `$XDG_DATA_HOME/optpilot/realm`, or
  `~/.local/share/optpilot/realm`

The code can calculate a Windows user-data path, but that does not make the
current POSIX retained runtime Windows-supported.

For an isolated local Studio, choose both a non-synchronized start directory
and a dedicated local Realm before launch:

```bash
cd /absolute/local/non-synchronized/OptPilot
OPTPILOT_REALM_ROOT=/absolute/local/optpilot-realm \
  uv run optpilot ui --host 127.0.0.1 --port 8765
```

For one isolated CLI run, pass the same kind of dedicated root explicitly:

```bash
uv run optpilot run path/to/package/studies/study.yaml \
  --package-root path/to/package \
  --realm-root /absolute/local/test-realm
```

The Realm root contains one authority database and provider-private storage:

| Relative location | Purpose |
| --- | --- |
| `authority/` | Canonical Realm ledger and identities. |
| `content/` | Immutable content-addressed blobs and trees. |
| `editable-workspaces/` | Realm-managed editable Workspace checkouts. |
| `projections/` | Rebuildable read-only or execution views. |
| `volumes/` and `processes/` | Private attempt/runtime state and supervision records. |
| `retained-dependency-cache/` | Exact offline Python dependency preparation for Environments and Methods. |
| `runtime-cache/studio-prepared-runtimes/` | Reusable prepared output for eligible Studio interfaces. |
| `runtime-cache/studio-workspace-runtimes/` | Per-Studio Workspace containers, interface launch roots, logs, control files, and temporary generated outputs. |
| `studio/projects/<opaque-project-key>/` | Per-project Studio coordination database, Workspace index, and runtime-supervisor lock. |
| `container-web/` | Provider control state when container Preview is enabled. |

These are implementation directories, not public APIs. Do not edit, copy
individual files from, or partially delete them. Use Studio and Realm services
to read canonical Runs, Catalog revisions, and Workspaces.

### Environment Preview image approvals

An Environment Preview container can execute package interface code. OptPilot
therefore requires an explicit approval for the exact digest-pinned image and
gateway contract before it can launch. Tags such as `latest` are not accepted.

Approve an image in the same Realm used by Studio:

```bash
uv run optpilot environment-preview trust approve \
  registry.example/preview@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  --realm-root /absolute/local/optpilot-realm
```

The command asks you to type `APPROVE`. Automation and other noninteractive
sessions must make the decision explicit with `--yes`. List or revoke approvals
with the corresponding commands:

```bash
uv run optpilot environment-preview trust list \
  --realm-root /absolute/local/optpilot-realm
uv run optpilot environment-preview trust revoke \
  registry.example/preview@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  --realm-root /absolute/local/optpilot-realm
```

`approve`, `revoke`, and `list` also accept `--json`. Approval decisions are
stored in the selected private Realm and survive process and Studio restarts.
Replace the example image and digest with the exact reference declared by the
Environment, then restart Studio so its startup trust snapshot is refreshed.

Approval records permission to execute the exact image; it does not install
the image. Studio checks the local container inventory before creating an
Environment Preview job and offers **Approve & download** or **Download image**
as appropriate. For CLI-only provisioning, install the same pinned digest
explicitly:

```bash
docker pull registry.example/preview@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
```

The execution provider continues to use `--pull never`. A Preview launch
therefore performs no implicit registry access and cannot silently substitute
a tag or a different image generation.
The `--realm-root` value must be absolute and must identify the same Realm that
Studio opens; omitting it uses the normal secure per-user default.

The existing Studio `--environment-preview-trusted-image` option and
`OPTPILOT_ENVIRONMENT_PREVIEW_TRUSTED_IMAGES` fallback remain available for
compatibility. Supplying either selects an exact session-only trust set for that
Studio process; it does not update or combine with the Realm's persistent
approvals. Prefer the persistent commands for routine operation so a missing
startup flag cannot unexpectedly disable an interface.

Studio's `--environment-preview-trust-source` switch makes the source
explicit: `realm` uses only persistent approvals, `session` uses only the
CLI/environment list, and `disabled` trusts no Preview image. The default
`auto` selects a session override only when one was supplied; otherwise it
uses the Realm. OptPilot never unions the two sets.

### OS-local Studio project state

For a production Studio with a Realm, the following operational files live
below `<realm-root>/studio/projects/<opaque-project-key>/`:

| Relative location | Purpose |
| --- | --- |
| `studio-coordination.sqlite3` | Transactional Study drafts, launch requests, candidate tries, interface-launch ownership, and related Studio coordination records. |
| `workspace-index.json` | Index of Studio-visible Workspace references; moving this index does not move any Workspace folder. |
| `runtime-supervisor.lock` | Process-lifetime ownership claim for this Studio start directory. It is retained between launches and must not be deleted while Studio is running. |

The project key is derived deterministically from the canonical Studio start
path, so the same project selects the same local directory without exposing its
path as a directory name. On first use, when the local target does not yet
exist, Studio validates and non-destructively copies a valid legacy coordination
database and Workspace index from `.optpilot-ui/`. The copy includes committed
SQLite WAL state and is validated before atomic publication. Existing local
targets always win; legacy database and index files are not renamed, truncated,
deleted, or merged. The authoritative runtime-supervisor lock is created at the
new local location rather than adopted from a previous process. For upgrade
safety, the same process also holds the legacy advisory lock for its lifetime;
this excludes an older Studio that knows only the checkout-local lock.

### Checkout-local Studio data and Workspace content

Studio still stores the following below
`<studio-start-directory>/.optpilot-ui/`:

| Relative location | Purpose |
| --- | --- |
| `settings.json` | Local Assistant, environment-value, and UI settings. |
| `jobs/`, `sessions/`, and `agent_sessions/` | Studio job and Assistant coordination records. |
| `workspaces/` | Studio-owned draft and editable Catalog-copy folders. |
| `code-server/` | Local Code Server profile and process state. |
| `runtime-supervisor.lock` | Compatibility-only advisory lock held alongside the authoritative OS-local lock while Studio runs. |

Legacy `studio-coordination.sqlite3`, `workspaces/index.json`, or
`runtime-supervisor.lock` files may remain in this directory after an upgrade.
Once the OS-local project state exists, those retained legacy files are not the
production authority and should not be edited as a way to change Studio state.

An interface launch receives a private directory below the Realm's
`runtime-cache/studio-workspace-runtimes/` namespace with separate runtime,
control, log, and `interface-outputs` areas. Unsaved generated outputs remain
there only for that launch. **Save as Workspace** gives the selected sealed tree
durable editable Workspace ownership; it does not make the whole launch
directory durable.

Configured Catalog source folders and folders linked with **Link local folder**
remain user-owned at their original paths. Removing their Studio reference must
not delete those folders. Realm-managed editable Workspaces remain under the
Realm's `editable-workspaces/` area. Relocating only the Workspace index does not
copy or relocate any of these folders. Container images and engine-level caches
live in the container engine, outside both storage roots.

## Safe cleanup that exists today

Use the product controls while Studio is running:

| What to reclaim | User action | Result |
| --- | --- | --- |
| Live interface runtime and unsaved generated output | Click **Stop** on the interface. Use **Save as Workspace** for wanted generated outputs when prompted, or choose **Stop without saving**. A slow setup may first show **Stopping**; wait for its current bounded command to reach a safe boundary. If the card later reports cleanup pending, click **Retry cleanup**. | Studio never deletes launch storage while setup/startup code or a Preview request can still use it. After the worker and Preview handlers quiesce, Studio proves the process/container and any prepared-runtime builder stopped, retires the launch output session, releases its borrowed source/cache handles, and removes launch-scoped storage. Saved Workspaces remain. |
| Realm-managed editable Workspace | Detach it from Assistant sessions, stop its interface if one is live, then use **Delete Workspace**. | Studio deletes its private checkout, retires the Workspace, and releases that Workspace's content memberships. A Catalog version registered from it is unchanged; shared immutable objects are not promised to disappear immediately. |
| Studio-owned draft or editable Catalog copy | Stop its interface, then use **Delete Draft** or **Delete Copy**. | Studio removes its owned folder and runtime state. The original Catalog source/version is unchanged. |
| Externally owned local folder | Use **Remove From Studio**. | Only the Studio reference and owned runtime state are removed; the folder remains on disk. |
| Saved Study draft | Open the draft's **More** menu and choose **Discard draft**. | The draft is removed; existing Runs are unchanged. |
| Idle Workspace container | Leave it unreferenced until the configured idle timeout. | Studio's runtime health reconciliation stops the container; it does not delete Workspace files or caches. |

Stopping Studio also asks its transient interface launches to stop and reconciles
orphaned launch runtimes the next time Studio starts. A `cleanup pending` state
means cleanup was not proven; retry it instead of manually deleting the path.
Browser launch diagnostics expose only bounded, no-follow regular-file log
tails. Studio removes launch-private paths and values declared through
`secretsFromHost`; ordinary `envFromHost` values are not treated as secrets.
Very short declared secrets may cause an affected diagnostic string to be
withheld entirely rather than risk disclosure.

### Deleting a chosen Run

A finished Run's record can be deliberately deleted from a terminal:

```
optpilot runs list
optpilot runs delete <run-id>
```

Deletion erases the Run's results, code snapshots, and history, then reclaims
whatever stored bytes only that Run kept alive; bytes shared with anything
else — another Run, a saved review decision — are never touched. A note stays
in the Run's place naming what was removed and when, so a deleted Run is
never mistaken for one that never existed. There is no undo and no flag that
skips confirmation: the command refuses to run non-interactively, and the
confirmation is retyping the Run id. The command ends by naming any container
images that no remaining record references; removing those from Docker or
Podman is a separate manual step (`docker rmi …`), never automatic.

There is currently no whole-Realm
reset, prepared-cache clear, or container-image cleanup. Stopping a Run stops
work but intentionally preserves its evidence. Do not reclaim those areas by
deleting internal subdirectories. For disposable tests, select a dedicated
absolute Realm with `--realm-root`; after every OptPilot process using it has
stopped, an administrator can discard that entire dedicated root as one unit
with the operating system's file manager or normal filesystem tools.
Never apply that procedure to the default Realm unless losing all of its Runs,
Catalog publications, Workspaces, and retained content is intentional.
