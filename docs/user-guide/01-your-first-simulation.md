# Your First Simulation

## Aim

This page guides you through running an existing scenario from the launcher,
generating a report, and opening the visualiser.

The aim is to become familiar with the basic workflow before editing a scenario.

## Before You Start

You need:

- the simulator launcher open
- at least one scenario config available to import
- the required floor layout files available on the computer

If you are using a launcher-managed config that has already been imported, you
can start from the **Configs** tab.

## Step 1: Import A Scenario Config

1. Open the **Configs** tab.
2. Select **Import Config**.
3. Choose the JSON scenario config you want to use.
4. Confirm that it appears in the config list.

The launcher copies the config into its own managed config folder. This protects
the original file and keeps launcher-managed work separate from files created
manually.

## Step 2: Review The Config Details

Select the imported config and review the **Config Details** panel.

Check that the broad counts look sensible:

- locations and departments
- floor layouts
- delivery-resource operating model
- AMR fleet and staff-delivery resource types
- payloads
- tasks
- route profiles
- graph nodes and edges

These figures do not prove that the config is correct, but they help identify
obvious mistakes. For example, a config with no floor layouts, no permitted
delivery resource, or no graph edges is unlikely to run as intended.

## Step 3: Run The Selected Config

1. Optionally enter a run name.
2. Choose the output detail level.
3. Select **Run Selected Config**.

The launcher creates a new run folder. New runs are grouped under the config
name and include the config name in the run folder name.

During the run, the **Activity Log** shows messages from the simulator. Long
configurations may take some time. 30 minutes or more is to be expected for a
whole hospital model across multiple days of operations.

## Step 4: Check The Run Status

After the process finishes, select the run in the **Runs** tab.

Review the **Run Details** panel:

- run name
- source config
- start time
- completion time
- status
- report availability

A report can only be generated for a completed run. Cancelled, failed, or
incomplete runs are not suitable for reporting.

## Step 5: Generate Or Open The Report

If the run completed successfully and no report exists yet, select **Generate
Report**.

Once the report has been generated, the same button changes to **Open Report**.

The report is useful for reviewing summary outputs and sharing results, but it
should be interpreted alongside the config assumptions and visualiser output.

## Step 6: Open The Visualiser

Select **Visualise** for the completed run.

The launcher opens the visualiser with the run config and simulation CSV already
selected. This allows you to inspect movement against the hospital layout.

The resource selector can follow either an AMR or an individual porter when the
run contains those movements.

## What To Check

For a first simulation, check:

- the run completed successfully
- the report opens without error
- the visualiser opens the expected layout
- the simulated routes look plausible
- the expected AMR, porter or hybrid resources appear
- failed tasks, if any, are understood

## Common Problems

**The run takes a long time**

Large scenarios can be slow. Start with a smaller or simpler config when testing
launcher behaviour.

**The report button says Report Unavailable**

The selected run did not complete successfully. Reports are only available for
completed runs.

**The visualiser cannot find a file**

The run may be incomplete, or the expected output CSV was not created.

**The layout does not appear correctly**

Check that the config refers to valid DXF floor layout files.

## Modelling Notes

A successful run only means that the simulator could execute the scenario. It
does not prove that the scenario reflects real operations. Before relying on
results, review the config assumptions, route graph, task definitions, resource
availability, dispatch policy and outputs. Numerical differences between AMR,
porter and hybrid runs should be treated as illustrative until their material
assumptions have been validated.
