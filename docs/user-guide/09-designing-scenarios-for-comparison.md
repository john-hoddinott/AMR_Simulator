# Designing Scenarios For Comparison

## Aim

This page explains how to design scenarios so that simulation results can be
compared fairly.

The simulator is most useful when comparing options under consistent
assumptions. A single run can be informative, but comparison is where modelling
usually becomes operationally useful.

## The Core Questions

Scenario comparisons should be designed around clear operational questions:

- Are the needs of receiving departments met?
- Are lift demand and key bottlenecks within tolerance?
- What is the relative benefit of different logistics models?
- Which workflows create the greatest pressure on the system?
- Which assumptions materially change the result?

These questions should guide what is changed between scenarios.

## Keep One Baseline

Start with one baseline scenario that represents the best current understanding
of the hospital, workflows, AMR fleet, routes, and task demand.

The baseline should be:

- named clearly
- documented
- runnable
- reproducible
- understood before variants are created

Do not keep editing the baseline for every question. Create copies for scenario
variants.

## Change One Main Assumption At A Time

Good comparisons isolate the effect of a change.

For example, compare:

- baseline AMR fleet versus larger AMR fleet
- unrestricted routes versus clean/dirty route separation
- shared lifts versus restricted lift use
- current task demand versus increased task demand
- one workflow enabled versus all workflows enabled

If several things change at once, it becomes difficult to explain why the
outputs changed.

## Use Clear Scenario Names

Scenario and run names should describe the modelling assumption.

Useful names include:

- `Baseline_All_AMR`
- `Baseline_All_Porter`
- `CaseCarts_CleanDirtyRoutes`
- `Waste_Lift5Only`
- `Pharmacy_HigherDemand`
- `Hybrid_PorterPreference`

Avoid names such as `test1` or `new version`. They become hard to interpret
later.

## Record The Scenario Intent

For each scenario, record:

- what question it answers
- what changed from the baseline
- which workflows are included
- which AMR fleet assumptions are used
- which porter roster and delivery-resource assumptions are used
- which route and lift assumptions are used
- whether task generation is enabled
- any known limitations

The core configuration file does not currently include a dedicated scenario
intent field. For now, record this in a separate note, run log, or agreed
naming convention. A future launcher enhancement should prompt the user for an
optional scenario intent when a run is started, then store that text alongside
the run manifest.

## Compare Like With Like

When comparing scenarios, keep these consistent unless they are the thing being
tested:

- simulation start and end dates
- operating days
- task-generation rules
- department demand assumptions
- floor layouts
- route graph
- payload definitions
- reporting method

Changing these accidentally can make two runs look different for the wrong
reason.

## Workflow-Level Comparisons

It is often useful to test workflows separately before combining them.

For example:

1. Run pharmacy only.
2. Run waste only.
3. Run linen only.
4. Run stores only.
5. Run case carts only.
6. Run the combined scenario.

This helps identify which workflows create demand, failed tasks, lift pressure,
or congestion.

## AMR, Porter, And Hybrid Comparisons

The simulator supports comparison of:

- full AMR models
- full porter-led models
- hybrid models

Keep the operational demand consistent. The comparison should usually change
the logistics solution, not the hospital demand being served. Use staff-delivery
resources for porter transport; staff-assisted handling represents additional
endpoint support and is not a substitute.

Check that each scenario uses the same simulation dates, generated-flow rules,
payload definitions, return logic and route graph. Return tasks can make total
task counts differ when one scenario completes more initiating work, so compare
the common demand population as well as aggregate totals.

Treat current numerical differences as illustrative until material assumptions
have been locked down. In particular, validate porter response delay, rosters,
breaks, handling and turnaround, AMR charging and speeds, and the hybrid
selection policy.

## Lift And Bottleneck Comparisons

Lift demand is likely to be one of the most important comparison areas.

Useful scenario variants include:

- all flows sharing lifts
- selected flows restricted to particular lifts
- clean and dirty flows separated
- AMRs sharing lifts with porter-led flows
- AMRs having dedicated lift access

Check not only total lift use, but also waiting time, peak periods, and whether
route restrictions create unrealistic detours.

## Capacity And Receiving Constraints

Some scenarios should test whether receiving areas can tolerate the flow of
goods.

For example:

- Can theatres receive case carts without oversupply?
- Do waste collection points fill faster than they are cleared?
- Do departments receive required deliveries within acceptable windows?
- Do location inventory spaces become a constraint?

These questions may require config assumptions as well as output review.

## Step 1: Define The Question

Write the question before editing the model.

For example:

> Can the proposed AMR fleet deliver case carts overnight without creating lift
> pressure during morning operations?

This is better than starting with:

> What happens if I add more robots?

## Step 2: Choose The Baseline

Choose the baseline config and confirm it runs successfully.

Do not compare against a failed or poorly understood run.

## Step 3: Create A Scenario Variant

Copy the baseline and change only the intended assumption.

Examples:

- add two AMRs
- change a route profile
- disable one workflow
- adjust task-generation timing
- restrict a flow to selected lifts

## Step 4: Run And Review

Run the baseline and variant using the same output method.

Review:

- run status
- failed tasks
- pending or unassigned tasks
- AMR utilisation
- porter workload and roster effects
- hybrid allocation by delivery method
- lift use and waits
- generated task counts
- staff handling effects, where relevant
- location capacity issues

## Step 5: Record The Finding

Write down what changed and what it means.

For example:

> Restricting dirty waste movements to lifts 5 and 6 reduced clean-route overlap
> but increased lift waiting time during the afternoon peak.

Short notes like this are valuable when reviewing multiple scenarios later.

## Common Problems

**Too many assumptions change at once**

Create smaller variants. Make the comparison easier to explain.

**The baseline is not stable**

Fix the baseline before comparing variants.

**Runs have different simulation periods**

Check start and end dates before interpreting output differences.

**Generated demand differs unexpectedly**

Check enabled categories, department settings, active days, and generation
modes.

**The hybrid allocation does not match the stated preference**

A preference is not exclusive. Check availability, compatibility, roster
windows, charging and type restrictions. The fallback resource may be selected
when the preferred resource is infeasible.

## Modelling Notes

The purpose of scenario comparison is not to find a single perfect answer. It is
to understand which assumptions matter, where the system is sensitive, and which
operational options are worth deeper review.
