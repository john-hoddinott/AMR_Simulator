# Staff-Assisted Handling

## Aim

This page explains the staff-assisted handling model and how it affects
generated tasks.

This feature is different from a porter acting as the delivery resource. Porter
and hybrid delivery are covered in
[Porter And Hybrid Delivery](08a-porter-and-hybrid-delivery.md).

## What This Feature Represents

Staff-assisted handling models staff availability for payload handling and
related local travel around a task.

It can affect:

- when staff are available
- how long handling takes
- whether staff need to travel between locations
- whether generated tasks are spread across staff working hours
- staff-related events written to simulation outputs

The assisted-handling staff do not replace the AMR or porter assigned to move
the payload. They represent additional people needed at an endpoint or for a
configured handling activity.

## Key Concepts

**Requires staff**

A category or task can mark that staff are required.

**Staff initial count**

The number of staff available for a staff resource pool before shift-pattern
rules are applied.

**Staff resource name**

A name used to identify the staff resource group.

**Staff movement policy**

Controls how staff movement is handled. The default value in the config model is
`batch_same_location`.

**Staff shift pattern**

Defines when staff are available. The code recognises a default `none` pattern
and a `four_on_four_off_12h` style pattern.

**Staff handling minutes**

The handling time associated with the staff-assisted task.

**Custom working hours**

Some categories can define working hours by day instead of using the global
staff pattern.

## Global Staff Configuration

The `task_generation.staff_config` section contains shared staff assumptions.

These include:

- whether staff support is enabled
- walking speed
- lift wait assumptions
- default handling time
- whether timeframe tasks should be spread across staff hours
- shift pattern definitions

These values can affect generated task timing and staff assignment outputs.

## Category-Level Staff Settings

Task-generation categories can include staff-related settings.

For each category, check:

- whether `requires_staff` is enabled
- staff resource name
- staff initial count
- movement policy
- shift pattern
- handling minutes
- custom working hours

These settings should reflect the operational assumption for that workflow.

## Staff And Timeframe Tasks

The task-generation code can spread timeframe-based tasks across staff hours
when staff support is enabled.

This means a category configured for a broad timeframe may release tasks
differently when staff constraints are active.

If task release timing looks unexpected, check both the task timeframe and staff
availability settings.

## Staff Travel And Handling Events

The simulator can write staff-related events such as:

- staff travel
- staff payload handling
- staff wait time
- staff shift pattern
- staff team
- staff people required

These events help explain delays caused by endpoint staff-handling assumptions.
They should not be combined with porter-delivery events when interpreting the
delivery fleet.

## Worked Example: Catering Delivery

In a catering task-generation category, department-specific scheduled deliveries
can move full food trolleys from a catering pickup point to ward or department
catering locations. These deliveries can require staff support from a **Host
team** to handle the trolley at the receiving end. Example settings include:

- category: `catering`
- generation mode: `scheduled`
- payload: `Burlodge Trolley`
- pickup location: `D28-CATERING`
- example dropoff locations: `D1-CATERING`, `D21-CATERING`, `D29-CATERING`
- `requires_staff`: `true`
- `staff_resource_name`: `Host team`
- `staff_movement_policy`: `minimise_movement`
- `staff_shift_pattern`: `four_on_four_off_12h`

In practical terms, this means the model can generate scheduled catering tasks
for departments and include a Host team availability and handling assumption
around those trolley delivery tasks. It does not model kitchen preparation or
the detail of serving individual patients.

The same pattern can be used for stores deliveries with a **Stores team**. For
example, a stores workflow could use `requires_staff: true`,
`staff_resource_name: Stores team`, and `staff_movement_policy:
batch_same_location`.

These examples are useful because they show that staff-assisted handling can be
configured per workflow and, where needed, per department.

## Step 1: Decide Whether Staff Handling Is Needed

Do not enable staff handling just because a workflow involves people in real
life.

Use it when the model needs to represent staff availability or handling time at
the pickup or dropoff stage.

## Step 2: Set A Clear Staff Resource

Give the staff resource a clear name linked to the workflow.

For example:

- pharmacy receiving staff
- stores handling staff
- waste handling staff
- theatre receipt staff

Avoid using vague names if several workflows require different assumptions.

## Step 3: Choose Availability Assumptions

Choose whether the workflow should use:

- no shift constraint
- a shared shift pattern
- custom working hours

Keep early assumptions simple. More precise shift patterns should be added only
when they are needed to answer a modelling question.

## Step 4: Review Output Carefully

After running a staff-assisted scenario, check:

- whether staff events appear
- whether tasks were delayed waiting for staff
- whether handling time is included
- whether timeframe tasks were spread differently
- whether staff assumptions changed task completion performance

## Common Problems

**Tasks appear later than expected**

Staff working hours or shift patterns may be limiting when tasks can be
released or handled.

**Staff events are missing**

The category may not require staff, global staff support may be disabled, or the
task may not have staff fields.

**Staff demand looks too high**

Check staff initial count, shift multiplier, movement policy, and whether many
generated tasks are being released together.

## Modelling Notes

Staff-assisted handling is useful when the operational question is about whether
endpoint staff availability affects task timing. Use a staff-delivery resource,
not this feature, when the person is intended to accept the delivery task, walk
to the pickup, accompany the payload and complete the drop-off journey.
