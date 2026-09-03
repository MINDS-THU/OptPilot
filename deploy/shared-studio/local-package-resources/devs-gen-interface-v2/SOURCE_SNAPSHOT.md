# Source snapshot

This versioned Resource starts from OptPilot's upstream
`catalog/devs_gallery/resources/devs-gen-interface` at commit `5094231`, then
selectively merges the persistence, participant identity, feedback, collector,
telemetry, and remote-finalizer work from:

```text
/Users/minds/MINDS/devs-gen-interface-persistence-dev
commit a2a3cd8
```

The merge deliberately retains the upstream `devs.simulation.v2`, metrics,
policy, event-trace conformance, headless action, and current OptPilot Resource
contracts. It is not a whole-directory copy of the standalone source.
