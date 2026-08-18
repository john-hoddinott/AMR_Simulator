# Delivery Resources regression scenarios

These deliberately small scenarios provide quick manual checkpoints while the
Delivery Resources and porter-led transport functionality is developed.

Run all commands from the repository root:

```powershell
cd "D:\John\PycharmProjects\AMR_Simulator"
```

## 01 — Existing AMR behaviour

`01_amr_regression.json` is the pre-porter baseline. It contains one AMR, one
payload, three locations, a small one-floor graph and one explicit delivery.
The configured simulation window is 08:00–08:30 on 5 January 2026.

Open it directly in the editor:

```powershell
.\.venv\Scripts\python.exe visualiser\amr_editor_main.py --config examples\delivery_resources\01_amr_regression.json
```

Create a local output directory:

```powershell
New-Item -ItemType Directory -Force examples\delivery_resources\outputs\01_amr_regression
```

Run the simulator with detailed and visualiser outputs:

```powershell
.\.venv\Scripts\python.exe simulator.py `
  --config examples\delivery_resources\01_amr_regression.json `
  --verbose `
  --verbose-csv examples\delivery_resources\outputs\01_amr_regression\simulation_steps.csv `
  --visualiser-csv examples\delivery_resources\outputs\01_amr_regression\visualiser_steps.csv `
  --failed-tasks-csv examples\delivery_resources\outputs\01_amr_regression\failed_tasks.csv `
  --transport-matrix-csv examples\delivery_resources\outputs\01_amr_regression\transport_matrix.csv `
  --route-lengths-csv examples\delivery_resources\outputs\01_amr_regression\route_lengths.csv `
  --charger-estimate-csv examples\delivery_resources\outputs\01_amr_regression\charger_estimate.csv `
  --scenario-impact-csv examples\delivery_resources\outputs\01_amr_regression\scenario_impact.csv
```

Generate a report without a DXF background:

```powershell
.\.venv\Scripts\python.exe report\amr_report_main.py `
  examples\delivery_resources\outputs\01_amr_regression\simulation_steps.csv `
  --config-json examples\delivery_resources\01_amr_regression.json `
  --failed-tasks-csv examples\delivery_resources\outputs\01_amr_regression\failed_tasks.csv `
  --omit-drawings `
  -o examples\delivery_resources\outputs\01_amr_regression\simulation_report.pdf
```

Expected baseline:

- simulation completes without an exception;
- `AMR-REGRESSION-1` completes;
- no delivery task fails;
- the AMR travels from `AMR-BASE` to `PICKUP`, then to `DELIVERY`;
- a PDF report is generated;
- simulation runtime is well under one minute on the development computer.

The simulator may also record its internally generated idle return to the AMR
base. Regression comparisons should therefore check the named delivery task and
failed-task output rather than relying only on the aggregate completed-task
count.

## 02 — Delivery Resources editor checkpoint

Open `02_delivery_resources_editor.json` in the editor:

```powershell
.\.venv\Scripts\python.exe visualiser\amr_editor_main.py --config examples\delivery_resources\02_delivery_resources_editor.json
```

Use **Assets → Delivery Resources** to see one AMR type and one staff type in
the same list. The manual task is restricted to staff, while the disabled
Stores generated flow allows either AMR or staff.

Run the first porter journey with:

```powershell
New-Item -ItemType Directory -Force examples\delivery_resources\outputs\02_staff_delivery
.\.venv\Scripts\python.exe simulator.py `
  --config examples\delivery_resources\02_delivery_resources_editor.json `
  --verbose `
  --verbose-csv examples\delivery_resources\outputs\02_staff_delivery\simulation_steps.csv `
  --visualiser-csv examples\delivery_resources\outputs\02_staff_delivery\visualiser_steps.csv `
  --failed-tasks-csv examples\delivery_resources\outputs\02_staff_delivery\failed_tasks.csv
```

Expected result: `EDITOR-STAFF-EXAMPLE` is assigned to `PORTER-DAY-1`, walks
from `PORTER-BASE` to `PICKUP`, handles the payload, walks to `DELIVERY`, handles
the drop-off, and becomes available after its configured turnaround time.

## 03 — Staff availability

`03_staff_availability.json` is a fast roster regression with two individual
porters, a 07:00–07:15 shift, a 07:05–07:10 contractual break and 30 seconds of
turnaround. It checks pre-shift waiting, two simultaneous assignments, busy
waiting, break deferral and a task deferred to the next active day because it
cannot finish before shift end.

```powershell
New-Item -ItemType Directory -Force examples\delivery_resources\outputs\03_staff_availability
.\.venv\Scripts\python.exe simulator.py `
  --config examples\delivery_resources\03_staff_availability.json `
  --verbose `
  --verbose-csv examples\delivery_resources\outputs\03_staff_availability\simulation_steps.csv `
  --visualiser-csv examples\delivery_resources\outputs\03_staff_availability\visualiser_steps.csv `
  --failed-tasks-csv examples\delivery_resources\outputs\03_staff_availability\failed_tasks.csv
```
