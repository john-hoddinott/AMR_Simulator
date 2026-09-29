# Hospital Logistics Simulator

***Project licensed under AGPL.***

This project models the movement of hospital logistics payloads by Automated
Mobile Robots (AMRs), porters, or a hybrid of both. It uses a configured route
graph, locations and lifts to simulate delivery tasks, resource availability,
payload compatibility, waiting, charging, staff rosters and shared
infrastructure use.

The model is intended for scenario comparison and decision support. Results are
only as reliable as the demand, routing, resource and operational assumptions in
the configuration.

## Main applications

- **Launcher** - manages scenario configs, runs, reports and visualisation.
- **Editor** - edits the hospital layout, routes, demand and delivery resources.
- **Simulator** - executes a scenario and writes detailed event outputs.
- **Visualiser** - replays AMR and porter movement over the hospital layout.
- **Report generator** - summarises the configured operating model and results.

The launcher is the recommended entry point:

```powershell
python amr_launcher.py
```

The individual tools can also be run directly. See the
[user guide](docs/user-guide/README.md) for the normal workflow.

## Delivery operating models

A scenario can represent:

- **AMR only** - delivery tasks are assigned to compatible robots.
- **Porter only** - delivery tasks are assigned to compatible rostered staff.
- **Hybrid** - individual flows can allow either resource, with an AMR, porter,
  or earliest-completion preference.

Preferences can vary by day and time. A preference is not an exclusive rule: if
the preferred resource is unavailable or incompatible, the dispatcher may use
the other permitted resource.

## Delivery resources

The editor's **Delivery Resources** screen contains both AMR and staff resource
types.

AMR assumptions include quantity, speed, payload capacity, dimensions, battery,
charging, start location and payload compatibility.

Staff-delivery assumptions include quantity, individual IDs, walking speed,
base location, payload capacity, capabilities, working days, shifts,
contractual breaks, turnaround and optional task-acceptance delay. Pickup and
drop-off use the scenario's configured building load/unload time.

Both resource types use the configured route graph and lifts. AMRs additionally
apply robot-specific battery, charging and physical compatibility rules.

## Task allocation

Manual tasks and generated logistics flows can specify a delivery-resource
policy:

- `amr`
- `staff`
- `either`

An `either` policy can use `earliest_completion`, `prefer_amr` or
`prefer_staff`, plus optional day/time preference windows. Policies can also
restrict eligible AMR types, staff types and required capabilities.

## Quick regression scenarios

Small, reviewable scenarios are provided under
[`examples/delivery_resources`](examples/delivery_resources/README.md):

1. existing AMR behaviour;
2. basic staff delivery;
3. staff shifts, breaks and response delay;
4. AMR-or-staff dispatch and preference fallback.

These are the best starting point for code review and model-maths validation.

## Direct command-line use

Create an example config:

```powershell
python simulator.py --write-example your_file_name.json
```

Run an existing config with detailed output:

```powershell
python simulator.py `
  --config your_file_name.json `
  --verbose `
  --verbose-csv simulation_steps.csv `
  --visualiser-csv visualiser_steps.csv `
  --failed-tasks-csv failed_tasks.csv
```

Generate a report:

```powershell
python report/amr_report_main.py simulation_steps.csv `
  --config-json your_file_name.json `
  --failed-tasks-csv failed_tasks.csv `
  -o simulation_report.pdf
```

## Validation status

The repository supports AMR, porter and hybrid delivery runs, but comparative
results should not be treated as validated operational predictions until the
material assumptions have been agreed. Particular attention should be given to
task demand, resource numbers, shifts and breaks, response delay, handling and
turnaround time, speeds, compatibility, charging, dispatch policy, graph scale,
lift behaviour and the treatment of pending or failed work.

See [Known Limitations And Modelling Assumptions](docs/user-guide/11-known-limitations-and-modelling-assumptions.md)
before relying on scenario comparisons.
