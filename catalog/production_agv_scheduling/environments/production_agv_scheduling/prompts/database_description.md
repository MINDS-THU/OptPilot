# Worst-replication SQLite trace

`worst_run.db` is recreated by replaying the lowest-total-score seed. Timestamps
use simulation minutes. List and object values are JSON-encoded SQLite `TEXT`.

## Tables

| Tables | Important columns | Meaning |
| --- | --- | --- |
| `rawmaterial`, `warehouse` | `timestamp`, `buffer`, `message` | Shared input/output storage events. |
| `order` | `timestamp`, `order_id`, `items`, `priority`, `deadline` | Dynamic order arrivals and due dates. |
| `line{1..3}_station{a,b,c}` | `timestamp`, `status`, `buffer`, `message` | Processing, starvation and blocking history. |
| `line{1..3}_qualitycheck` | station columns plus `output_buffer` | Inspection, pass/rework/scrap flow. |
| `line{1..3}_conveyor_{ab,bc}` | `timestamp`, `status`, `buffer`, `message` | Automated internal transfers. |
| `line{1..3}_conveyor_cq` | conveyor columns plus `upper_buffer`, `lower_buffer` | CQ transfer and AGV pickup congestion. |
| `line{1..3}_agv_{1,2}` | `timestamp`, `status`, `current_point`, `target_point`, `estimated_time`, `position`, `payload`, `battery_level`, `message` | AGV motion, interaction, charging and payload history. |
| `line{1..3}_fault` | `timestamp`, `alert_type`, `symptom`, `fault_type`, `estimated_duration`, `message` | Fault/recovery events when enabled. |
| `line{1..3}_response` | `timestamp`, `command_id`, `response` | Command completion and rejection messages. |
| `kpi` | timestamp plus raw production, quality, cost and AGV KPIs | KPI trajectory during the replay. |

## Useful analyses

- Order intervals: consecutive timestamps in `order`.
- Station processing/blocking: status and buffer transitions per station.
- Transport and idle time: AGV status/current-point transitions.
- Battery cost: battery deltas across motion, load/unload, and charge events.
- Downstream congestion: CQ and QualityCheck buffer occupancy over time.
- Command failures or duplicated work: join command IDs and messages in the
  response tables against AGV state near the same timestamp.

Use multiple observations rather than inferring a parameter from one event.
Aggregate evaluator metrics describe all replications; this database describes
only the explicitly reported `worst_seed`.

