# Task Generation Basics

## Aim

This page introduces task generation: the process of creating transport tasks
automatically from rules in the scenario config.

Task generation is one of the most powerful parts of the simulator, but it is
also one of the easiest places to create confusing scenarios. Start simple.

## Manual Tasks Versus Generated Tasks

Manual tasks are listed explicitly in the config.

Generated tasks are created by rules while the simulator runs.

Manual tasks are useful for testing known journeys. Generated tasks are useful
when modelling recurring operational activity across departments, days, and
workflows.

## Where Task Generation Is Configured

Task generation is configured in the `task_generation` section of the scenario
config.

The editor supplies default task-generation settings. The simulator then reads
those settings and creates generators when task generation is enabled.

The implementation currently includes:

- category-based task generation
- department-driven generation
- scheduled tasks
- timeframe-based tasks
- threshold or volume-triggered tasks
- waste-related generation
- optional return or exchange tasks
- staff-assisted handling settings
- AMR, porter or mixed delivery-resource policies

Not every scenario uses every mode.

## Task Categories

Task generation is organised around categories. A category represents a broad
workflow or logistics stream.

Common categories include:

- catering
- pharmacy
- linen
- waste
- stores
- SSD or sterile services

Categories can define payloads, pickup and dropoff assumptions, schedules,
timeframes, route profiles, delivery-resource policies and staff-handling
settings.

## Department-Based Generation

Many generated tasks are linked to departments.

A category may use department locations to decide where tasks should start or
end. This allows the same workflow to generate tasks for many departments
without writing each task manually.

For example, a linen category could generate delivery tasks to several ward
locations, each with its own department configuration.

## Scheduled Generation

Scheduled generation creates tasks at specified times.

This is useful when a workflow happens at known points in the day, such as a
regular delivery round.

When reviewing scheduled generation, check:

- scheduled times
- active days
- pickup and dropoff locations
- payload
- route profile

## Timeframe-Based Generation

Timeframe generation creates one or more tasks within a time window.

For example, a category may specify a start and end time such as `09:00` to
`17:00`. Generated tasks can then be placed within that window.

The code includes support for spreading tasks across staff hours when
staff-assisted handling is enabled. This means the generated release pattern may
depend on both the task timeframe and staff availability assumptions.

## Threshold Or Volume-Triggered Generation

Some generated tasks are based on quantities or thresholds rather than fixed
times.

This is useful for workflows where work is triggered by accumulation, such as:

- waste containers reaching a threshold
- tracked items falling below a top-up level
- volume-based collection rules

These models need careful validation because small changes to thresholds can
change the number and timing of generated tasks.

## Return Or Exchange Tasks

Some generated workflows may create a related return or exchange task.

For example, an outbound delivery might later require a return movement. The
simulator can create this as a separate generated task when configured to do so.

This is useful for closed-loop logistics, but it can make demand harder to
interpret because one operational request may produce more than one simulated
task.

## Staff-Assisted Handling

Task generation categories can include staff-related fields.

These model staff availability for payload handling and related local travel at
pickup or dropoff locations. They are separate from a staff-delivery resource
that performs the full porter journey.

Staff-assisted handling is covered in more detail in a later section.

## Delivery-Resource Selection

Each generated category can set the same delivery policy used by a manual task:

- `amr` for compatible AMRs only;
- `staff` for compatible staff-delivery resources only;
- `either` for both resource classes.

For `either`, the default can be `earliest_completion`, `prefer_amr` or
`prefer_staff`. Optional preference windows can apply a different rule on
selected days and between selected times. Leaving the schedule empty makes the
default apply continuously.

Eligibility can also be narrowed by AMR type, staff type or required
capabilities. Payload mass, dimensions and allowed payload types are checked in
addition to those explicit restrictions.

## Step 1: Check Whether Task Generation Is Enabled

Before interpreting a scenario, check whether task generation is enabled.

If it is enabled, the number of tasks in the config may not represent the total
workload. Additional tasks may be created during the run.

## Step 2: Identify Active Categories

Review which categories are enabled and what each one represents.

For each active category, check:

- payload
- pickup and dropoff logic
- departments used
- schedule or timeframe
- route profile
- delivery-resource mode, selection preference and eligibility restrictions
- staff requirements
- return or exchange behaviour

## Step 3: Start With One Category

When learning or testing, enable one category at a time.

Run a short scenario and check that generated tasks match the intended
operational behaviour before enabling several categories together.

## Step 4: Review Generated Outputs

After running the simulation, review:

- how many tasks were generated
- when they were released
- which departments were involved
- whether tasks completed or failed
- whether return tasks were created
- whether staff-handling events appeared

The generated task output is the best evidence of what the task-generation rules
actually did.

## Common Problems

**The run creates more tasks than expected**

Multiple categories may be enabled, or return/exchange tasks may be creating
additional work.

**No generated tasks appear**

Task generation may be disabled, categories may be disabled, departments may be
inactive, or required category locations may be missing.

**Tasks appear at unexpected times**

Check scheduled times, timeframe windows, department operating hours, active
days, and staff-hour spreading.

**Generated tasks fail**

Check payload compatibility, pickup/dropoff locations, route graph connections,
route profiles, resource eligibility and porter roster windows.

## Modelling Notes

Task generation should be introduced gradually. It is better to understand one
workflow clearly than to run a large multi-category scenario whose demand cannot
be explained.

For operational comparison, record which categories were enabled and what demand
assumptions they used. Otherwise it becomes difficult to compare AMR, porter-led,
and hybrid scenarios fairly.
