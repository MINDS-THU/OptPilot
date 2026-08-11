# `production_line.json` structure

Top-level keys: `task_name`, `target_product`, `target_rate_per_minute`,
`blocks`, `global_entities`, `logistic_robot_count`, and optional `reasoning`.

- **`blocks`** — a mapping of block id to a block. Each block declares a
  `bounding_box` (`left_top` / `right_bottom`, each `{x, y}`) and four entity
  lists: `machines`, `inserters`, `containers`, `logistics_chests`. Every
  entity carries a unique `name`, a `type`, a `position` (`{x, y}`, the entity
  centre) and a `direction`. Machines that run a recipe also carry `recipe`.
  Logistics chests carry `request_filters` (`{index, name, count}`) when they
  request items.
- **`global_entities`** — power and water infrastructure that is not owned by
  a block: substations, roboports, offshore pumps, boilers, steam engines and
  pipes.
- **`logistic_robot_count`** — how many logistic robots the network is given.

Coordinates are Factorio tiles and may be fractional; entity footprints are
centred on `position`, so a 3x3 assembler at `{x: 0, y: 0}` covers -1.5..1.5.
Blocks must contain their own entities, and blocks must not overlap.

Use the `production_line.json` template as the canonical example of every one
of these shapes.
