# Working With Routes And Constraints

## Aim

This page explains how route graphs and route constraints influence the way AMRs
and porters move through a scenario.

Routes are central to the simulator. Realistic delivery-resource assumptions are
not useful if the route graph does not accurately represent the hospital
movement network.

## Key Concepts

**Route graph**

The network of nodes and edges used by the simulator to calculate movement.

**Node**

A point on the route graph. Nodes often represent junctions, turns, lift access
points, side corridor entrances, room access points, or location connection
points.

**Edge**

A connection between two nodes. Edges represent sections of travel that a
delivery resource can use.

**Location connection**

A location must connect to the graph for AMRs or porters to move to or from it.
A delivery point that looks correct on the layout may still fail if it is not
connected to the graph.

**Route profile**

A named set of restrictions that can limit which lifts, nodes, or edges may be
used by a task or workflow.

## Why Routes Matter

The simulator uses the graph to calculate:

- whether a route exists
- which path is chosen
- travel distance
- travel time
- lift demand
- congestion and bottlenecks

The DXF layout is important for visual alignment, but the simulator does not
automatically infer routes from the drawing. The graph is the operational route
network.

## Clean And Dirty Route Separation

Hospitals may require different flows to avoid crossing where possible.

For example:

- clean supplies may use one set of corridors or lifts
- dirty returns may use another
- case carts may have clean and dirty movement assumptions
- waste may be restricted to particular lifts

Route profiles can be used to test these assumptions by limiting which graph
resources are available to a task.

## Lift Constraints

Lifts are often critical bottlenecks in multi-floor hospital models.

Route assumptions should check:

- which lifts each workflow may use
- whether clean and dirty flows share lifts
- whether AMRs and porters compete for lift space
- whether the selected lifts create unrealistic detours

Porters use the existing graph-routing and lift model. AMRs additionally apply
robot-specific physical, route-profile, battery and charging constraints. A
shared graph therefore does not imply that every resource is eligible for every
route or payload.

If a route profile excludes necessary lifts, tasks may fail even if the pickup
and dropoff are otherwise connected.

## Step 1: Check The Graph Visually

Open the config in the editor and inspect the graph over the floor layout.

Check that:

- graph lines follow plausible AMR and porter routes
- nodes are placed at turns, junctions, and access points
- lifts connect floors correctly
- important locations have nearby graph connections
- there are no obvious missing corridor sections

## Step 2: Check Location Connections

For key pickup and dropoff locations, confirm that each location is connected to
the intended route node.

This is especially important for:

- stores and receipt points
- waste collection points
- pharmacy and linen locations
- theatre case cart receipt and return locations
- AMR start and charging locations
- porter bases and shift start locations

## Step 3: Test With A Simple Task

Before running a large scenario, create or select a simple task that exercises
the route being tested.

Run it and inspect the visualiser output.

This is often the quickest way to find missing edges, incorrect lift choices, or
route-profile restrictions that are too tight.

## Step 4: Review Route Profiles

For each route profile, check:

- allowed lifts
- allowed nodes
- allowed edges
- workflows or tasks that use the profile

Route profiles should be named clearly so that users understand the operational
assumption being tested.

## What To Check

When reviewing routes, check:

- all key locations are connected to the graph
- expected floors are linked by lifts
- route profiles do not accidentally block valid routes
- graph distances look plausible compared with the layout
- clean and dirty routes match the operational intent
- lift choices match the scenario assumptions

## Common Problems

**A route appears visually possible but the task fails**

The graph may not contain the necessary node or edge connections. The simulator
uses the graph, not the visual corridor drawing.

**A delivery resource uses the wrong lift**

Check the route profile and lift restrictions. If no restriction applies, the
simulator may choose the shortest available route.

**A route is much longer than expected**

There may be missing graph edges, overly restrictive route profiles, or a lift
constraint forcing a detour.

**Clean and dirty flows overlap**

Check whether the same nodes, edges, or lifts are allowed in both route
profiles.

## Modelling Notes

Route modelling is one of the main sources of scenario risk. A model can appear
detailed while still giving misleading results if the graph does not match the
real operational movement network.

For important scenarios, route assumptions should be reviewed with operational
staff who understand how goods actually move through the hospital.
