SupplyNetPy API Reference
SupplyNetPy includes a sub-module called Components,
 which facilitates the creation of supply chain networks by providing 
essential components such as nodes, links, and demand, and assembling 
them into a network. The Components module contains three sub-modules: core, logger, and utilities.
The core module is responsible for creating supply chain components. It includes classes such as RawMaterial, Product, Inventory, Node, Link, Supplier, Manufacturer, InventoryNode, and Demand, the replenishment policies (SSReplenishment, RQReplenishment, PeriodicReplenishment), and the supplier-selection policies (SelectFirst, SelectAvailable, SelectCheapest, SelectFastest). Any new node created using these classes will be instantiated within a SimPy environment. The Inventory
 class is responsible for monitoring inventories. By default, the 
classes in the core module support single-product inventories. The Inventory class wraps a SimPy Container
 to implement the basic behavior of an inventory, including routines to 
record inventory level changes. If users wish to create a different 
inventory type with custom behavior, they can do so by extending either Inventory or SimPy Container. The core module also exports set_seed(seed) for seeding the library-wide default RNG used for probabilistic disruption, so runs can be made reproducible.
The node_type argument on Node / Supplier / InventoryNode / Demand is validated against the NodeType str-enum (also exported from the core module). Accepted values are infinite_supplier, supplier, manufacturer, factory, warehouse, distributor, retailer, store, shop, and demand. You can pass either a string (case-insensitive) or the enum member for IDE autocomplete:

scm.Supplier(env=env, ID="S1", name="S1", node_type="supplier")
scm.Supplier(env=env, ID="S1", name="S1", node_type=scm.NodeType.SUPPLIER)
Writing a custom replenishment policy
The built-in replenishment policies (SSReplenishment, RQReplenishment, PeriodicReplenishment) communicate with the node they belong to through a small set of helper methods on Node, rather than reading or writing the node's internal attributes directly. A user-defined InventoryReplenishment subclass should use the same helpers so that it stays decoupled from the internal layout of Node:

node.position() — current backorder-aware inventory position (on_hand - stats.backorder[1]). Use this in place of reading node.inventory.on_hand and node.stats.backorder[1] separately.
node.place_order(quantity) — picks a supplier via the node's supplier-selection policy and spawns the dispatch process. Use this in place of selection_policy.select(...) + env.process(process_order(...)).
node.wait_for_drop() — generator that blocks until the inventory drops and rotates the drop event atomically. Use yield from node.wait_for_drop() in the policy's run() loop.
On the supplier-selection side, Link exposes link.available_quantity() (returns source.inventory.level), so a custom SupplierSelectionPolicy subclass can compare candidate suppliers without reaching past the link.

Modeling what a disruption does to stored inventory
A real-world disruption can take many forms — a natural disaster, a 
power outage, contamination, theft, a strike — and each affects the 
goods sitting on the shelf differently. The disruption settings already 
on Node (failure_p for random failures, node_disrupt_time for scheduled ones, and node_recovery_time for how long the outage lasts) only switch the node off and stop it from accepting new orders. They do not describe what happens to the existing inventory.
If you want the simulation to also account for goods that are physically lost during a disruption, set the disruption_impact argument when you create the node (and, if you choose "destroy_fraction", also disruption_loss_fraction). The available choices are:
disruption_impact valueWhat happens the moment the disruption beginsNone (the default) or "none"The node goes offline but the inventory is left alone."destroy_all"All stock at the node is lost. For a Manufacturer, the 
on-hand raw materials are also wiped, since the idea behind this preset 
is "every physical thing at this site is gone." Infinite suppliers 
(which never run out) are skipped, since "destroying" an infinite shelf 
has no meaning."destroy_fraction"A share of the current stock is lost. The share is given by disruption_loss_fraction, which can be a fixed number between 0 and 1 (e.g., 0.3 to lose 30%) or a small function that returns a different number each time, if you want randomized losses.A callable, i.e. f(node) -> NoneFor anything more specific. You write a short Python function 
describing the effect; the simulator calls it with the affected node 
when a disruption hits. Examples: a contamination that destroys only 
certain batches, damage that reduces capacity, a lead-time penalty that 
kicks in once the node recovers.
The effect is applied once, exactly at the moment the node goes from active to inactive — both for scheduled (node_disrupt_time) and random (failure_p) disruptions. Importantly, it is not
 re-applied at every step while the node is offline. If you want a loss 
that grows over time during an outage (slow spoilage from prolonged loss
 of cooling, ongoing pilferage), implement that inside your own 
callable, where you can spawn a parallel SimPy process and time it 
against node_recovery_time.
The two built-in presets ultimately call Inventory.destroy(amount, reason), a public method on Inventory that immediately removes the requested quantity from the underlying simpy.Container. If the node holds perishable batches, the oldest batches are removed first (FIFO). One subtle but important detail: destroy does not
 wake up the replenishment policy. The reason is that while the node is 
offline, new orders are blocked at the dispatch gate anyway; if destroy
 were to wake the replenishment policy, it would simply queue up a 
backlog of orders that all fire the instant the node recovers — flooding
 the supplier and distorting the simulation. If you write a custom 
impact callable that destroys stock, call Inventory.destroy(...) for the same reason; avoid editing level, on_hand, or perish_queue by hand.
The amount destroyed (destroyed_qty) and its monetary value (destroyed_value) are recorded on the node's Statistics. destroyed_value is registered as a cost component, so it is automatically included in node_cost and therefore in profit
 — you don't need to subtract it manually. When writing a custom impact 
callable, follow the same convention by reporting losses through node.stats.update_stats(destroyed_qty=qty, destroyed_value=qty * unit_cost), rather than inventing parallel statistics that the simulator won't know about.
A short example:

import randomimport SupplyNetPy.Components as scm# Wipe everything when disruption hits — common for natural-disaster scenarios.
distributor = scm.InventoryNode(
    env=env, ID="D1", name="D1", node_type="warehouse",
    capacity=200, initial_level=120, inventory_holding_cost=0.2,
    replenishment_policy=scm.SSReplenishment, policy_param={'s': 100, 'S': 200},
    product_sell_price=10, product_buy_price=5,
    node_disrupt_time=lambda: 30, node_recovery_time=lambda: 5,
    disruption_impact="destroy_all",
)# Power-outage style: each disruption spoils 10–40% of the current shelf.
warehouse = scm.InventoryNode(
    ..., disruption_impact="destroy_fraction",
    disruption_loss_fraction=lambda: random.uniform(0.1, 0.4),
)
The logger module is designed to maintain simulation logs. It includes the GlobalLogger
 class, which serves as a common logger for all components within the 
environment. Users can configure this logger to save logs to a specific 
file or print them to the console. A package-level global_logger instance is also exported so users can call scm.global_logger.disable_logging() / scm.global_logger.enable_logging() to toggle all simulation logs at once.
The utilities module provides useful Python routines to reduce manual work. It contains functions for creating supply chains (create_sc_net), running simulations (simulate_sc_net), and inspecting / visualizing the resulting network (get_sc_net_info, print_node_wise_performance, visualize_sc_net).
Note that create_sc_net accepts two interchangeable construction styles — plain dict netlists, or pre-built Node / Link / Demand objects — but each of its nodes, links, and demands
 arguments must be homogeneous: a list mixing dicts and pre-built 
objects is rejected up-front (the mixed case would cause dicts and 
objects to run against two different simpy.Environment instances). When any list contains pre-built objects you must also pass env explicitly, and each object's own env must match it.
The public API of each sub-module is declared via an explicit __all__ list, and SupplyNetPy.Components
 re-exports these names explicitly — no wildcard imports. Module-level 
imports (SimPy, NumPy, NetworkX, Matplotlib, etc.) and internal 
constants are deliberately not reachable as scm.<name>.          Pharmacy Supply Chain
This study is from a publication 'Improving Simulation Optimization Run Time When Solving For Periodic Review Inventory Policies in A Pharmacy', read it here.
 This study uses the simulation-based optimization (SBO) method to find 
the optimum values of replenishment policy parameters (s, S) to minimize
 the overall cost of the pharmacy supply chain.
In this example, we aim to understand the supply chain network system
 described by the authors and to use SupplyNetPy to implement it and 
replicate the results. This exercise allows us to evaluate the 
complexity of reconstructing a specific system with SupplyNetPy and to 
validate the library.

System Description
The system is a single-echelon supply chain — that is, a chain with 
just one layer between the source and the customer. In this case, a 
hospital pharmacy (the distributor) is supplied directly by a 
pharmaceutical manufacturing facility (the supplier).

The pharmacy faces stochastic (i.e., unpredictable, 
day-to-day-varying) demand and is connected to a supplier with unlimited
 stock.
On any given day, the supplier may be disrupted (unable to ship) with probability 0.001.
The pharmacy follows an (s, S) replenishment policy: when the inventory level falls below s, place an order large enough to bring it up to S.
The product is perishable, with a shelf life of 90 days.
The authors model and simulate this system to find the values of s and S that minimize the expected total cost per day.

Inventory Type: Perishable 
Inventory position is the amount of stock the pharmacy can count on as of today — what is physically on the shelf right now, plus anything already ordered that has not yet arrived (i.e., in transit from the supplier). It is the basis on which the next reorder decision is made. 

  Inventory position = inventory on hand + inventory en route 

Replenishment Policy: (s, S) — when the inventory position falls below s, place an order for (S − current inventory position) units, so that the position is brought back up to S. 

Review period = 1 day (the pharmacy checks its inventory every day). 
The flowchart below uses the following shorthand: 

  H = inventory on hand (what is on the shelf) 

  H′ = portion of H that will expire at the end of day t 

  P = inventory position = H + inventory en route 

This flowchart illustrates the order and sequence of events that take place at the pharmacy. 
(Image source)
Algorithm: Process flow of Perishable Inventory
Begin day t
    1. Order arrive at the Pharmacy
    2. Update Inventory Levels (H)
    3. Satisfy demand based on Inventory Levels
        3.1. If (d>H):
            - demand Satisfied = H
            - Shortage = d-H
            - Waste = 0
        3.2. Else:
            - demand Satisfied = d
            - Shortage = 0
            - Waste = max{0,H'-d}
    4. Discard Expired Drugs
    5. Update Inventory Levels (H)
        5.1. Is (s>P) and (Supplier not Disrupted):
            - Place order for (S-P)
        5.2. Else:
            - Do not place order
    6. End of the day t
Optimization Objective: Minimize Expected Cost per day 

Costs of interest are shortage, waste, holding and ordering 
Assumptions
All drugs that arrive in the same month come from the same production batch and have 
 same end of the month expiration date. Hence drugs are only discarded at the end of the month.
The inventory on-hand is know each day.
Estimated days until inventory expires is known each day.
Lead time is deterministic and positive. The order placed at the end of the day (t-1) arrives at the beginning of day t.
Demand is stochastic.
Supply uncertainty is due to disruptions
 (these two are independent of each other)
All demand that is not met is lost.
First in first out protocol is followed when serving orders.
System Configuration
T = number of days (360 days)
l = deterministic lead time (6 days)
e = shelf life of the drugs (months) (3 months)
R = number of simulation replication (5000)
b = shortage cost (5 units)
z = waste cost (1 units)
h = holding cost (0.001 units)
o = ordering cost (0.5 units)
dt = demand on day t (stochastic) (Poisson 25/day)
yt = binary variable for supply disruption status on day t (yt=0 disrupted, yt=1 available) (stochastic) (p=0.01)
disrupt_time ~ Geom(p=0.01)
recovery_time ~ Geom(p=1/30)
Implementation
import simpyimport numpy as npimport matplotlib.pyplot as pltfrom matplotlib.pyplot import figureimport SupplyNetPy.Components as scm
class Distributions:
    """
    Class to generate random numbers for demand (order quantity) and order arrival arrival times.
    Parameters:
        mu (float): Mean of the exponential distribution for order arrival times.
        lam (float): Lambda parameter of the Poisson distribution for demand (order quantity).        
    """
    def __init__(self,mu=1,lam=1,p=0.01):
        self.mu = mu
        self.lam = lam
        self.p = p

    def poisson_demand(self):
        return np.random.poisson(self.lam)

    def expo_arrival(self):
        return np.random.exponential(self.mu)

    def geometric(self):
        return np.random.geometric(self.p,1)[0]def manufacturer_date_cal(time_now):
    """Calculate manufacturing date rounded down to the nearest month."""
    return time_now - (time_now % 30) # Global parameters
T = 360     #number of days (360 days)
l = 6       #deterministic lead time (6 days)
e = 90      #shelf life of the drugs (months) (3 months)
R = 5000    #number of simulation replication (5000)
b = 5       #shortage cost (5 units)
z = 1       #waste cost (1 units)
h = 0.001   #holding cost (0.001 units)
o = 0.5     #ordering cost (0.5 units)
dt = 25     #demand on day t (stochastic) (Poisson 25/day)# yt = binary variable for supply disruption status on day t (yt=0 disrupted, yt=1 available) (stochastic) (p=0.01)
yt_p = 0.01  # ~ Geometric(p=0.01), sampled from Geometric distribution with probability p = 0.01
yt_r = 1/30 # node recovery time ~ Geometric(p=1/30), sampled from Geometric distribution with probability p = 1/30 = 0.033
price = 7 # unit cost of drugdef setup_simulation(env, s, S, ini_level, st, disrupt_time, recovery_time):
    """Setup environment, supplier, distributor, link, and demand process."""
    supplier = scm.Supplier(env=env, ID="S1", name="Supplier 1", node_type="infinite_supplier",
                            node_disrupt_time=disrupt_time.geometric,
                            node_recovery_time=recovery_time.geometric)

    distributor = scm.InventoryNode(env=env, ID="D1", name="Distributor 1", node_type="distributor",
                                    capacity=float('inf'), initial_level=ini_level,
                                    inventory_holding_cost=h, inventory_type="perishable",
                                    manufacture_date=manufacturer_date_cal, shelf_life=e,
                                    replenishment_policy=scm.SSReplenishment,
                                    policy_param={'s': s, 'S': S},
                                    product_buy_price=0, product_sell_price=price)

    link = scm.Link(env=env, ID="l1", source=supplier, sink=distributor,
                    cost=o, lead_time=lambda: l)

    demand = scm.Demand(env=env, ID="d1", name="demand 1", order_arrival_model=lambda: 1,
                        order_quantity_model=st.poisson_demand, demand_node=distributor)

    return supplier, distributor, demand, linkdef single_sim_run(S, s, ini_level, logging=True):
    """Run a single simulation instance and return expected cost."""
    env = simpy.Environment()
    st = Distributions(mu=1, lam=dt)
    disrupt_time = Distributions(p=yt_p)
    recovery_time = Distributions(p=yt_r)

    supplier, distributor, demand, link = setup_simulation(env, s, S, ini_level, st, disrupt_time, recovery_time)

    pharma_chain = scm.create_sc_net(env=env, nodes=[supplier, distributor], links=[link], demands=[demand])
    pharma_chain = scm.simulate_sc_net(pharma_chain,sim_time=30,logging=logging)
    supplier.stats.reset()
    distributor.stats.reset()
    demand.stats.reset()
    pharma_chain = scm.simulate_sc_net(pharma_chain,sim_time=T,logging=logging)

    shortage = distributor.stats.shortage[1]
    waste = distributor.stats.inventory_waste
    holding = distributor.stats.inventory_carry_cost
    transport = distributor.stats.transportation_cost

    total_cost = (shortage * b + waste * z + holding + transport)
    norm_cost = total_cost / ((T - 30) * (b + z + h + o)) * price

    if logging:
        print("Shortage cost:", shortage * b)
        print("Waste cost:", waste * z)
        print("Holding cost:", holding)
        print("Transportation cost:", transport)

    return norm_costdef run_for_s(s_low, s_high, s_step, capacity, ini_level, num_replications):
    """Run simulations across reorder point values and report results."""
    results = []
    st = Distributions(mu=1, lam=dt)
    disrupt_time = Distributions(p=yt_p)
    recovery_time = Distributions(p=yt_r)


    #print("reorder_point, exp_cost_per_day, std, std_err")
    for s in range(s_low, s_high, s_step):
        costs = []
        for _ in range(num_replications):
            env = simpy.Environment()
            supplier, distributor, demand, link = setup_simulation(env, s, capacity, ini_level, st, disrupt_time, recovery_time)

            pharma_chain = scm.create_sc_net(env=env, nodes=[supplier, distributor], links=[link], demands=[demand])
            pharma_chain = scm.simulate_sc_net(pharma_chain,sim_time=30,logging=False)
            supplier.stats.reset()
            distributor.stats.reset()
            demand.stats.reset()
            pharma_chain = scm.simulate_sc_net(pharma_chain,sim_time=T,logging=False)

            shortage = distributor.stats.shortage[1]
            waste = distributor.stats.inventory_waste
            holding = distributor.stats.inventory_carry_cost
            transport = distributor.stats.transportation_cost

            total_cost = (shortage * b + waste * z + holding + transport)
            norm_cost = total_cost / ((T - 30) * (b + z + h + o)) * price
            costs.append(norm_cost)

        mean_cost = np.mean(costs)
        std_dev = np.std(costs)
        std_err = std_dev / np.sqrt(num_replications)

        #print(f"[{s}, {mean_cost}, {std_dev}, {std_err}]")
        results.append((s, mean_cost, std_dev, std_err))

    return results# Parameters for the simulation
s_low = 100
s_high = 5000
s_step = 100
capacity = 5000
ini_level = 5000
num_rep = 1000
Assessing Time Complexity
First, let's evaluate the time complexity in relation to the number 
of simulation runs (replications) (R) in order to estimate the expected 
costs. The code estimates the execution time for R simulations, where R 
takes on the values of 1000, 2000, 3000, 4000, and 5000.

import time
stats = []
scm.global_logger.disable_logging() # enable loggingfor replications in [1000, 2000, 3000, 4000, 5000]:
    exp_cost_arr = []
    start_time = time.time()
    for rep in range(0, replications):
        exp_cost_arr.append(single_sim_run(s=2000,S=5000,ini_level=5000,logging=False))
    exp_cost_arr = np.array(exp_cost_arr)
    exe_time = time.time() - start_time
    print(f"R ={replications}, exe_time:{exe_time} sec, mean:{np.mean(exp_cost_arr)}, std:{np.std(exp_cost_arr)}, std_err:{np.std(exp_cost_arr)/np.sqrt(R)}")
    stats.append((replications, exe_time, np.mean(exp_cost_arr), np.std(exp_cost_arr), np.std(exp_cost_arr)/np.sqrt(replications)))
R =1000, exe_time:22.119476795196533 sec, mean:51.75029532053344, std:23.668711396825312, std_err:0.33472612661285R =2000, exe_time:45.451146602630615 sec, mean:51.80183997566808, std:23.633314688276155, std_err:0.33422554155991413R =3000, exe_time:68.95947742462158 sec, mean:51.01044921449537, std:23.62288667057547, std_err:0.3340780671193043R =4000, exe_time:86.83561849594116 sec, mean:51.584926030610674, std:23.706247962166834, std_err:0.3352569738107588R =5000, exe_time:106.06455659866333 sec, mean:52.27106894743466, std:23.863645906143574, std_err:0.3374829168813743
Estimating Confidence Interval
To ensure the estimation error is reasonable, we estimate the expected costs for the replenishment policy setting  and variable  with  simulation runs and plot the confidence intervals.

exp_cost_per_day = run_for_s(s_low=s_low,s_high=s_high,s_step=s_step,capacity=capacity,ini_level=ini_level,num_replications=num_rep)
exp_cost_per_day = np.array(exp_cost_per_day)

figure(figsize=(25, 10), dpi=60)
plt.plot(exp_cost_per_day[:,0], exp_cost_per_day[:,1],marker='o', linestyle='-', color='b', label='S=5000')
plt.fill_between(exp_cost_per_day[:,0], exp_cost_per_day[:,1]-2*exp_cost_per_day[:,3], exp_cost_per_day[:,1]+2*exp_cost_per_day[:,3],alpha=0.2, color='b', label='95% CI')
plt.xlabel('Reorder Point (s)')
plt.ylabel('Expected Cost per Day')
plt.xticks(np.arange(s_low, s_high, 200))
plt.title('Expected Cost per Day vs Reorder Point (s)')
plt.legend()
plt.grid()
plt.show()


Running the Model with Varying (S, s)
To determine the optimal values of the parameters  and , we run the model with different settings for both parameters. The parameter  takes on values of 1000, 2000, 3000, 4000, and 5000. For each value of , the parameter  is set to values ranging from 100 up to .
 The results obtained from these runs are then plotted and compared with
 the findings reported by the authors. Below, we present both sets of 
plots for comparison.

exp_cost_per_day = run_for_s(s_low=s_low, s_high=5000, s_step=s_step, capacity=5000, ini_level=5000, num_replications=num_rep)
exp_cost_per_day = np.array(exp_cost_per_day)

exp_cost_per_day2 = run_for_s(s_low=s_low,s_high=4000,s_step=s_step,capacity=4000,ini_level=4000,num_replications=num_rep)
exp_cost_per_day2 = np.array(exp_cost_per_day2)

exp_cost_per_day3 = run_for_s(s_low=s_low,s_high=3000,s_step=s_step,capacity=3000,ini_level=3000,num_replications=num_rep)
exp_cost_per_day3 = np.array(exp_cost_per_day3)

exp_cost_per_day4 = run_for_s(s_low=s_low,s_high=2000,s_step=s_step,capacity=2000,ini_level=2000,num_replications=num_rep)
exp_cost_per_day4 = np.array(exp_cost_per_day4)

exp_cost_per_day5 = run_for_s(s_low=s_low,s_high=1000,s_step=s_step,capacity=1000,ini_level=1000,num_replications=num_rep)
exp_cost_per_day5 = np.array(exp_cost_per_day5)

figure(figsize=(25, 10), dpi=60)
plt.plot(exp_cost_per_day[:,0], exp_cost_per_day[:,1],marker='o', linestyle='-', color='m', label='S=5000')
plt.plot(exp_cost_per_day2[:,0], exp_cost_per_day2[:,1],marker='o', linestyle='-', color='b', label='S=4000')
plt.plot(exp_cost_per_day3[:,0], exp_cost_per_day3[:,1],marker='o', linestyle='-', color='g', label='S=3000')
plt.plot(exp_cost_per_day4[:,0], exp_cost_per_day4[:,1],marker='o', linestyle='-', color='c', label='S=2000')
plt.plot(exp_cost_per_day5[:,0], exp_cost_per_day5[:,1],marker='o', linestyle='-', color='r', label='S=1000')
plt.xticks(np.arange(s_low, s_high, 200))
plt.xlabel('Reorder Point (s)')
plt.ylabel('Expected Cost per Day')
plt.title('Expected Cost per Day vs Reorder Point (s)')
plt.legend()
plt.grid()
plt.show()

The following plot displays the results obtained by the authors.         Complex Supply Chain Network
This example shows how to build and simulate a multi-echelon, hybrid supply chain in SupplyNetPy, using a mix of replenishment policies. By multi-echelon
 we mean a chain with several layers — for instance, a raw-material 
supplier feeding a factory, the factory feeding distributors, and the 
distributors feeding retailers. By hybrid we mean that a
 downstream node can be replenished by more than one upstream node (so 
retailers can order from either of two distributors).


Goals
Creating a network with multiple raw materials, suppliers, a manufacturer, two distributors, and several retailers.
Mix replenishment policies (SS, RQ, Periodic).
Include hybrid connections (ordering from multiple distributors).
Key Concepts Used
Products, Raw Materials: Class Product used to create a product, Bread, with some shelf life, RawMaterial is used to create raw materials (dough, sugar, yeast)
Nodes: Classes Supplier, Manufacturer, InventoryNode are used to create suppliers,  bakery (factory), distributors, and retailers (cake shops).
Links: Class Link is used to link different nodes in the network
Policies:SSReplenishment: order up to S when inventory <= s
RQReplenishment: reorder point R, fixed order quantity Q
PeriodicReplenishment: review every T, order Q.
Perishability: inventory_type for all nodes is perishable, and parameter shelf_life is passed. 
Full Example
This script constructs a hybrid network with two distributors and five retailers, then runs a short simulation.
import SupplyNetPy.Components as scmimport simpyimport numpy as npclass Distributions:
    def __init__(self, lam=5, mu=5, low=1, high=10):
        self.lam = lam
        self.mu = mu
        self.low = low
        self.high = high

    def poisson_arrival(self):
        return np.random.exponential(scale=1/self.lam)

    def uniform_quantity(self):
        return int(np.random.uniform(low=self.low, high=self.high))


env = simpy.Environment()# ------------------- Raw Materials -------------------
flour = scm.RawMaterial(ID='flourk11', name='Flour', extraction_quantity=50,
                        extraction_time=1, mining_cost=10, cost=2.5)

sugar = scm.RawMaterial(ID='sugark21', name='Sugar', extraction_quantity=30,
                        extraction_time=1, mining_cost=8, cost=1.4)

yeast = scm.RawMaterial(ID='yeast31', name='Yeast', extraction_quantity=20,
                        extraction_time=1, mining_cost=3, cost=0.2)# ------------------- Suppliers -------------------
flour_mill = scm.Supplier(env=env, ID='fmill', name='Flour Mill',
                          node_type='infinite_supplier', raw_material=flour, logging=False)

sugar_factory = scm.Supplier(env=env, ID='sfact', name='Sugar Factory',
                             node_type='infinite_supplier', raw_material=sugar, logging=False)

yeast_factory = scm.Supplier(env=env, ID='yfact', name='Yeast Factory',
                             node_type='infinite_supplier', raw_material=yeast, logging=False)# ------------------- Manufacturer -------------------
bread = scm.Product(ID='soft_regular11', name='Bread', manufacturing_cost=10, manufacturing_time=1,
                    sell_price=40, raw_materials=[(flour, 30), (sugar, 15), (yeast, 15)], batch_size=300)

bakery = scm.Manufacturer(env=env, ID='bakery', name='Bakery', product=bread, shelf_life=5,
                          inventory_type='perishable', capacity=500, initial_level=100, 
                          inventory_holding_cost=0.5, replenishment_policy=scm.SSReplenishment, 
                          policy_param={'s':300,'S':400}, product_sell_price=35, logging=False)# ------------------- Distributors -------------------
distributor1 = scm.InventoryNode(env=env, ID='dist1', name='Distributor 1', node_type='distributor', product=bread,
                                 inventory_type='perishable', shelf_life=5,
                                 capacity=float('inf'), initial_level=50, inventory_holding_cost=0.2,
                                 replenishment_policy=scm.RQReplenishment, policy_param={'R':150,'Q':50},
                                 product_buy_price=0, product_sell_price=36, logging=False)

distributor2 = scm.InventoryNode(env=env, ID='dist2', name='Distributor 2', node_type='distributor', product=bread,
                                 inventory_type='perishable', shelf_life=5,
                                 capacity=float('inf'), initial_level=40, inventory_holding_cost=0.25,
                                 replenishment_policy=scm.SSReplenishment, policy_param={'s':150,'S':200},
                                 product_buy_price=0, product_sell_price=37, logging=False)# ------------------- Retailers -------------------
cake_shops = []
capacities = [40,60,80,60,100]
ini_levels = [30,20,30,20,50]
policies = [scm.RQReplenishment, scm.PeriodicReplenishment, scm.SSReplenishment, scm.RQReplenishment, scm.PeriodicReplenishment]
policy_params = [{'R':20,'Q':40}, {'T':3,'Q':50}, {'s':20,'S':50}, {'R':20,'Q':40}, {'T':3,'Q':100}]
supplier_selection = [scm.SelectFirst,scm.SelectAvailable,scm.SelectFirst,scm.SelectAvailable,scm.SelectFirst]for i in range(0,5):
    shop = scm.InventoryNode(env=env, ID=f'cake_shop{i+1}', name=f'Cake Shop {i+1}', node_type='retailer', product=bread,
                               inventory_type='perishable', shelf_life=5,
                               capacity=capacities[i], initial_level=ini_levels[i], inventory_holding_cost=0.1,
                               replenishment_policy=policies[i], policy_param=policy_params[i],
                               supplier_selection_policy=supplier_selection[i],
                               product_buy_price=25, product_sell_price=40, logging=False)
    cake_shops.append(shop)# ------------------- Links -------------------
links = []# Raw material links, suppliers to bakery
suppliers = [flour_mill, sugar_factory, yeast_factory]
costs = [5, 6, 5]for i in range(3): 
    links.append(scm.Link(env=env, ID=f'l{i+1}', source=suppliers[i], sink=bakery, cost=costs[i], lead_time=lambda: 0.3))# Bakery → Distributors
links.append(scm.Link(env=env, ID='l4', source=bakery, sink=distributor1, cost=6, lead_time=lambda: 1))
links.append(scm.Link(env=env, ID='l5', source=bakery, sink=distributor2, cost=7, lead_time=lambda: 1.2))# Distributor 1 → Shops 1, 2, 3for i in range(0,3):
    link = scm.Link(env=env, ID=f'l{i+6}', source=distributor1, sink=cake_shops[i], cost=4, lead_time=lambda: 0.4 + i*0.1)
    links.append(link)# Distributor 2 → Shops 4, 5for i in range(3,5):
    link = scm.Link(env=env, ID=f'l{i+6}', source=distributor2, sink=cake_shops[i], cost=5, lead_time=lambda: 0.5)
    links.append(link)# Hybrid cross-links
links.append(scm.Link(env=env, ID='l11', source=distributor1, sink=cake_shops[3], cost=8, lead_time=lambda: 0.5))
links.append(scm.Link(env=env, ID='l12', source=distributor2, sink=cake_shops[4], cost=6, lead_time=lambda: 0.8))# ------------------- Demands -------------------
arrival = Distributions(lam=5)
quantity = Distributions(low=1, high=5)

demands = []for i in range(5):
    demands.append(scm.Demand(env=env, ID=f'd{i+1}', name=f'Demand Shop {i+1}', order_arrival_model=arrival.poisson_arrival,
                          order_quantity_model=quantity.uniform_quantity, demand_node=cake_shops[i], logging=False))# ------------------- Network -------------------
bread_chain = scm.create_sc_net(
    env=env,
    nodes=[flour_mill, sugar_factory, yeast_factory, bakery,
           distributor1, distributor2,
           *cake_shops],
    links=links,
    demands=demands
)

scm.simulate_sc_net(bread_chain, sim_time=365, logging=True)print("---- Node-wise performance ----\n---- ---- Suppliers and Bakery---- ----")
scm.print_node_wise_performance([flour_mill, sugar_factory, yeast_factory, bakery])print("\n---- ---- Distributors ---- ----")
scm.print_node_wise_performance([distributor1, distributor2])print("\n---- ---- Retail Shops ---- ----")
scm.print_node_wise_performance(cake_shops)
Sample Output
INFO sim_trace - Supply chain info:INFO sim_trace - available_inv                     : 534INFO sim_trace - avg_available_inv                 : 365.46027397260275INFO sim_trace - avg_cost_per_item                 : 1.8309188338110083INFO sim_trace - avg_cost_per_order                : 352.90117548464065INFO sim_trace - backorders                        : [182, 23989]INFO sim_trace - demand_by_customers               : [9175, 22985]INFO sim_trace - demand_by_site                    : [436, 1829491]INFO sim_trace - demands                           : {'d1': Demand Shop 1, 'd2': Demand Shop 2, 'd3': Demand Shop 3, 'd4': Demand Shop 4, 'd5': Demand Shop 5}INFO sim_trace - env                               : <simpy.core.Environment object at 0x00000278F5714A10>INFO sim_trace - fulfillment_received_by_customers : [911, 2193]INFO sim_trace - fulfillment_received_by_site      : [412, 1827149]INFO sim_trace - inventory_carry_cost              : 59324.19758288176INFO sim_trace - inventory_spend_cost              : 3056583.0INFO sim_trace - inventory_waste                   : 25961INFO sim_trace - links                             : {'l1': fmill to bakery, 'l2': sfact to bakery, 'l3': yfact to bakery, 'l4': bakery to dist1, 'l5': bakery to dist2, 'l6': dist1 to cake_shop1, 'l7': dist1 to cake_shop2, 'l8': dist1 to cake_shop3, 'l9': dist2 to cake_shop4, 'l10': dist2 to cake_shop5, 'l11': dist1 to cake_shop4, 'l12': dist2 to cake_shop5}INFO sim_trace - nodes                             : {'fmill': Flour Mill, 'sfact': Sugar Factory, 'yfact': Yeast Factory, 'bakery': Bakery, 'dist1': Distributor 1, 'dist2': Distributor 2, 'cake_shop1': Cake Shop 1, 'cake_shop2': Cake Shop 2, 'cake_shop3': Cake Shop 3, 'cake_shop4': Cake Shop 4, 'cake_shop5': Cake Shop 5}INFO sim_trace - num_distributors                  : 2INFO sim_trace - num_manufacturers                 : 1INFO sim_trace - num_of_links                      : 12INFO sim_trace - num_of_nodes                      : 11INFO sim_trace - num_retailers                     : 5INFO sim_trace - num_suppliers                     : 3INFO sim_trace - profit                            : -2226001.1975828814INFO sim_trace - revenue                           : 1165732INFO sim_trace - shortage                          : [8446, 34633]INFO sim_trace - total_cost                        : 3391733.1975828814INFO sim_trace - total_demand                      : [9611, 1852476]INFO sim_trace - total_fulfillment_received        : [1323, 1829342]INFO sim_trace - transportation_cost               : 2466---- Node-wise performance -------- ---- Suppliers and Bakery---- ----Performance Metric       Flour Mill               Sugar Factory            Yeast Factory            Bakerybackorder                [0, 0]                   [0, 0]                   [0, 0]                   [132, 22135]demand_fulfilled         [21, 898260]             [21, 449130]             [21, 449130]             [214, 26936]demand_placed            [0, 0]                   [0, 0]                   [0, 0]                   [63, 1796520]demand_received          [21, 898260]             [21, 449130]             [21, 449130]             [228, 29138]fulfillment_received     [0, 0]                   [0, 0]                   [0, 0]                   [63, 1796520]inventory_carry_cost     0                        0                        0                        53334.0inventory_level          0                        0                        0                        500inventory_spend_cost     0                        0                        0                        2964258.0inventory_waste          0                        0                        0                        243node_cost                0                        0                        0                        3291288.0shortage                 [0, 0]                   [0, 0]                   [0, 0]                   [132, 12825]profit                   2245650.0                628782.0                 89826.0                  -2348528.0revenue                  2245650.0                628782.0                 89826.0                  942760total_material_cost      0                        0                        0                        N/Atotal_raw_materials_mined0                        0                        0                        N/Atransportation_cost      0                        0                        0                        336---- ---- Distributors ---- ----Performance Metric       Distributor 1            Distributor 2backorder                [26, 805]                [24, 1049]demand_fulfilled         [60, 1389]               [75, 2304]demand_placed            [124, 6200]              [104, 22938]demand_received          [67, 1449]               [78, 2384]fulfillment_received     [116, 5800]              [98, 21136]inventory_carry_cost     1165.8018979669055       4439.666173296053inventory_level          7                        27inventory_spend_cost     0                        0inventory_waste          4842                     19345node_cost                1909.8018979669055       5167.666173296053shortage                 [26, 470]                [24, 691]profit                   48094.19810203309        80080.33382670395revenue                  50004                    85248transportation_cost      744                      728---- ---- Retail Shops ---- ----Performance Metric       Cake Shop 1              Cake Shop 2              Cake Shop 3              Cake Shop 4              Cake Shop 5            backorder                [0, 0]                   [0, 0]                   [0, 0]                   [0, 0]                   [0, 0]                 demand_fulfilled         [39, 96]                 [239, 555]               [39, 105]                [310, 757]               [284, 680]             demand_placed            [8, 198]                 [52, 947]                [7, 304]                 [29, 1160]               [49, 1224]             demand_received          [39, 96]                 [239, 555]               [39, 105]                [310, 757]               [284, 680]             fulfillment_received     [8, 198]                 [45, 887]                [7, 304]                 [28, 1120]               [47, 1184]             inventory_carry_cost     9.42778086039718         81.4112372198074         15.98847104347198        148.6863226007502        129.21569989437646     inventory_level          0                        0                        0                        0                        0                      inventory_spend_cost     4950                     22175                    7600                     28000                    29600                  inventory_waste          102                      352                      200                      383                      494                    node_cost                4991.427780860397        22464.411237219807       7643.988471043472        28293.68632260075        29974.215699894376     shortage                 [1759, 4320]             [1595, 3992]             [1787, 4534]             [1567, 3925]             [1556, 3876]profit                   -1151.4277808603974      -264.41123721980694      -3443.9884710434717      1986.3136773992483       -2774.2156998943756    revenue                  3840                     22200                    4200                     30280                    27200                  transportation_cost      32                       208                      28                       145                      245 
Suggested Experiments
Vary policy_param values (s/S, R/Q, T/Q).
Change lead_time lambdas and link costs.
Switch retailer inventory_type and shelf_life to study perishability.
Add/remove cross‑links to test resilience.
Notes
Keep node IDs unique.
Ensure product_buy_price ≤ upstream product_sell_price where applicable.
Use consistent time units across processing, lead times, and review periods.