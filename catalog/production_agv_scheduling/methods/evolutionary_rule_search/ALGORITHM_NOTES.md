# Evolutionary baseline fidelity

All three configurations evaluate the same 64 Cartesian one-hot policies
before running ten generations of 64 candidates. Each candidate has four line,
eight task, and two AGV weights; every segment is repaired independently onto
the nonnegative simplex. Selection uses the paper's stability objective:

```text
mean_total_score - 0.35 * std_total_score
```

The implementation is dependency-free and exposes every simulation as an
OptPilot trial. It therefore implements the named operators directly instead
of calling pymoo's hidden evaluation loop:

- GA uses binary tournaments, SBX (`p=0.9`, `eta=15`, per-variable `p=0.5`),
  polynomial mutation (`p=0.25`, `eta=20`), and elitist parent-plus-offspring
  survival.
- DE uses `best/1/bin` (`F=0.5`, `CR=0.2`) followed by the configured 0.1
  polynomial-mutation probability and target-versus-trial survival.
- PSO uses random initial velocities, fixed `w=0.9`, `c1=c2=2.0`, and velocity
  clipping at 0.2.

These preserve the paper-level parameters and deterministic seeded behavior.
They do not reproduce pymoo's undocumented version-specific duplicate
elimination, adaptive PSO coefficient update, or best-particle perturbation.
Consequently, exact candidate trajectories can differ from the original pymoo
run even with identical simulation observations. The 64 initial candidates and
the environment scoring protocol remain exact and directly comparable.
