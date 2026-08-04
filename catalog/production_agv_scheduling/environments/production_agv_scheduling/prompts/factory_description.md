# Multi-line production and AGV simulation

## System boundary

The environment simulates three production lines sharing a raw-material store
and finished-goods warehouse. Each line contains `StationA`, `StationB`,
`StationC`, `QualityCheck`, conveyors `Conveyor_AB`, `Conveyor_BC`, and the
triple-buffer `Conveyor_CQ`, plus `AGV_1` and `AGV_2`.

The simulator owns product routing and local processing. A candidate policy
chooses line assignment for uncommitted raw material, transport-task priority,
AGV assignment and movement, and voluntary charging.

Product routes are:

- P1/P2: RawMaterial → A → B → C → QualityCheck → Warehouse.
- P3: RawMaterial → A → B → C → B → C → QualityCheck → Warehouse.
- Rework: QualityCheck → C → QualityCheck.

The A→B and B→C conveyors transfer products automatically. `Conveyor_CQ` moves
products from C into upper/lower pickup buffers. `AGV_1` services the lower CQ
buffer and `AGV_2` services the upper CQ buffer. QualityCheck pass/rework/scrap
outcomes and all station/conveyor buffer limits are simulated.

## Snapshot

`run(snapshot)` receives:

- `time`: current simulation time in minutes.
- `warehouses`: RawMaterial and Warehouse buffers and interaction points.
- `lines`: per-line `stations`, `conveyors`, and `agvs`.
- `products`: every visible product's type, order, priority, location,
  `buffer_type`, quality state, process step, and last state-change time.
- `commands`: completed/error response text keyed by command ID.
- `static.travel_times`: all-pairs AGV travel estimates.
- `static.macro_times`: estimated line/product processing times.
- `global_stats.wip`: current visible work in process per line.

Each enhanced product also contains:

- `line_owner` and `due_date`;
- `metrics.remaining_time`;
- `metrics.next_op_time`;
- `metrics.remaining_ops`.

AGV records use the exact fields shown below. In particular, the battery field
is named `battery_level` (not `battery`). Typical AGV status values are `idle`,
`moving`, `interacting`, `charging`, and `fault`.

```python
{
    "id": "AGV_1",
    "status": "idle",
    "current_point": "P0",
    "target_point": None,
    "battery_level": 87.5,
    "payload": [],
    "payload_capacity": 1,
}
```

Station and conveyor records expose their finite buffers. A policy should avoid
issuing a new task to an AGV that is moving, interacting, charging, carrying a
different product, or already committed in policy state.

## Commands

Every command contains `line_id`, `command_id`, `action`, `target`, and `params`.
Supported actions are:

- `move`: `params.target_point` is one of P0–P10.
- `load`: optionally identify `params.product_id`.
- `unload`: identify `params.product_id` when carrying multiple products.
- `charge`: optionally set `params.target_level` (default 80).

Commands are asynchronous. Observe AGV state and the `commands` response map on
later calls before advancing a multi-step transfer.

## Score

The maximum total is 100:

- production efficiency, 40: order completion, production-cycle efficiency,
  and device utilization;
- quality/cost, 30: first-pass yield and cost efficiency;
- AGV efficiency, 30: charge strategy, energy efficiency, and utilization.

The default paper setting uses a 500-minute horizon, a 0.5-minute policy step,
fixed 10-minute order intervals, and no faults. Other environment variants
change only fidelity inputs, not the policy contract or scoring implementation.
