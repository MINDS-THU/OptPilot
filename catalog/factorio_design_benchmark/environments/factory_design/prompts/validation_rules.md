# Validation rules

The 22 static checks applied to every design, vendored verbatim from the
Factorio Design Benchmark (MIT). Each heading is the check id reported in
the failed-check list.


<!-- v1_1_schema.md -->
#### V1.1 Schema and JSON Structure

The submitted object must be parseable JSON and must match the public
`ProductionLine` schema.

This rule is satisfied when:
- the top-level output is one JSON object, not prose or a partial fragment
- required top-level fields and nested collections have the expected names and types
- empty required collections are represented as empty arrays or objects rather than omitted
- numeric fields such as positions, rates, counts, and robot counts are real numbers or integers as required


<!-- v1_2_entity_types.md -->
#### V1.2 Supported Entity Types

Every entity `type` must be one of the benchmark-supported Factorio entity types
for its field.

This rule is satisfied when:
- block machines use only:
  `assembling-machine-2`,
  `electric-furnace`, 
  `electric-mining-drill`,
- inserters use only:
  `inserter`, 
- logistics chests use only:
  `logistic-chest-passive-provider`,   `logistic-chest-requester`, 
- containers use only:
  `iron-chest`,
- global entities use only:
  `roboport`, `substation`, `big-electric-pole`,
  `offshore-pump`, `boiler`, `steam-engine`, `pipe`,
- no placeholder type such as `string` is used


<!-- v2_1_recipe_names.md -->
#### V2.1 Valid Recipe Names

Explicit machine recipes must be known benchmark recipes.

This rule is satisfied when:
- every non-empty `machine.recipe` value is an exact supported recipe name
- assembling machines use explicit recipe names for assembly products
- furnace recipes may be omitted for plate smelting because furnaces auto-select from inputs during execution
- no invented recipe names or item names are used as recipes


<!-- v2_2_target_producer.md -->
#### V2.2 Target Chain Producers

The design must contain producer machines for the target product and its required intermediate recipe chain.

This rule is satisfied when:
- for every assembly recipe in the target chain, at least one assembling machine has `recipe` exactly equal to that recipe name
- for furnace recipes such as `iron-plate` and `copper-plate`, at least one furnace exists for plate smelting
- the design is not only a final assembler plus requested intermediate items; it must include machines that can produce the required intermediates


<!-- v3_1_map_bounds.md -->
#### V3.1 Map Bounds

Every placed entity footprint must stay inside the configured map bounds.

This rule is satisfied when:
- each entity center plus its full footprint remains within the map rectangle
- no block entity or global entity extends past the map edge


<!-- v3_2_entity_overlap.md -->
#### V3.2 Entity Collision / Overlap

No two placed entity footprints may overlap.

This rule is satisfied when:
- every pair of block-owned entities and global entities has disjoint collision/footprint boxes
- touching edges is acceptable, but overlapping interiors is not

For two axis-aligned entities `A` and `B`, compare their centers and direction-adjusted footprint sizes.

$$
dx = |A.x - B.x|,\quad dy = |A.y - B.y|
$$

They are safe from overlap when they are separated on at least one axis:

$$
dx \ge (A.width + B.width) / 2
\quad\text{or}\quad
dy \ge (A.height + B.height) / 2
$$

They overlap only when both axis distances are too small:

$$
dx < (A.width + B.width) / 2
\quad\text{and}\quad
dy < (A.height + B.height) / 2
$$

If unsure, use `bbox_for_entity` to get each entity's direction-adjusted `width` and `height`, and use `geometry_distance` to get `abs_dx` and `abs_dy`. Compare axis distances, not Euclidean distance.


<!-- v3_3_alignment.md -->
#### V3.3 Center Alignment

Entity centers must follow the benchmark static-validator placement convention.

This rule is satisfied when:
- odd-sized footprint dimensions such as `1x1`, `3x3`, and `3x5` use `.5` centers on that axis
- even-sized footprint dimensions such as `2x2` and `4x4` use integer centers on that axis
- steam-power entities are exempt from this center-alignment check: `offshore-pump`, `boiler`, `steam-engine`, and `pipe`


<!-- v3_4_block_bboxes.md -->
#### V3.4 Block Bounding Boxes

Every block bounding box must be a valid nonzero rectangle in global map
coordinates, and block bounding boxes must not overlap each other.

This rule is satisfied when:
- each block has `left_top.x < right_bottom.x`(Scalar value)
- each block has `left_top.y < right_bottom.y`(Scalar value)
- all block boxes use the same global coordinate frame as entity positions
- block boxes are mutually disjoint


<!-- v3_5_block_containment.md -->
#### V3.5 Block Entity Containment

Every block-owned entity footprint must fit fully inside that block's bounding
box.

This rule is satisfied when:
- all machines, inserters, logistics chests, and containers in a block are inside that block's `bounding_box`
- full entity footprints are inside the block, not merely centers
- global entities are not checked against any block bounding box


<!-- v4_1_roboport_coverage.md -->
#### V4.1 Roboport Logistics Coverage

If logistics chests exist, roboports must exist, and every logistics chest center
must be inside at least one roboport logistics coverage square.

This rule is satisfied when, for every logistics chest, there is a roboport such that:

$$
|chest.x - roboport.x| \le 24
$$

and

$$
|chest.y - roboport.y| \le 24
$$

Coverage is checked by chest center, not by chest footprint.


<!-- v4_2_request_filters.md -->
#### V4.2 Requester Chest Filters

Every requester chest must have valid request filters.

This rule is satisfied when:
- each `logistic-chest-requester` has a non-empty `request_filters` list
- every filter has a non-empty item `name`
- every filter `index` is an integer slot in `1..30`
- every filter `count` is a positive integer buffer target
- negative filter indices, zero counts, and placeholder filters are invalid


<!-- v4_3_provider_feasibility.md -->
#### V4.3 Provider Feasibility

Requested robot-delivered items need provider-side availability in the logistics
network.

This rule is satisfied when:
- if any requester chest has request filters, at least one passive-provider or active-provider chest exists
- this rule checks provider existence for requested logistics, not exact item matching


<!-- v4_4_roboport_connectivity.md -->
#### V4.4 Roboport Network Connectivity

When multiple roboports are used for one logistics system, they must form a
connected roboport network.

This rule is satisfied when:
- all roboports that must exchange items belong to one connected component
- two roboports are treated as connected when their center-to-center Euclidean distance is at most `48`


<!-- v4_5_inserter_reach.md -->
#### V4.5 Inserter Pickup and Drop Reach

Every inserter must reach at least one valid pickup entity and one valid drop
entity.

Let the inserter center be `(x, y)`, the direction vector be `(dx, dy)`, and the
inserter reach be `r` (`1` for `inserter`).

The validator computes:

$$
pickup = (x - dx \cdot r,\ y - dy \cdot r)
$$

$$
drop = (x + dx \cdot (r + 0.2),\ y + dy \cdot (r + 0.2))
$$

This rule is satisfied when:
- the pickup point is inside at least one non-inserter entity footprint in the same block
- the drop point is inside at least one non-inserter entity footprint in the same block
- `direction` is the drop-side direction used to compute `(dx, dy)`
- in practice, the inserter should sit next to the source and destination closely enough for these points to land inside their footprints, but the inserter footprint itself must not overlap them


<!-- v4_6_logistic_robot_count.md -->
#### V4.6 Logistic Robot Count

If requester/provider logistics chests are used, the executable top-level
`logistic_robot_count` must be positive.

This rule is satisfied when:
- the final exported design contains `logistic_robot_count` or the workflow writes the equivalent value into the final design
- the value is a positive integer
- the value is not `null`, `0`, `-1`, or any sentinel meaning "unknown"


<!-- v4_7_material_flow_interfaces.md -->
#### V4.7 Material-Flow Interfaces

Machines and chests that participate in item flow must be connected by actual
inserter or mining-drill transfer behavior.

This rule is satisfied when:
- every furnace or assembling machine has at least one inserter dropping input into it
- every furnace or assembling machine has at least one inserter picking output from it
- putting a chest next to a furnace or assembling machine does not count as automatic input or output
- every regular container or provider chest is fed by an inserter drop or by direct mining-drill output
- every requester chest used as an input buffer is picked from by an inserter
- mining-drill output may go directly into a chest placed on the drill output tile


<!-- v5_1_consumer_power.md -->
#### V5.1 Powered Consumers

Every electric consumer center must be inside the supply area of a pole or
substation that is connected to a power source.

This rule is satisfied when:
- every electric machine center is covered by a source-connected pole/substation supply area
- every inserter center is covered by a source-connected pole/substation supply area
- every roboport center is covered by a source-connected pole/substation supply area
- supply coverage is checked per axis:

$$
|consumer.x - pole.x| \le supply\_radius
$$

and

$$
|consumer.y - pole.y| \le supply\_radius
$$

- a consumer covered only by an isolated, unconnected pole/substation is still unpowered


<!-- v5_2_power_source_connection.md -->
#### V5.2 Power Source Connected to the Grid

The electric pole/substation network must be connected to an actual power source.

This rule is satisfied when:
- at least one power generation source exists, such as a `steam-engine`
- at least one pole or substation supply area covers a power source
- every downstream consumer-covering pole/substation is connected to that generation-side pole/substation component through wire reach
- wire reach is checked by center distance between poles/substations:

$$
distance(pole_a, pole_b) \le min(wire\_reach_a, wire\_reach_b)
$$


<!-- v6_1_mining_on_ore.md -->
#### V6.1 Mining Drills on Ore

Mining drills must be placed consistently with a configured ore patch.

This rule is satisfied when:
- each mining drill center lies within the usable circular area of at least one configured ore patch
- the drill does not need to be placed at the ore patch center; any valid location within the ore circle is acceptable
- the validator accepts the drill when its center distance to an ore patch center is at most `ore_radius + 1.5`


<!-- v6_2_offshore_pump_shoreline.md -->
#### V6.2 Offshore Pump Shoreline and Direction

Offshore pumps must be placed at or adjacent to a configured water shoreline, and
their direction must point into the water patch.

This rule is satisfied when:
- the task has a configured water patch if an `offshore-pump` is used
- the pump footprint touches or overlaps the shoreline region of that water rectangle
- the pump direction points from the pump toward the water rectangle


<!-- v6_3_non_pump_land.md -->
#### V6.3 Non-Pump Entities on Land

All non-pump entities must stay on land and must not overlap configured water
rectangles.

This rule is satisfied when:
- machines, inserters, chests, containers, roboports, poles, substations, pipes, boilers, and steam engines do not overlap water rectangles
- only `offshore-pump` is allowed to touch or overlap water according to the pump shoreline rule
- water checks use full entity footprints, not only centers


<!-- v6_4_steam_direction.md -->
#### V6.4 Steam Chain Direction and Fluid Connection Consistency

Boilers, steam engines, and pipes must form a plausible fluid chain:
`offshore-pump -> pipe -> boiler -> pipe -> steam-engine`.

This rule is satisfied when:
- all boilers and steam engines in the submitted steam setup use the same direction value
- water from the offshore pump reaches each boiler through the boiler's water-input short side. If boiler is at (x,y) and its direction is east and water pump is on its north, then its water-in pipe is at (x-0.5,y-2). If boiler is at (x,y) and its direction is west and water pump is on its north, then its water-in pipe is at (x+0.5,y-2). 
- steam leaves each boiler along the boiler's output direction through a steam-side pipe
- each first steam engine row receives steam on its upstream short side from a steam pipe
- later steam engines in the same row may receive steam from the previous steam engine
- for chained boilers carrying water onward, the water path connects short side to short side

For an east-facing row, a safe pattern is:
- if boiler is at (x,y), then its water-in pipe is at (x-0.5,y-2)
- boiler facing east
- steam pipe(x+1.5,y) immediately east of the boiler(x,y)
- steam engine(x+4.5,y) facing east immediately after that steam pipe(x+1.5,y)

Steam-power entities are exempt from the general integer/.5 coordinate alignment rule. This rule checks relative connector geometry, not a global coordinate parity convention.
