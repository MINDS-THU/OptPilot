# Source snapshot

This versioned Resource starts from OptPilot's upstream
`catalog/devs_gallery/resources/devs-gen-interface` at commit `5094231`, then
selectively merges the persistence, participant identity, feedback, collector,
telemetry, and remote-finalizer work from:

```text
devs-gen-interface-persistence-dev
commit a2a3cd8
```

The merge deliberately retains the upstream `devs.simulation.v2`, metrics,
policy, event-trace conformance, headless action, and current OptPilot Resource
contracts. It is not a whole-directory copy of the standalone source.

The v3 construction engine is imported from the archived source bundle
`code_20260914.zip`
(`code_20260914/devs_tools/devs_construct_recon`) as
`devs_tools/devs_construct_recon_v3`.

```text
SHA-256 11e426465a4ebf42d76650f6dd3d273308272eb2cd07f6600c05a71efdf786fe
```

`devs_construct_recon_v3/adapter.py` preserves the reviewed-plan, progress,
path-safety, event-trace, metrics, and external Codex-finalizer contracts used
by the Interface. The existing `devs_construct_recon` package remains in this
v3 Resource only as that compatibility-contract implementation; v3 generation
entry points use the new engine through the adapter.
