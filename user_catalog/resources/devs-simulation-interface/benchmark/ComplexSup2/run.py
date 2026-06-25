#!/usr/bin/env python3
"""Reference simulator for ComplexSup2 — Multi-Product Bakery Supply Chain.

Discrete-event simulation using simpy.
5-tier supply chain: 3 Suppliers → Kitchen → 2 Warehouses → 6 Stores.
3 products (bread, cake, cookie) with different BOM, shelf life, and prices.
"""

import argparse
import json
import random
import sys
from collections import defaultdict

import numpy as np
import simpy

# ── Constants ────────────────────────────────────────────────────────────────
PRODUCTS = ["bread", "cake", "cookie"]
BATCH_SIZES = {"bread": 100, "cake": 50, "cookie": 200}
SHELF_LIFE = {"bread": 3.0, "cake": 5.0, "cookie": 10.0}
SELL_PRICES = {"bread": 2.0, "cake": 8.0, "cookie": 1.0}
PROD_COSTS = {
    "bread": 20 * 1.0 + 5 * 0.8,
    "cake": 10 * 1.0 + 15 * 0.8 + 10 * 1.5,
    "cookie": 15 * 1.0 + 10 * 0.8 + 5 * 1.5,
}
HOLDING_RATE = 0.05
KITCHEN_DAILY_CAP = 10
TRANSPORT_FIXED = 2.0
TRANSPORT_PER_UNIT = 0.01

# Store → warehouse mapping
STORE_WH = {
    "A1": "wh_north", "A2": "wh_north", "C1": "wh_north",
    "B1": "wh_south", "B2": "wh_south", "C2": "wh_south",
}

# (s, S) per store per product
STORE_POLICIES = {
    "A1": {"bread": (15, 40), "cake": (3, 15), "cookie": (10, 50)},
    "A2": {"bread": (20, 50), "cake": (5, 20), "cookie": (15, 60)},
    "B1": {"bread": (10, 30), "cake": (2, 10), "cookie": (8, 40)},
    "B2": {"bread": (15, 40), "cake": (4, 18), "cookie": (12, 50)},
    "C1": {"bread": (8, 25),  "cake": (2, 10), "cookie": (6, 30)},
    "C2": {"bread": (12, 35), "cake": (3, 15), "cookie": (10, 45)},
}

# Store capacities and initial inventory
STORES = {
    "A1": {"wh": "wh_north", "cap": {"bread": 60, "cake": 30, "cookie": 100}, "init": {"bread": 20, "cake": 5, "cookie": 30}},
    "A2": {"wh": "wh_north", "cap": {"bread": 80, "cake": 40, "cookie": 120}, "init": {"bread": 30, "cake": 8, "cookie": 40}},
    "B1": {"wh": "wh_south", "cap": {"bread": 50, "cake": 25, "cookie": 80},  "init": {"bread": 15, "cake": 3, "cookie": 20}},
    "B2": {"wh": "wh_south", "cap": {"bread": 70, "cake": 35, "cookie": 100}, "init": {"bread": 25, "cake": 6, "cookie": 30}},
    "C1": {"wh": "wh_north", "cap": {"bread": 40, "cake": 20, "cookie": 60},  "init": {"bread": 10, "cake": 2, "cookie": 15}},
    "C2": {"wh": "wh_south", "cap": {"bread": 60, "cake": 30, "cookie": 90},  "init": {"bread": 20, "cake": 5, "cookie": 25}},
}

WAREHOUSES = {
    "wh_north": {"cap": 2000, "init": {"bread": 100, "cake": 20, "cookie": 200}},
    "wh_south": {"cap": 1500, "init": {"bread": 80, "cake": 15, "cookie": 150}},
}

LEAD_KITCHEN_WH = {"wh_north": 0.5, "wh_south": 0.8}
LEAD_WH_STORE = {"A1": 0.3, "A2": 0.3, "C1": 0.3, "B1": 0.4, "B2": 0.4, "C2": 0.4}

KITCHEN_REORDER = {"bread": 100, "cake": 20, "cookie": 150}
KITCHEN_TARGET = {"bread": 200, "cake": 40, "cookie": 300}


def emit(evt):
    sys.stdout.write(json.dumps(evt, ensure_ascii=True) + "\n")


class Inventory:
    """FIFO inventory with expiry tracking."""
    def __init__(self):
        self.batches = []  # list of (expiry_time, quantity)
        self.total = 0

    def add(self, qty, expiry_time):
        if qty <= 0:
            return
        self.batches.append([expiry_time, qty])
        self.total += qty

    def remove(self, qty):
        if qty <= 0:
            return 0
        removed = 0
        remaining_batches = []
        for exp, q in self.batches:
            if removed >= qty:
                remaining_batches.append([exp, q])
                continue
            can_take = min(q, qty - removed)
            removed += can_take
            left = q - can_take
            if left > 0:
                remaining_batches.append([exp, left])
        self.batches = remaining_batches
        self.total -= removed
        return removed

    def expire(self, current_time):
        expired = 0
        new_batches = []
        for exp, q in self.batches:
            if exp <= current_time:
                expired += q
            else:
                new_batches.append([exp, q])
        self.batches = new_batches
        self.total -= expired
        return expired


class Simulation:
    def __init__(self, env, sim_time):
        self.env = env
        self.sim_time = sim_time

        # Inventories
        self.wh_inv = {wh: {p: Inventory() for p in PRODUCTS} for wh in WAREHOUSES}
        self.store_inv = {s: {p: Inventory() for p in PRODUCTS} for s in STORES}

        # Initialize warehouse inventory
        for wh, info in WAREHOUSES.items():
            for p, qty in info["init"].items():
                self.wh_inv[wh][p].add(qty, SHELF_LIFE[p] + 100)  # long shelf life for WH

        # Initialize store inventory
        for s, info in STORES.items():
            for p, qty in info["init"].items():
                self.store_inv[s][p].add(qty, SHELF_LIFE[p])

        # Financials
        self.revenue = 0.0
        self.prod_cost = 0.0
        self.transport_cost = 0.0
        self.holding_cost = 0.0
        self.waste_cost = 0.0
        self.total_demand = 0
        self.total_fulfilled = 0
        self.total_lost = 0
        self.total_waste = 0
        self.total_batches = 0

        # In-transit shipments: list of (arrival_time, dest_type, dest_id, product, qty)
        self.in_transit = []

        # Daily production tracking
        self.daily_prod = defaultdict(int)  # day → batches

    def ship(self, arrival_time, dest_type, dest_id, product, qty):
        self.in_transit.append((arrival_time, dest_type, dest_id, product, qty))

    def process_shipments(self, current_time):
        arrived = [s for s in self.in_transit if s[0] <= current_time]
        self.in_transit = [s for s in self.in_transit if s[0] > current_time]
        for _, dest_type, dest_id, product, qty in arrived:
            if dest_type == "wh":
                self.wh_inv[dest_id][product].add(qty, current_time + SHELF_LIFE[product])
                emit({"time": current_time, "event": "shipment_received",
                      "node_id": dest_id, "payload": {"product": product, "quantity": qty, "source_id": "kitchen"}})
            elif dest_type == "store":
                cap = STORES[dest_id]["cap"][product]
                inv = self.store_inv[dest_id][product].total
                can_accept = max(0, cap - inv)
                actual = min(qty, can_accept)
                if actual > 0:
                    self.store_inv[dest_id][product].add(actual, current_time + SHELF_LIFE[product])
                    emit({"time": current_time, "event": "shipment_received",
                          "node_id": dest_id, "payload": {"product": product, "quantity": actual, "source_id": STORES[dest_id]["wh"]}})
                    if actual < qty:
                        self.total_waste += (qty - actual)  # overflow = waste

    def kitchen_process(self):
        """Daily kitchen production at start of each day."""
        day = 0
        while day < self.sim_time:
            yield self.env.timeout(1.0)
            day = int(self.env.now)
            t = float(day)

            # Check total warehouse inventory positions
            wh_pos = {p: 0 for p in PRODUCTS}
            for wh in WAREHOUSES:
                for p in PRODUCTS:
                    wh_pos[p] += self.wh_inv[wh][p].total
                    # Add in-transit to warehouses
                    for _, dt, did, prod, qty in self.in_transit:
                        if dt == "wh" and did == wh and prod == p:
                            wh_pos[p] += qty

            # Determine what to produce
            to_produce = {}
            for p in PRODUCTS:
                if wh_pos[p] < KITCHEN_REORDER[p]:
                    deficit = KITCHEN_TARGET[p] - wh_pos[p]
                    batches_needed = int(np.ceil(deficit / BATCH_SIZES[p]))
                    to_produce[p] = batches_needed

            if not to_produce:
                continue

            # Allocate daily capacity proportionally
            total_needed = sum(to_produce.values())
            allocated = {}
            for p, needed in to_produce.items():
                allocated[p] = max(1, int(np.floor((needed / total_needed) * KITCHEN_DAILY_CAP)))

            # Adjust to fit capacity
            while sum(allocated.values()) > KITCHEN_DAILY_CAP:
                max_p = max(allocated, key=allocated.get)
                allocated[max_p] = max(0, allocated[max_p] - 1)
            while sum(allocated.values()) < KITCHEN_DAILY_CAP:
                max_p = max(to_produce, key=lambda p: to_produce[p] - allocated.get(p, 0))
                allocated[max_p] = allocated.get(max_p, 0) + 1

            for p, batches in allocated.items():
                if batches <= 0:
                    continue
                self.total_batches += batches
                self.daily_prod[day] += batches
                qty = batches * BATCH_SIZES[p]
                self.prod_cost += batches * PROD_COSTS[p]

                emit({"time": t, "event": "production_start", "node_id": "kitchen",
                      "payload": {"product": p, "batch_size": BATCH_SIZES[p],
                                  "raw_materials": {"flour": 0, "sugar": 0, "dairy": 0}}})

                # Ship to warehouses
                lt_n = LEAD_KITCHEN_WH["wh_north"]
                lt_s = LEAD_KITCHEN_WH["wh_south"]
                # Split proportionally
                half = qty // 2
                self.ship(t + lt_n, "wh", "wh_north", p, half)
                self.ship(t + lt_s, "wh", "wh_south", p, qty - half)

    def store_process(self, store_id):
        """Daily demand and replenishment for a store."""
        wh_id = STORES[store_id]["wh"]
        day = 0
        while day < self.sim_time:
            yield self.env.timeout(1.0)
            day = int(self.env.now)
            t = float(day)

            # Process arrivals
            self.process_shipments(t)

            # Check expiry
            for p in PRODUCTS:
                expired = self.store_inv[store_id][p].expire(t)
                if expired > 0:
                    wc = expired * SELL_PRICES[p] * 0.5
                    self.waste_cost += wc
                    self.total_waste += expired
                    emit({"time": t, "event": "inventory_expired", "node_id": store_id,
                          "payload": {"product": p, "quantity": expired, "waste_cost": wc}})

            # Replenishment check (s, S)
            for p in PRODUCTS:
                s, S = STORE_POLICIES[store_id][p]
                on_hand = self.store_inv[store_id][p].total
                if on_hand <= s:
                    qty = S - on_hand
                    wh_avail = self.wh_inv[wh_id][p].total
                    actual = min(qty, wh_avail, STORES[store_id]["cap"][p] - on_hand)
                    if actual > 0:
                        self.wh_inv[wh_id][p].remove(actual)
                        cost = TRANSPORT_FIXED + TRANSPORT_PER_UNIT * actual
                        self.transport_cost += cost
                        lt = LEAD_WH_STORE[store_id]
                        self.ship(t + lt, "store", store_id, p, actual)
                        emit({"time": t, "event": "replenishment_order", "node_id": store_id,
                              "payload": {"product": p, "quantity": actual, "source_id": wh_id, "order_cost": cost}})

            # Demand arrivals
            dow = day % 7
            is_weekend = dow in (5, 6)
            lam = 6 if is_weekend else 3
            num_arrivals = np.random.poisson(lam)

            for _ in range(num_arrivals):
                # Choose product
                r = random.random()
                if r < 0.5:
                    product = "bread"
                elif r < 0.7:
                    product = "cake"
                else:
                    product = "cookie"

                # Choose quantity
                min_q = 2 if is_weekend else 1
                max_q = 8 if is_weekend else 4
                qty_req = random.randint(min_q, max_q)

                self.total_demand += qty_req
                fulfilled = self.store_inv[store_id][product].remove(qty_req)
                self.total_fulfilled += fulfilled
                rev = fulfilled * SELL_PRICES[product]
                self.revenue += rev

                emit({"time": t + random.uniform(0, 0.99), "event": "demand_arrival",
                      "node_id": store_id, "payload": {
                          "product": product, "quantity_requested": qty_req,
                          "quantity_fulfilled": fulfilled, "revenue": rev}})

                if fulfilled < qty_req:
                    lost = qty_req - fulfilled
                    self.total_lost += lost
                    emit({"time": t + random.uniform(0, 0.99), "event": "lost_sale",
                          "node_id": store_id, "payload": {"product": product, "quantity_lost": lost}})

            # Snapshot
            on_hand = {p: self.store_inv[store_id][p].total for p in PRODUCTS}
            emit({"time": t + 0.999, "event": "snapshot", "node_id": store_id,
                  "payload": {"on_hand": on_hand}})

    def holding_cost_process(self):
        """Accumulate holding cost once per day across all nodes."""
        day = 0
        while day < self.sim_time:
            yield self.env.timeout(1.0)
            day = int(self.env.now)
            for s_id in STORES:
                for p in PRODUCTS:
                    self.holding_cost += max(0, self.store_inv[s_id][p].total) * HOLDING_RATE
            for wh_id in WAREHOUSES:
                for p in PRODUCTS:
                    self.holding_cost += max(0, self.wh_inv[wh_id][p].total) * HOLDING_RATE

    def run(self):
        # Start processes
        self.env.process(self.kitchen_process())
        self.env.process(self.holding_cost_process())
        for s in STORES:
            self.env.process(self.store_process(s))

        self.env.run(until=self.sim_time)

        # Final sim_trace
        total_cost = self.prod_cost + self.transport_cost + self.holding_cost + self.waste_cost
        profit = self.revenue - total_cost
        service_level = self.total_fulfilled / self.total_demand if self.total_demand > 0 else 1.0

        emit({"time": float(self.sim_time), "event": "sim_trace", "node_id": "System",
              "payload": {
                  "total_revenue": round(self.revenue, 2),
                  "total_cost": round(total_cost, 2),
                  "total_profit": round(profit, 2),
                  "total_demand": self.total_demand,
                  "total_fulfilled": self.total_fulfilled,
                  "total_lost_sales": self.total_lost,
                  "service_level": round(service_level, 4),
                  "total_waste": self.total_waste,
                  "total_production_batches": self.total_batches,
              }})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--simulate_time", type=int, default=30)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    random.seed(args.seed)
    np.random.seed(args.seed)

    env = simpy.Environment()
    sim = Simulation(env, args.simulate_time)
    sim.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
