# Domain: single-server job queue

Jobs of three classes (standard, express, bulky) arrive over a 480-minute
shift with exponential inter-arrival gaps. One server processes a single
job at a time; service durations are class-fixed and known on arrival.
Each job carries a due time (four service durations after arrival) and a
class-specific per-minute lateness penalty — express jobs penalize
lateness three times as hard as standard ones, bulky jobs 1.5 times.

The dispatch decision is the only lever: whenever the server frees up,
the policy chooses which waiting job to serve next. First-come
first-served is the baseline; the score rewards short waits and punishes
weighted lateness, so class-aware sequencing (for example serving
soon-due express jobs earlier without starving bulky ones) is where
improvements live.
