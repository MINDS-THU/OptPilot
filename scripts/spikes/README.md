# Disposable architecture spike

These modules are executable design probes for the mandatory pre-production
gate in `resource/design/experiment_runtime_workspace_and_interaction_design.md`.
They are intentionally outside `src/optpilot`, are not public APIs, and must not
be imported by Core or Studio. Production work must implement the proven
contracts cleanly rather than promote these modules in place.

| Gate | Probe | Focused test |
|---|---|---|
| Realm transaction and recovery | `realm_ledger_spike.py` | `tests/test_realm_ledger_spike.py` |
| Deterministic tree seal, efficient provider, immutability, and GC race | `tree_snapshot_spike.py` | `tests/test_tree_snapshot_spike.py` |
| Portable binding, isolated Preview, and one package digest | `runtime_binding_spike.py` | `tests/test_runtime_binding_spike.py` |
| Restart-safe Operator Job and study handoff | `operator_job_spike.py` | `tests/test_operator_job_spike.py` |
| Multi-terabyte remote retain/hydrate/export contract | `remote_content_spike.py` | `tests/test_remote_content_spike.py` |

Run the complete gate with:

```bash
python3 -m unittest discover -s tests -p 'test_*_spike.py' -v
```

The current suite has 77 tests. It includes hard subprocess death, concurrent
SQLite writers and reconcilers, deterministic capture/adoption-versus-GC
interleavings, a strict real `fclonefileat` path on APFS, native and container
bindings, credential-audience rejection, package artifact substitution, and a
virtual 3 TiB remote export. APFS-only assertions skip on unsupported hosts;
verified-copy remains mandatory everywhere.

## Decisions proved by the spike

- One realm transaction is the commit authority for run revision, sequence,
  budget, handle state, domain rows, and owner content membership. Controller
  handoff is holder-bound, fenced, and idempotent.
- A content object is protected provisionally before it becomes visible. GC
  rechecks protection and tombstones under the same lock as capture/adoption.
- Tree identity is provider-independent. Optimized clone and verified-copy
  providers must produce the same canonical manifest and immutable result.
- Persisted run identity contains logical scopes and immutable refs, never
  provider paths. Concrete native/container paths exist only in an ephemeral
  binding.
- Preview consumes a sealed terminal selection, allocates a fresh upper, and
  receives only Preview-audience secrets and grants.
- Package prepare, validation, smoke, and apply consume one pinned immutable
  artifact; no phase silently rebuilds or recopies source.
- External admission/backend side effects require durable tokens and
  discoverable idempotent adapters. Study launch transfers the same admission
  lease from job to run before controller heartbeat completes the job.
- Remote identity is content-derived and location-independent. Provider facts,
  not config flags, determine availability, range support, transfer cost, and
  retention.

## Deliberate limitations

- The RealmLedger probe uses fake content identities and a spike schema. It is
  evidence for the transaction contract, not the production migration/schema.
- The tree probe uses conservative on-disk protection markers and one advisory
  store lock rather than the production RealmLedger API. It has no TTL reaper,
  quotas, chunked objects, remote backing, or hostile same-user tamper defense.
- The runtime probe uses synthetic paths and in-memory authorities. It launches
  no process/container, creates no real overlay, and has no transactional
  catalog backend.
- The Operator Job probe assumes a backend can discover an execution by its
  stable token. Direct `subprocess.Popen` plus a PID written afterward cannot
  satisfy this contract; a production local adapter needs a durable supervisor
  or independently discoverable process/container labels.
- The remote probe authenticates deterministic virtual segment content so a
  multi-terabyte fixture requires no multi-terabyte allocation. It does not
  exercise a real network, credential provider, persistent owner registry, or
  physical full export.

No current run, catalog, workspace, or Studio path uses these files. The next
production step starts at the WP4A authority boundary and keeps the current
batch path single-authority at every cutover.
