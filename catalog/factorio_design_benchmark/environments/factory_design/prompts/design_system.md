# Designing a Factorio production line

You produce one file, `production_line.json`: a complete factory design that
must satisfy every static check before it counts as valid.

Work to this order of priority:

1. **Make it parse and conform.** A design that fails the schema check scores
   nothing else. Copy the structure of the provided `production_line.json`
   template exactly — the same keys, the same nesting, the same entity type
   spellings.
2. **Drive `failed_check_count` to zero.** Each failed check comes back with
   its id and a detail message. Fix the specific cause rather than rewriting
   the design; a design that goes from 6 failures to 2 is real progress.
3. **Only then reduce cost.** Among valid designs, lower `total_entity_cost`
   is better. Do not chase cost by deleting logistic robots: the count is
   self-declared and dominates the cost model, so cutting it lowers the
   number without improving the factory.

Rules of the world, in short: every machine, inserter and chest sits inside a
block whose bounding box contains it; nothing overlaps; everything stays on
the map; mining drills sit on ore of the right kind; offshore pumps sit on a
shoreline; every consumer is powered and connected to a source; robots move
all items, so there are no belts and every item flow needs a provider chest, a
requester chest with the right filter, and roboport coverage that reaches both.

The full text of all 22 checks is in the `validation_rules` reference. Read the
ones you are failing.
