# Start Here: What the Simulator Does

## Aim

This page gives a plain-English overview of the hospital logistics simulator
toolchain and the main ideas used throughout the user guide.

## What The Simulator Is For

The simulator is a modelling tool for exploring how Automated Mobile Robots
(AMRs), porters, or a hybrid of both could move goods around a hospital. A
scenario describes the hospital layout, delivery resources, payloads and the
tasks or rules that create logistics work.

The simulator can then estimate how those resources move through the modelled
hospital, how long tasks take, where tasks remain pending or fail, how routes
and lifts are used, and what outputs are produced for review.

## The Main Tools

The project currently contains several separate tools:

- **Editor**: used to view and edit scenario configuration files.
- **Simulator**: runs a scenario and writes output files.
- **Visualiser**: replays or displays simulation outputs against the layout.
- **Report generator**: creates a PDF report from simulation outputs.
- **Launcher**: provides a single front door for managing configs, runs,
  reports, and visualisation.

The launcher does not replace the other tools. It helps users move between them
more easily and keeps launcher-managed configs and runs in a consistent folder
structure.

## Key Concepts

**Scenario config**

A JSON file that defines the model. It includes the hospital floors and layout
references, graph nodes and edges, departments, delivery resources, payloads,
route profiles, manual tasks, and task-generation settings.

**Layout**

The DXF floorplan files used as visual references in the editor and visualiser.
The route graph is drawn over these layouts.

**Route graph**

The network of nodes and edges that AMRs and porters use to move through the
hospital. The simulator uses this graph for routing and distance calculations.

**Delivery resources**

The configured AMR and staff resource types. Each type has a quantity and its
own speed, capacity, compatibility and availability assumptions.

**AMR fleet**

The configured robot types and quantities. Each AMR type can have different
capacity, size, speed, battery, and payload compatibility assumptions.

**Staff-delivery resources**

The configured porter types and quantities. Each individual porter has a base,
walking speed, roster, contractual breaks, handling and turnaround time,
compatibility rules and an optional task-acceptance delay.

**Payload**

The type of item being moved, such as waste, linen, pharmacy items, stores, or
case carts.

**Task**

A transport job. A task normally has a pickup location, dropoff location,
payload type, release time, priority and delivery-resource policy.

**Delivery-resource policy**

The rule that permits `amr`, `staff` or `either`. An `either` flow can prefer
AMR, prefer staff or select the earliest estimated completion. Optional windows
can vary that preference by day and time.

**Task generation**

Rules that create tasks automatically from departments, schedules, timeframes,
thresholds, or waste-volume assumptions. This is separate from manually defined
tasks.

**Run**

One execution of a scenario config. A run has its own folder containing a copy
of the config used, output CSV files, a run manifest, and optionally a report.

## What A Typical Workflow Looks Like

1. Import or create a scenario config in the launcher.
2. Open the config in the editor if changes are needed.
3. Run the selected config from the launcher.
4. Review whether the run completed successfully.
5. Generate a report for a completed run.
6. Open the visualiser to inspect movement and routes.
7. Compare outputs with other scenario runs.

## What The Simulator Does Not Yet Fully Model

Some operational concepts may need further development before they can be used
for formal comparison.

Porter-led delivery is implemented, but it remains a simplified operational
model. It represents shifts, breaks, response delay, walking, handling,
turnaround, routing and lift use; it does not reproduce informal communication,
local workarounds, fatigue, variable walking behaviour or every interruption
that affects a real porter service.

Staff-assisted handling is a separate concept. It models people who support
handling at a pickup or drop-off point and should not be confused with the
porter who performs the delivery journey.

Similarly, the route graph depends on the quality of the graph drawn over the
DXF layouts. Users should treat route and distance results as only as reliable
as the underlying graph and floorplan scaling.

## What To Check Before Trusting A Run

- The selected config is the intended scenario.
- The required floor layouts are present.
- The route graph connects the relevant pickup and dropoff locations.
- AMR and porter quantities, availability and payload assumptions are
  appropriate.
- Each flow permits the intended delivery resource and uses the intended
  preference policy.
- Tasks or task-generation rules reflect the intended operational workflow.
- The run completed successfully.
- The report and visualiser outputs match the run being reviewed.

## Modelling Note

The simulator is best used as a comparative modelling tool. Its strongest use
case is comparing scenarios under consistent assumptions, rather than treating a
single run as a precise prediction of future operations.
