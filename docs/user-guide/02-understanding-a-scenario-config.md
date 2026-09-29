# Understanding A Scenario Config

## Aim

This page explains the main sections of a scenario config and how they relate to
the simulator workflow.

The config is the source of truth for a scenario. It defines what is being
modelled before the simulator creates any outputs.

## What A Config Contains

A scenario config is a JSON file. Most users will edit it through the editor
rather than by typing JSON directly.

The main sections are:

- building and simulation settings
- floor layout references
- departments and locations
- route graph nodes and edges
- lifts
- delivery resources: AMR and staff types
- payloads
- route profiles
- manual tasks
- task-generation rules
- waste streams and mass collections

Not every scenario uses every section.

## Floor Layouts

Floor layouts point to DXF files used by the editor and visualiser.

The layout files provide the visual background. The simulator itself relies on
the route graph drawn over the layout, not directly on the drawing geometry.

When reviewing a config, check that every modelled floor has the expected floor
layout file.

## Locations

Locations represent meaningful places in the hospital model, such as stores,
departments, collection points, delivery points, or robot charging areas.

A location normally has:

- a name
- a floor
- x/y coordinates
- optional inventory spaces

Locations are used by tasks, departments, delivery resources, and route graph
connections.

## Departments

Departments represent hospital areas that can drive operational activity.

Depending on the config, a department may include:

- name or department identifier
- operating hours
- active days
- bed count
- patient turnover
- staff count
- task-generation locations
- waste-stream settings

Department data is particularly important for generated tasks and waste-volume
modelling.

## Route Graph

The route graph is the network used for movement.

It contains:

- nodes: points on the route graph
- edges: connections between nodes

A node is often used at a junction between edges. Nodes typically mark places
where a delivery resource may change direction, enter a side corridor, access a
room, or enter a lift. Some nodes may also act as endpoints or connection
points for locations.

The route graph is drawn over the floor layouts in the editor. The simulator
uses this graph to calculate routes and distances.

If a pickup or dropoff location is not properly connected to the graph, tasks
may fail or route unexpectedly.

## Lifts

Lifts connect floors. They are important because they can become bottlenecks in
multi-floor hospital models.

Lift assumptions may affect:

- which routes are possible
- waiting time
- congestion
- comparison between AMR, porter and hybrid options

## Delivery Resources

The editor's **Delivery Resources** screen holds AMR and staff resource types in
one list. The scenario can contain either type or both.

The `simulation.delivery_operating_model` value labels the intended scenario in
outputs and reports. Supported labels are AMR only, porter only, hybrid with AMR
preference and hybrid with porter preference. This label describes the scenario;
the policy on each task or generated flow controls actual eligibility and
selection.

### AMR Fleet

The AMR fleet defines robot types and quantities.

An AMR type can include:

- quantity
- speed
- payload capacity
- dimensions
- battery assumptions
- start location
- payload slots
- manual task compatibility

The total AMR fleet is the sum of AMR quantities. AMR types are the distinct
configured robot definitions.

### Staff-Delivery Resources

A staff-delivery type can include:

- quantity and generated individual IDs
- walking speed
- base or start location
- payload mass and dimensional capacity
- allowed payload types and required capabilities
- working days, shift start and shift end
- one or more contractual breaks
- turnaround time after a completed delivery
- minimum and maximum task-acceptance delay
- whether response delay applies only when away from base

Porter pickup and drop-off use the scenario's configured building
load/unload time rather than a porter-specific handling-time field.

Breaks are separate daily time windows. A task is assigned only if the porter
can complete it within a valid roster window without crossing a break or shift
end.

Capabilities are explicit resource tags used by a delivery policy. Payload
compatibility is configured separately through allowed payload types, weight and
dimensions. A capability can describe a requirement such as controlled-drug
authorisation or a handling competency; it is not automatically a payload type.

## Payloads

Payloads describe what is being moved.

Examples might include linen, waste, pharmacy items, stores, sterile supplies,
or case carts.

Payload definitions can include size, mass, handling assumptions, and inventory
or tracked-item behaviour.

## Route Profiles

Route profiles define route restrictions or preferences for certain flows.

For example, a config may use different profiles for clean and dirty flows, or
restrict a workflow to particular lifts, nodes, or edges.

Route profiles are important when testing operational separation of flows.

## Manual Tasks

Manual tasks are explicitly listed transport jobs.

A task usually defines:

- task ID
- pickup location
- dropoff location
- payload
- release time
- priority
- optional route profile
- delivery-resource mode and optional eligibility restrictions
- selection policy and optional day/time preference windows

Manual tasks are useful for simple scenarios and for testing specific flows.

## Task Generation

Task generation creates tasks automatically from rules in the config.

Generated tasks may be based on:

- schedules
- timeframes
- departments
- payload categories
- inventory thresholds
- waste-volume calculations

Task generation is powerful, but it is also more complex than manual task
definition. It should be introduced only after the basic config structure is
understood.

Manual tasks and generated categories use the same delivery-resource policy:

- `amr` restricts the flow to compatible AMRs;
- `staff` restricts the flow to compatible staff-delivery resources;
- `either` permits both and applies a selection preference.

An `either` flow can use `earliest_completion`, `prefer_amr` or `prefer_staff`.
Preference windows can change that rule by day and time. With no window, the
default applies 24 hours a day, seven days a week. A preference is not an
exclusive restriction: the dispatcher can fall back to the other permitted
resource when the preferred resource is unavailable or incompatible.

## What The Launcher Summary Shows

The launcher shows a compact summary for each managed config:

- locations and departments
- floor layout coverage
- delivery operating model, AMR fleet and staff-resource types
- payloads
- tasks
- route profiles
- graph nodes and edges
- basic checks

This summary is designed to catch obvious issues quickly. It is not a full
validation of the scenario.

## What To Check Before Editing

Before changing a config, confirm:

- it is a launcher-managed copy, not the only original
- the floor layouts are available
- the route graph appears complete
- AMR and porter quantities, rosters and speeds are plausible
- resource compatibility and selection policies match the intended workflows
- the payloads match the intended workflows
- manual and generated tasks are not being confused

## Modelling Notes

Scenario configs can become complex. The safest approach is to start with a
working config, make one meaningful change at a time, run the simulation, and
record what changed. This makes later comparison much easier.
