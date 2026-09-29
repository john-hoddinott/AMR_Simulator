# Porter And Hybrid Delivery

## Aim

This page explains how staff-delivery resources perform porter journeys and how
the dispatcher selects between AMRs and porters in a hybrid scenario.

## What A Porter Journey Represents

A porter is an alternative delivery resource, not an endpoint handling helper.
For an assigned task the model can represent:

1. task release and acceptance delay;
2. travel from the porter's current location to the pickup;
3. fixed pickup handling time;
4. travel with the payload to the drop-off, including lifts;
5. fixed drop-off handling time; and
6. turnaround before the porter is available again.

The porter remains at the completed task's destination unless later work moves
them elsewhere. Their base is used as their initial location and can affect
whether an away-from-base response delay applies.

## Staff-Delivery Resource Assumptions

For each porter type, review:

- quantity and the generated individual IDs;
- walking speed;
- base location;
- payload mass and dimensional capacity;
- allowed payload types;
- capability tags;
- active days and shift times;
- contractual break windows;
- turnaround after completion; and
- minimum and maximum task-acceptance delay.

Pickup and drop-off use the scenario's configured building load/unload time;
this is not currently set separately for each porter type.

The response delay is reproducible for the same porter/task pair. It can be
configured to apply only when the porter is away from base, representing the
additional time required to receive and accept a task remotely.

## Shifts And Breaks

Working days, shift start and shift end define when each porter can work.
Contractual breaks are separate start/end windows within the day. Multiple
breaks are supported.

A task must fit within a valid roster interval. The dispatcher will not start a
porter journey that cannot finish before the next break or shift end. This is
why a porter-only task may wait for the next roster window or fail when no
compatible window exists within the simulation.

## Payload Compatibility And Capabilities

Payload compatibility and capabilities answer different questions:

- **Payload compatibility** checks allowed payload types, mass and dimensions.
- **Capabilities** are named qualifications or operational attributes required
  by the task, such as controlled-drug authorisation or specialist handling.

A capability is not automatically a payload type. If a flow requires a
capability, at least one otherwise-compatible resource must carry that exact
capability tag.

## Delivery Modes

Each manual task or generated flow can permit:

- `amr` - only compatible AMRs;
- `staff` - only compatible staff-delivery resources;
- `either` - both resource classes.

When the mode is `amr` or `staff`, the selection-policy controls are disabled in
the editor because there is no cross-resource choice to make.

## Hybrid Selection Policies

An `either` task can use:

- `earliest_completion` - choose the feasible resource with the earliest
  estimated completion;
- `prefer_amr` - select AMR when feasible, otherwise fall back to staff;
- `prefer_staff` - select staff when feasible, otherwise fall back to AMR.

A preference is deliberately not a guarantee. Availability, payload capacity,
type restrictions, capabilities, route feasibility, charging, shifts and breaks
can all make the preferred resource infeasible.

## Day And Time Preference Windows

The default selection policy applies 24 hours a day, seven days a week when no
preference schedule is configured.

Windows can override the default for selected days and times. In the editor,
enter one window per line:

```text
mon,tue,wed,thu,fri | 07:00 | 17:00 | prefer_staff
```

Leave the days field empty to apply a window every day. Leave both time fields
empty to apply it for the full day. Overnight windows are supported. Overlapping
windows for the same day are validation errors.

For example, a scenario could prefer porters during two staffed daytime shifts
and prefer AMRs overnight. The actual task allocation will still depend on
feasibility at each task release.

## Shared Routes And Lifts

Porters and AMRs use the configured route graph and lift system. This allows a
hybrid run to show both resource classes competing for shared infrastructure.

AMRs additionally apply robot-specific constraints such as battery state,
charging, payload slots and physical route compatibility. Porter travel uses
walking speed and roster availability instead.

## Visualiser And Report

The visualiser displays porter and AMR symbols and allows either resource class
to be selected in the follow-resource control.

The report states the delivery operating model. It includes porter results for
porter and hybrid scenarios, AMR sections when AMRs are used, and a delivery
allocation table for hybrid runs. The allocation table describes who performed
the work; it is not a controlled comparison of AMR and porter performance because
the two resource classes may have received different flows.

## What To Validate

Before interpreting a porter or hybrid result, validate:

- porter numbers and shift coverage;
- contractual breaks;
- walking speeds and graph distances;
- pickup/drop-off handling and turnaround;
- task-acceptance delay;
- payload and capability rules;
- AMR charging and compatibility;
- selection-policy windows;
- treatment of tasks pending or failing at the horizon; and
- whether the same underlying demand is used across compared scenarios.

## Modelling Note

The feature is suitable for exploring how explicit operating assumptions affect
resource allocation, timing and infrastructure demand. It is not yet evidence
that a real porter, AMR or hybrid service will achieve the reported performance.
Calibrate material assumptions with operational data before using numerical
differences for investment or workforce decisions.
