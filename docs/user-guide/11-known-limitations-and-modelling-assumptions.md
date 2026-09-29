# Known Limitations And Modelling Assumptions

## Aim

This page summarises important limitations and assumptions to keep in mind when
using the AMR Simulator.

The simulator is most useful as a comparative planning tool. It helps test how
different logistics assumptions affect task completion, route demand, lift use,
staff handling, and bottlenecks. It should not be treated as a direct prediction
of exactly what will happen in a live hospital.

## Model Scope

The simulator models logistics activity across a configured hospital layout.
It can represent AMR movements, task timing, payloads, route restrictions,
lift use, location capacity, task generation, porter delivery and
staff-assisted handling.

It does not capture every real-world operational factor. Examples include:

- ad hoc clinical interruptions
- local workarounds by ward or theatre teams
- infection prevention decisions made outside the configured route rules
- temporary blockages not represented in the scenario
- human behaviour beyond the configured porter and staff-handling assumptions
- equipment faults unless they are explicitly represented in the scenario

## Route Graph And Floor Layouts

The DXF floor layouts are used as a visual reference for building and reviewing
the route graph. The simulator follows the configured graph nodes and edges; it
does not automatically infer safe travel routes directly from the CAD drawing.

This means route accuracy depends on:

- the DXF being displayed at the correct scale
- graph nodes being placed in the right locations
- graph edges matching realistic paths of travel
- lift and floor connections being correctly defined
- route profiles allowing and excluding the intended edges

Where distance accuracy is important, sample graph edge lengths should be
checked against known real-world distances from drawings, surveys, or measured
routes.

## Travel Times

Travel time depends on the configured route graph, route distance, movement
speed, lift behaviour, waiting time, and any task-specific constraints.

Travel-time outputs should therefore be treated as scenario results rather than
universal facts about the building. A small error in route scale, edge placement,
or lift configuration can affect downstream timing and utilisation results.

## Task Demand

Task demand may come from manually defined tasks or generated task rules.

Manually defined tasks are useful for controlled tests because the task list is
explicit. Generated task rules are useful for whole-hospital modelling, but they
depend heavily on configured categories, department settings, dates, frequencies,
volumes, and return-task assumptions.

When comparing scenarios, changes in task generation rules can alter the demand
being tested. This can make two scenarios difficult to compare unless the change
in demand is intentional.

## Payloads And Capacity

Payload definitions and location inventory spaces are simplified
representations of real physical handling.

They can be used to test whether the model creates capacity pressure at receipt
points, collection points, charging areas, and storage locations. They do not
replace an operational review of whether a real space can safely hold, marshal,
clean, or transfer those items.

## Staff-Assisted Handling

Staff-assisted handling represents staff support at a task endpoint, such as
time needed to handle a trolley at a receiving location.

Staff-assisted handling is separate from porter-led delivery. It represents
additional endpoint support and should not be counted as the delivery workforce.

## Porter-Led Delivery

Porter-led logistics is represented through staff-delivery resources that accept
tasks, walk from their current location to the pickup, accompany the payload to
the drop-off and use the configured lifts and route graph.

This remains a simplified human model. Results depend on configured shifts,
breaks, walking speed, handling, turnaround, compatibility and response delay.
The simulator does not currently reproduce informal task allocation, radio or
telephone communication, fatigue, individual behavioural variation, local
workarounds, unplanned clinical requests or every interruption experienced by a
real porter service.

The task-acceptance delay is a reproducible bounded value, not an empirically
calibrated queueing or communication model unless its parameters have been set
from operational evidence.

## Hybrid Dispatch

Hybrid policy chooses between currently feasible resources using a default or
scheduled preference. A preference is not an exclusive allocation rule. The
other permitted resource can be selected when the preferred resource is
unavailable or incompatible.

AMR and porter results within one hybrid run may describe different work. They
must not be treated as a like-for-like performance comparison without checking
flow, payload, route and release-time equivalence.

## Lift Modelling

Lift outputs are central to multi-floor scenarios, but lift modelling is still a
scenario abstraction.

Results depend on:

- which lifts are connected to which floors
- whether route profiles permit those lifts
- assumed lift transfer and waiting behaviour
- the number and timing of tasks needing vertical travel
- whether AMR and porter flows are included in the same lift demand model

A scenario can complete all tasks while still creating lift pressure that would
be operationally unacceptable.

## AMR Fleet Assumptions

AMR performance depends on configured fleet size, speed, payload compatibility,
charging behaviour, route access, and task assignment logic.

The model can help compare fleet assumptions, but it does not by itself prove
that a specific AMR product, vendor system, charging strategy, or fleet
management approach will perform identically in practice.

## Comparative Assumptions

Before using AMR, porter or hybrid differences for a decision, agree and record:

- common task demand, dates and return-generation behaviour;
- AMR and porter quantities;
- AMR speeds, charging and payload compatibility;
- porter walking speed, shifts, breaks and base locations;
- porter response delay, handling and turnaround;
- delivery modes, eligibility restrictions and hybrid preference windows;
- graph scale, route restrictions and lift behaviour;
- service-level definitions by logistics flow; and
- treatment of work that is pending or failed at the simulation horizon.

Without this calibration, comparative outputs demonstrate model behaviour but
do not establish the real-world superiority of an operating model.

## Run Duration And Performance

Whole-hospital simulations across multiple days can take a long time to run.
Complex configurations may take 30 minutes or more, depending on task volume,
simulation length, route complexity, task generation, and output settings.

Shorter focused scenarios are useful during model development because they make
it easier to test one assumption at a time before running a full comparison.

## Output Interpretation

Outputs should be interpreted as evidence from a configured scenario.

Cancelled or failed runs should not be treated as valid operational results.
Failed tasks should be reviewed before relying on utilisation, average timing,
or lift summary figures. A scenario with good-looking averages may still fail
important individual tasks.

## Recommended Checks

Before relying on a scenario comparison, check:

- the run completed successfully
- the intended config was used
- floor layouts and graph routes are plausible
- route distances are credible for sample known journeys
- task demand matches the scenario intent
- delivery-resource policies match the operating-model intent
- AMR and porter assumptions have an identified evidence source
- failed tasks have been reviewed
- pending and unassigned work has been reviewed
- lift activity is plausible in the visualiser
- report conclusions match the scenario question

## Documentation Status

This guide has been drafted from review of the code, example configurations,
and practical testing of the simulator. It should be reviewed alongside local
operational knowledge before being used as formal modelling guidance.
