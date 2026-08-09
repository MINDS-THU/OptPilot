# Worst-run trace schema (SQLite)

- `events(time REAL, event TEXT, job_id TEXT, detail REAL)` — one row per
  simulation event: `arrival`, `start`, and `finish` (detail = lateness
  in minutes on finish rows). Ordered by time.
- `kpi(name TEXT, value REAL)` — `total_score`, `served`, `mean_wait`,
  `weighted_lateness` for this replication.
