from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd
import json

COLUMN_ALIASES = {
    "timestamp": [
        "timestamp",
        "time",
        "event_time",
        "datetime",
        "sim_datetime",
        "sim_time_iso",
        "current_datetime",
        "clock_time",
    ],
    "seconds": [
        "sim_time_s",
        "sim_time_seconds",
        "current_time",
        "time_s",
        "elapsed_seconds",
        "seconds",
    ],
    "amr": [
        "amr",
        "amr_id",
    ],
    "task": ["task", "task_id", "job", "job_id", "consignment_id"],
    "event": ["event", "event_type", "status", "state", "action", "phase"],
    "segment_type": ["segment_type", "segment", "segment_name", "movement_type"],
    "duration": [
        "duration_sec",
        "duration_seconds",
        "segment_duration_s",
        "segment_seconds",
        "elapsed_s",
        "task_duration_sec",
    ],
    "wait": ["wait_time_sec", "wait_seconds", "waiting_s", "queue_s"],
    "lift": ["lift", "lift_id", "elevator", "elevator_id"],
    "from": [
        "from_location",
        "from",
        "from_node",
        "start_node",
        "origin",
        "pickup",
        "source",
    ],
    "to": [
        "to_location",
        "to",
        "to_node",
        "end_node",
        "destination",
        "dropoff",
        "target",
    ],
    "start_floor": ["start_floor", "from_floor", "floor_from"],
    "end_floor": ["end_floor", "to_floor", "floor_to"],
    "outcome": ["outcome", "result", "task_result"],
    "payload": ["payload", "payload_type", "payload_name", "load_type", "load"],
    "distance": ["distance_m", "segment_distance_m", "distance", "travel_distance_m"],
    "energy": [
        "energy_kwh",
        "segment_energy_kwh",
        "energy",
        "consumption_kwh",
        "recharge_energy_kwh",
    ],
    "reason": [
        "failure_reason",
        "reason",
        "failed_reason",
        "fail_reason",
        "pending_reason",
        "message",
        "detail",
        "details",
        "note",
        "notes",
    ],
    "start_x": ["start_x", "from_x", "x_from", "origin_x", "x1"],
    "start_y": ["start_y", "from_y", "y_from", "origin_y", "y1"],
    "end_x": ["end_x", "to_x", "x_to", "destination_x", "x2"],
    "end_y": ["end_y", "to_y", "y_to", "destination_y", "y2"],
}

WAIT_PATTERNS = re.compile(r"wait|queue|queued|blocked|hold|reserve|reservation", re.I)
COMPLETE_PATTERNS = re.compile(
    r"complete|completed|done|success|delivered|released", re.I
)
FAIL_PATTERNS = re.compile(
    r"fail|failed|abort|aborted|cancel|cancelled|stuck|timeout", re.I
)
ASSIGN_PATTERNS = re.compile(r"assign|allocated|accepted|dispatch", re.I)
LIFT_PATTERNS = re.compile(r"lift|elevator", re.I)


@dataclass
class Context:
    cols: Dict[str, Optional[str]]
    has_datetime: bool
    time_col: str


def normalise_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [
        re.sub(r"[^a-z0-9]+", "_", str(c).strip().lower()).strip("_")
        for c in df.columns
    ]
    return df


def pick_col(df: pd.DataFrame, key: str) -> Optional[str]:
    for alias in COLUMN_ALIASES[key]:
        if alias in df.columns:
            return alias
    return None


def parse_time_column(df: pd.DataFrame) -> Tuple[pd.DataFrame, Context]:
    df = normalise_columns(df)
    cols = {k: pick_col(df, k) for k in COLUMN_ALIASES}
    if cols["timestamp"]:
        df["_event_time"] = pd.to_datetime(df[cols["timestamp"]], errors="coerce")
        if df["_event_time"].notna().any():
            return df, Context(cols=cols, has_datetime=True, time_col="_event_time")
    if cols["seconds"]:
        df["_event_time"] = pd.to_numeric(df[cols["seconds"]], errors="coerce")
        return df, Context(cols=cols, has_datetime=False, time_col="_event_time")
    raise ValueError("No usable time column found.")


def fmt_ts(value, has_datetime: bool) -> str:
    if pd.isna(value):
        return "-"
    return (
        # pd.Timestamp(value).strftime("%Y-%m-%d %H:%M:%S")
        pd.Timestamp(value).strftime("%d/%m/%Y %H:%M:%S")
        if has_datetime
        else f"{float(value):,.1f}s"
    )


def fmt_duration(seconds: Optional[float]) -> str:
    if seconds is None or pd.isna(seconds):
        return "-"
    seconds = abs(float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def safe_text(value) -> str:
    if value is None or pd.isna(value):
        return "-"
    text = str(value)
    return text if text.strip() else "-"


NON_PHYSICAL_AMR_IDS = frozenset(
    {
        "",
        "-",
        "none",
        "null",
        "nan",
        "n/a",
        "na",
        "unassigned",
        "not assigned",
        "unknown",
        "system",
    }
)


def is_physical_amr_id(value) -> bool:
    """Return True only for a real runtime AMR identifier.

    Generated, pending and staff-handling rows can legitimately have no AMR.
    Task-level reporting represents those rows with ``-``; that placeholder must
    never be counted as an observed fleet member.
    """
    if value is None:
        return False
    try:
        if pd.isna(value):
            return False
    except Exception:
        pass
    return str(value).strip().casefold() not in NON_PHYSICAL_AMR_IDS


def configured_amr_quantity(amr_parameters: Optional[pd.DataFrame]) -> Optional[int]:
    """Return the physical fleet quantity from the JSON AMR parameter table."""
    if amr_parameters is None or amr_parameters.empty:
        return None
    if "quantity" not in amr_parameters.columns:
        return None
    values = pd.to_numeric(amr_parameters["quantity"], errors="coerce").fillna(0)
    return int(values.clip(lower=0).sum())


def natural_key(value):
    return tuple(
        int(text) if text.isdigit() else text.lower()
        for text in re.split(r"(\d+)", str(value))
    )




def split_payload_names(value) -> List[str]:
    """Return real payload names from a simulator payload cell.

    Multi-stop pickup rows can contain comma-separated payload names, while
    empty/return/charging rows may contain blanks or explicit empty payload
    labels.  Reporting should count physical payloads, not the literal CSV
    cell text.
    """
    if value is None:
        return []
    try:
        if pd.isna(value):
            return []
    except Exception:
        pass

    text = str(value).strip()
    if not text:
        return []

    # Some upstream rows may carry JSON arrays/dicts; accept the common forms
    # without requiring the caller to know which schema version produced them.
    if text.startswith("["):
        try:
            decoded = json.loads(text)
            names: List[str] = []
            if isinstance(decoded, list):
                for item in decoded:
                    if isinstance(item, dict):
                        names.extend(split_payload_names(item.get("payload", "")))
                    else:
                        names.extend(split_payload_names(item))
            return names
        except Exception:
            pass

    empty_tokens = {
        "",
        "-",
        "none",
        "nan",
        "null",
        "empty",
        "no payload",
        "no_payload",
        "empty_payload",
        "__empty__",
        "__empty_payload__",
    }
    names: List[str] = []
    for part in text.split(","):
        name = part.strip()
        normalised = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
        if not name or name.lower() in empty_tokens or normalised in empty_tokens:
            continue
        names.append(name)
    return names


def primary_payload_name(value) -> str:
    names = split_payload_names(value)
    return names[0] if names else "-"


def _clean_payload_instance_id(value) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    text = str(value).strip()
    return "" if text.lower() in {"", "-", "nan", "none", "null"} else text


def _payload_instance_column(df: pd.DataFrame) -> Optional[str]:
    for col in (
        "payload_instance_id",
        "payload_instance",
        "payload_id",
        "load_instance_id",
        "container_instance_id",
    ):
        if col in df.columns:
            return col
    return None


def _completed_transport_movement_rows(df: pd.DataFrame) -> pd.DataFrame:
    """Return one row per physical payload arrival where available.

    The simulator writes event-level ``location_payload_enter`` rows when a
    payload physically enters a location.  These rows carry ``payload_instance_id``
    and are the preferred source for unique transported-item counts.  Segment or
    task rows can repeat the same payload many times and must not be used for
    this metric.
    """
    if df is None or df.empty or "_event_text" not in df.columns:
        return pd.DataFrame()
    event_text = df["_event_text"].astype(str)
    rows = df[event_text.str.fullmatch("location_payload_enter", case=False, na=False)].copy()
    if rows.empty:
        return rows
    if "payload" in rows.columns:
        rows["_report_payload"] = rows["payload"].map(primary_payload_name)
        rows = rows[rows["_report_payload"] != "-"]
    return rows


def build_payload_population_summary(df: pd.DataFrame) -> pd.DataFrame:
    """Return simulator-owned runtime payload population by payload type.

    ``payload_population_summary`` rows are emitted by the simulator from the
    payload instance registry. ``payload_runtime_population`` is the peak
    simultaneous number of physical instances contained in the runtime, which is
    the asset count used for sizing. It is deliberately separate from task count
    and movement count.
    """
    columns = [
        "payload",
        "total_runtime_payloads",
        "known_payload_instances",
        "payload_weight_kg",
    ]
    if df is None or df.empty or "_event_text" not in df.columns:
        return pd.DataFrame(columns=columns)

    rows = df[df["_event_text"].astype(str).str.fullmatch("payload_population_summary", case=False, na=False)].copy()
    if rows.empty:
        return pd.DataFrame(columns=columns)

    if "payload" not in rows.columns:
        return pd.DataFrame(columns=columns)
    rows["_report_payload"] = rows["payload"].map(primary_payload_name)
    rows = rows[rows["_report_payload"] != ""].copy()
    if rows.empty:
        return pd.DataFrame(columns=columns)

    def _num_col(frame: pd.DataFrame, name: str, default: float = 0.0) -> pd.Series:
        if name not in frame.columns:
            return pd.Series(default, index=frame.index, dtype=float)
        return pd.to_numeric(frame[name], errors="coerce").fillna(default)

    rows["_runtime_population"] = _num_col(rows, "payload_runtime_population")
    rows["_known_instances"] = _num_col(rows, "payload_known_instances")
    rows["_weight"] = _num_col(rows, "payload_weight_kg")

    out = (
        rows.groupby("_report_payload", dropna=False)
        .agg(
            total_runtime_payloads=("_runtime_population", "max"),
            known_payload_instances=("_known_instances", "max"),
            payload_weight_kg=("_weight", "max"),
        )
        .reset_index()
        .rename(columns={"_report_payload": "payload"})
        .sort_values("payload")
    )
    out["total_runtime_payloads"] = out["total_runtime_payloads"].round().astype(int)
    out["known_payload_instances"] = out["known_payload_instances"].round().astype(int)
    out["payload_weight_kg"] = out["payload_weight_kg"].round(1)
    return out[columns].reset_index(drop=True)


def build_payload_schedule(
    tasks: pd.DataFrame,
    payload_weights: Dict[str, float],
    movement_df: Optional[pd.DataFrame] = None,
    population_df: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """Build the payload schedule from unique physical payload instances.

    Prefer simulator movement rows because they identify the physical item using
    ``payload_instance_id``.  This avoids reporting task count as transported
    item count.  If older CSVs do not contain movement rows, fall back to the
    previous completed-task count so legacy reports still build.
    """
    columns = [
        "payload",
        "total_runtime_payloads",
        "unique_payloads_moved",
        "tasks",
        "known_payload_instances",
        "payload_weight_kg",
    ]

    rows: List[dict] = []
    if movement_df is not None and not movement_df.empty:
        instance_col = _payload_instance_column(movement_df)
        for idx, row in movement_df.iterrows():
            payload_names = split_payload_names(row.get("payload"))
            if not payload_names and row.get("_report_payload") not in (None, "-"):
                payload_names = split_payload_names(row.get("_report_payload"))
            for payload_name in payload_names:
                instance_id = _clean_payload_instance_id(row.get(instance_col)) if instance_col else ""
                if not instance_id:
                    task_id = safe_text(row.get("task_id"))
                    instance_id = f"legacy:{task_id}:{payload_name}:{idx}"
                rows.append(
                    {
                        "payload_instance_id": instance_id,
                        "payload": payload_name,
                        "task_id": safe_text(row.get("task_id")),
                        "payload_weight_kg": float(payload_weights.get(str(payload_name), 0.0)),
                    }
                )

    if rows:
        payload_events = pd.DataFrame(rows).drop_duplicates(["payload_instance_id", "payload"])
        payload_schedule = (
            payload_events.groupby("payload", dropna=False)
            .agg(
                unique_payloads_moved=("payload_instance_id", "nunique"),
                tasks=("task_id", lambda s: s.replace("-", pd.NA).dropna().nunique()),
                payload_weight_kg=("payload_weight_kg", "first"),
            )
            .reset_index()
            .sort_values(["payload"])
        )
        payload_schedule["payload_weight_kg"] = (
            pd.to_numeric(payload_schedule["payload_weight_kg"], errors="coerce")
            .fillna(0.0)
            .round(1)
        )
        if population_df is not None and not population_df.empty:
            payload_schedule = payload_schedule.merge(
                population_df[["payload", "total_runtime_payloads", "known_payload_instances"]],
                on="payload",
                how="outer",
            )
        payload_schedule["total_runtime_payloads"] = pd.to_numeric(
            payload_schedule.get("total_runtime_payloads", payload_schedule.get("unique_payloads_moved")),
            errors="coerce",
        ).fillna(pd.to_numeric(payload_schedule.get("unique_payloads_moved"), errors="coerce").fillna(0)).round().astype(int)
        payload_schedule["known_payload_instances"] = pd.to_numeric(
            payload_schedule.get("known_payload_instances", payload_schedule.get("total_runtime_payloads")),
            errors="coerce",
        ).fillna(payload_schedule["total_runtime_payloads"]).round().astype(int)
        payload_schedule["unique_payloads_moved"] = pd.to_numeric(
            payload_schedule.get("unique_payloads_moved"), errors="coerce"
        ).fillna(0).round().astype(int)
        payload_schedule["tasks"] = pd.to_numeric(payload_schedule.get("tasks"), errors="coerce").fillna(0).round().astype(int)
        payload_schedule["payload_weight_kg"] = pd.to_numeric(payload_schedule.get("payload_weight_kg"), errors="coerce").fillna(0.0).round(1)
        return payload_schedule[columns].sort_values("payload").reset_index(drop=True)

    if tasks is None or tasks.empty:
        if population_df is not None and not population_df.empty:
            out = population_df.copy()
            out["unique_payloads_moved"] = 0
            out["tasks"] = 0
            return out[columns].sort_values("payload").reset_index(drop=True)
        return pd.DataFrame(columns=columns)

    source = tasks.copy()
    if "outcome" in source.columns and (source["outcome"] == "completed").any():
        source = source[source["outcome"] == "completed"].copy()

    fallback_rows: List[dict] = []
    for _, row in source.iterrows():
        task_id = safe_text(row.get("task_id"))
        for payload_name in split_payload_names(row.get("payload")):
            fallback_rows.append(
                {
                    "task_id": task_id,
                    "payload": payload_name,
                    "payload_weight_kg": float(payload_weights.get(str(payload_name), 0.0)),
                }
            )

    if not fallback_rows:
        if population_df is not None and not population_df.empty:
            out = population_df.copy()
            out["unique_payloads_moved"] = 0
            out["tasks"] = 0
            return out[columns].sort_values("payload").reset_index(drop=True)
        return pd.DataFrame(columns=columns)

    payload_events = pd.DataFrame(fallback_rows).drop_duplicates(["task_id", "payload"])
    payload_schedule = (
        payload_events.groupby("payload", dropna=False)
        .agg(
            unique_payloads_moved=("task_id", "count"),
            tasks=("task_id", "count"),
            payload_weight_kg=("payload_weight_kg", "first"),
        )
        .reset_index()
        .sort_values(["payload"])
    )
    payload_schedule["payload_weight_kg"] = (
        pd.to_numeric(payload_schedule["payload_weight_kg"], errors="coerce")
        .fillna(0.0)
        .round(1)
    )
    if population_df is not None and not population_df.empty:
        payload_schedule = payload_schedule.merge(
            population_df[["payload", "total_runtime_payloads", "known_payload_instances"]],
            on="payload",
            how="outer",
        )
    payload_schedule["total_runtime_payloads"] = pd.to_numeric(
        payload_schedule.get("total_runtime_payloads", payload_schedule.get("unique_payloads_moved")),
        errors="coerce",
    ).fillna(pd.to_numeric(payload_schedule.get("unique_payloads_moved"), errors="coerce").fillna(0)).round().astype(int)
    payload_schedule["known_payload_instances"] = pd.to_numeric(
        payload_schedule.get("known_payload_instances", payload_schedule.get("total_runtime_payloads")),
        errors="coerce",
    ).fillna(payload_schedule["total_runtime_payloads"]).round().astype(int)
    payload_schedule["unique_payloads_moved"] = pd.to_numeric(payload_schedule.get("unique_payloads_moved"), errors="coerce").fillna(0).round().astype(int)
    payload_schedule["tasks"] = pd.to_numeric(payload_schedule.get("tasks"), errors="coerce").fillna(0).round().astype(int)
    payload_schedule["payload_weight_kg"] = pd.to_numeric(payload_schedule.get("payload_weight_kg"), errors="coerce").fillna(0.0).round(1)
    return payload_schedule[columns].sort_values("payload").reset_index(drop=True)



def split_task_ids(value) -> List[str]:
    """Return task IDs from CSV cells that may contain JSON or comma lists."""
    if value is None or pd.isna(value):
        return []
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null", "-"}:
        return []
    if text.startswith("["):
        try:
            decoded = json.loads(text)
            if isinstance(decoded, list):
                return [str(x).strip() for x in decoded if str(x).strip()]
        except Exception:
            pass
    return [x.strip() for x in text.split(",") if x.strip()]


def _append_unique_stop(stops: List[str], item: str) -> None:
    item = str(item or "").strip()
    if not item or item == "-":
        return
    if not stops or stops[-1] != item:
        stops.append(item)


def build_multi_stop_task_paths(
    df: pd.DataFrame,
    ctx: Context,
    amr_col: str,
    task_col: str,
    from_col: Optional[str],
    to_col: Optional[str],
) -> Dict[str, str]:
    """Build a readable stop path for every task inside a multi-stop batch.

    Multi-stop segment rows use grouped task IDs, so the normal per-task rows
    cannot show the actual route sequence.  This function reconstructs the stop
    sequence from the grouped pickup/dropoff rows and maps it back to each
    component task ID.
    """
    if df is None or df.empty:
        return {}

    multi_stop_col = "multi_stop_task_ids" if "multi_stop_task_ids" in df.columns else None
    route_rows = df[df["_event_text"].astype(str).str.contains("multi_stop_segment", case=False, na=False)].copy()
    if route_rows.empty:
        grouped_task_mask = df[task_col].astype(str).str.contains(",", na=False)
        route_rows = df[grouped_task_mask].copy()
    if route_rows.empty:
        return {}

    route_rows = route_rows.sort_values([amr_col, ctx.time_col]).copy()
    grouped: Dict[Tuple[str, Tuple[str, ...]], List[pd.Series]] = {}
    for _, row in route_rows.iterrows():
        task_ids = split_task_ids(row.get(multi_stop_col)) if multi_stop_col else []
        if not task_ids:
            task_ids = split_task_ids(row.get(task_col))
        if len(task_ids) < 2:
            continue
        key = (safe_text(row.get(amr_col)), tuple(task_ids))
        grouped.setdefault(key, []).append(row)

    paths: Dict[str, str] = {}
    for (_amr, task_ids_tuple), rows in grouped.items():
        rows = sorted(rows, key=lambda r: r.get(ctx.time_col))
        stops: List[str] = []
        for row in rows:
            seg = str(row.get("_segment_text", "") or "").strip().lower()
            event = str(row.get("_event_text", "") or "").strip().lower()
            from_value = safe_text(row.get(from_col)) if from_col else "-"
            to_value = safe_text(row.get(to_col)) if to_col else "-"

            # Only show operational stops, not every graph/corridor node.
            # Pickup/dropoff rows normally have the location in from/to.
            if "pickup" in seg or "pickup" in event:
                loc = to_value if to_value != "-" else from_value
                _append_unique_stop(stops, f"Pickup: {loc}")
            elif "dropoff" in seg or "dropoff" in event:
                loc = to_value if to_value != "-" else from_value
                _append_unique_stop(stops, f"Drop-off: {loc}")
            elif "wait_for_location" in seg or "wait_for_location" in event:
                loc = to_value if to_value != "-" else from_value
                _append_unique_stop(stops, f"Wait: {loc}")

        if not stops:
            # Fallback: use unique from/to locations in time order.
            for row in rows:
                if from_col:
                    _append_unique_stop(stops, safe_text(row.get(from_col)))
                if to_col:
                    _append_unique_stop(stops, safe_text(row.get(to_col)))

        if not stops:
            continue
        path = " → ".join(stops)
        for task_id in task_ids_tuple:
            paths[str(task_id)] = path
    return paths


def _multi_stop_task_ids_from_row(
    row,
    task_col: str,
    multi_stop_col: Optional[str] = None,
) -> List[str]:
    """Extract all task IDs that define a multi-stop batch for a row."""
    ids: List[str] = []
    if multi_stop_col and multi_stop_col in row.index:
        ids = split_task_ids(row.get(multi_stop_col))
    if ids:
        return ids

    details = str(row.get("details", "") or "").strip()
    if details.startswith("{"):
        try:
            decoded = json.loads(details)
            for key in ("multi_stop_task_ids", "task_ids"):
                values = decoded.get(key)
                if isinstance(values, list):
                    ids = [str(x).strip() for x in values if str(x).strip()]
                    if ids:
                        return ids
        except Exception:
            pass

    return split_task_ids(row.get(task_col))


def build_multi_stop_task_leg_overrides(
    df: pd.DataFrame,
    ctx: Context,
    amr_col: str,
    task_col: str,
    from_col: Optional[str],
    to_col: Optional[str],
) -> Dict[str, dict]:
    """Map each task in a multi-stop route to its stop-to-stop leg.

    Task detail should describe the leg that delivered each payload, not the
    whole batch route.  For a pharmacy batch this produces rows such as:

        D7-PHARMACY -> D1-PHARMACY
        D1-PHARMACY -> D2-PHARMACY
        D2-PHARMACY -> D3-PHARMACY

    The returned override also carries leg start/finish/duration so the PDF
    does not repeat the full multi-stop batch duration on every payload row.
    """
    if df is None or df.empty or not from_col or not to_col:
        return {}

    multi_stop_col = "multi_stop_task_ids" if "multi_stop_task_ids" in df.columns else None
    route_rows = df[
        df["_event_text"].astype(str).str.contains(
            r"multi_stop_segment", case=False, na=False
        )
    ].copy()
    if route_rows.empty:
        grouped_task_mask = df[task_col].astype(str).str.contains(",", na=False)
        route_rows = df[grouped_task_mask].copy()
    if route_rows.empty:
        return {}

    grouped: Dict[Tuple[str, Tuple[str, ...]], List[pd.Series]] = {}
    for _, row in route_rows.sort_values([amr_col, ctx.time_col]).iterrows():
        batch_ids = _multi_stop_task_ids_from_row(row, task_col, multi_stop_col)
        if len(batch_ids) < 2:
            continue
        key = (safe_text(row.get(amr_col)), tuple(batch_ids))
        grouped.setdefault(key, []).append(row)

    overrides: Dict[str, dict] = {}

    for (_amr, _batch_ids), rows in grouped.items():
        rows = sorted(rows, key=lambda r: r.get(ctx.time_col))
        current_stop: Optional[str] = None
        current_depart_time = None
        current_wait_s = 0.0

        for row in rows:
            seg = str(row.get("_segment_text", "") or "").strip().lower()
            event = str(row.get("_event_text", "") or "").strip().lower()
            from_value = safe_text(row.get(from_col)) if from_col else "-"
            to_value = safe_text(row.get(to_col)) if to_col else "-"
            stop = to_value if to_value != "-" else from_value
            row_start = row.get(ctx.time_col)
            row_duration = float(pd.to_numeric(row.get("_duration_s", 0.0), errors="coerce") or 0.0)
            row_finish = add_seconds_to_event_time(row_start, row_duration, ctx.has_datetime)
            row_wait = float(pd.to_numeric(row.get("_wait_s", 0.0), errors="coerce") or 0.0)

            if "pickup" in seg or "pickup" in event:
                if stop != "-":
                    current_stop = stop
                # The first delivery leg starts once loading has finished.
                current_depart_time = row_finish
                current_wait_s = 0.0
                continue

            if current_stop is not None and ("wait" in seg or "wait" in event):
                # Count waits between stops against the leg that follows.
                current_wait_s += max(0.0, row_wait or row_duration)
                continue

            if "dropoff" not in seg and "dropoff" not in event:
                continue
            if stop == "-":
                continue

            drop_task_ids = split_task_ids(row.get(task_col))
            if not drop_task_ids:
                # Only use the whole batch as a last resort.  Current simulator
                # drop-off rows should identify the specific delivered task.
                drop_task_ids = _multi_stop_task_ids_from_row(row, task_col, multi_stop_col)

            leg_start = current_depart_time if current_depart_time is not None else row_start
            leg_finish = row_finish
            leg_duration = _duration_between_event_times(leg_start, leg_finish, ctx.has_datetime)
            if leg_duration is None or leg_duration < 0:
                leg_duration = row_duration

            start_stop = current_stop or (from_value if from_value != "-" else None)
            if start_stop:
                for task_id in drop_task_ids:
                    overrides[str(task_id)] = {
                        "origin": start_stop,
                        "destination": stop,
                        "start": leg_start,
                        "finish": leg_finish,
                        "duration_s": float(leg_duration),
                        "wait_s": float(current_wait_s),
                    }

            # The next delivery leg starts after this drop-off is complete.
            current_stop = stop
            current_depart_time = row_finish
            current_wait_s = 0.0

    return overrides


def build_multi_stop_task_endpoint_overrides(
    df: pd.DataFrame,
    ctx: Context,
    amr_col: str,
    task_col: str,
    from_col: Optional[str],
    to_col: Optional[str],
) -> Dict[str, Tuple[str, str]]:
    # Backwards-compatible wrapper for older callers.
    return {
        task_id: (data.get("origin", "-"), data.get("destination", "-"))
        for task_id, data in build_multi_stop_task_leg_overrides(
            df, ctx, amr_col, task_col, from_col, to_col
        ).items()
    }

def time_delta_seconds(start, end, has_datetime: bool) -> Optional[float]:
    if pd.isna(start) or pd.isna(end):
        return None
    return (
        (pd.Timestamp(end) - pd.Timestamp(start)).total_seconds()
        if has_datetime
        else float(end) - float(start)
    )


def event_time_to_float(value, has_datetime: bool) -> Optional[float]:
    if pd.isna(value):
        return None
    return pd.Timestamp(value).timestamp() if has_datetime else float(value)


def add_seconds_to_event_time(value, seconds: float, has_datetime: bool):
    if pd.isna(value):
        return value
    if has_datetime:
        return pd.Timestamp(value) + pd.to_timedelta(float(seconds or 0.0), unit="s")
    return float(value) + float(seconds or 0.0)


def _duration_between_event_times(start, finish, has_datetime: bool) -> Optional[float]:
    return time_delta_seconds(start, finish, has_datetime)


def percentile_95_concurrency(intervals: Iterable[Tuple[float, float]]) -> int:
    events: List[Tuple[float, int]] = []
    for start, end in intervals:
        if (
            start is None
            or end is None
            or pd.isna(start)
            or pd.isna(end)
            or end < start
        ):
            continue
        events.append((float(start), 1))
        events.append((float(end), -1))
    if not events:
        return 0
    events.sort(key=lambda x: (x[0], -x[1]))
    current = 0
    values: List[int] = []
    for _, delta in events:
        current += delta
        values.append(current)
    return int(math.ceil(pd.Series(values).quantile(0.95))) if values else 0


def peak_time_weighted_concurrency(
    intervals: Iterable[Tuple[float, float]], window_sec: float = 300.0
) -> int:
    clean = [
        (float(start), float(end))
        for start, end in intervals
        if start is not None
        and end is not None
        and not pd.isna(start)
        and not pd.isna(end)
        and float(end) > float(start)
    ]
    if not clean:
        return 0

    horizon_start = min(start for start, _ in clean)
    horizon_end = max(end for _, end in clean)
    effective_window = max(1e-9, min(float(window_sec), horizon_end - horizon_start))

    # The maximum integral over a fixed-width interval occurs when either the
    # window start or window end is aligned to an interval boundary.
    candidates = {horizon_start, max(horizon_start, horizon_end - effective_window)}
    for start, end in clean:
        candidates.add(min(max(start, horizon_start), horizon_end - effective_window))
        candidates.add(min(max(end - effective_window, horizon_start), horizon_end - effective_window))

    peak_weighted = 0.0
    for window_start in candidates:
        window_end = window_start + effective_window
        overlap_s = sum(
            max(0.0, min(end, window_end) - max(start, window_start))
            for start, end in clean
        )
        peak_weighted = max(peak_weighted, overlap_s / effective_window)

    return int(math.ceil(peak_weighted - 1e-9))


def merge_intervals(intervals: Iterable[Tuple[float, float]], gap_tolerance: float = 1.0) -> List[Tuple[float, float]]:
    clean = sorted(
        (float(start), float(end))
        for start, end in intervals
        if start is not None
        and end is not None
        and not pd.isna(start)
        and not pd.isna(end)
        and float(end) >= float(start)
    )
    if not clean:
        return []

    merged: List[Tuple[float, float]] = []
    cur_start, cur_end = clean[0]
    for start, end in clean[1:]:
        if start <= cur_end + gap_tolerance:
            cur_end = max(cur_end, end)
        else:
            merged.append((cur_start, cur_end))
            cur_start, cur_end = start, end
    merged.append((cur_start, cur_end))
    return merged


def build_amr_busy_intervals(
    df: pd.DataFrame,
    ctx: Context,
    amr_col: str,
) -> Tuple[Dict[str, List[Tuple[float, float]]], pd.DataFrame]:
    """Return actual AMR busy intervals from segment rows.

    Multi-stop logs repeat one route across several component task IDs.  Counting
    task duration therefore overstates AMR demand.  Segment rows are the correct
    source: each AMR is busy once on each corridor, lift, wait, pickup/dropoff or
    charge segment, regardless of how many payload slots are occupied.
    """
    if df is None or df.empty or amr_col not in df.columns:
        return {}, pd.DataFrame(columns=["amr", "route_start", "route_finish", "route_time_s"])

    event_text = df.get("_event_text", pd.Series("", index=df.index)).astype(str)
    seg_text = df.get("_segment_text", pd.Series("", index=df.index)).astype(str)
    duration = pd.to_numeric(df.get("_duration_s", pd.Series(0, index=df.index)), errors="coerce").fillna(0.0)

    segment_mask = (
        event_text.str.contains(r"segment|multi_stop_segment|charge", case=False, na=False)
        | seg_text.str.contains(r"corridor|lift|pickup|dropoff|wait|charge|reposition", case=False, na=False)
    )
    rows = df[segment_mask & df[amr_col].notna() & (duration > 0)].copy()
    if rows.empty:
        return {}, pd.DataFrame(columns=["amr", "route_start", "route_finish", "route_time_s"])

    per_amr: Dict[str, List[Tuple[float, float]]] = {}
    route_rows: List[dict] = []
    for amr, sub in rows.groupby(amr_col, dropna=False):
        intervals: List[Tuple[float, float]] = []
        for idx, row in sub.iterrows():
            start = event_time_to_float(row.get(ctx.time_col), ctx.has_datetime)
            if start is None:
                continue
            end = start + float(duration.loc[idx])
            intervals.append((start, end))
        merged = merge_intervals(intervals, gap_tolerance=1.0)
        amr_name = safe_text(amr)
        per_amr[amr_name] = merged
        for start, end in merged:
            route_rows.append(
                {
                    "amr": amr_name,
                    "route_start": start,
                    "route_finish": end,
                    "route_time_s": end - start,
                }
            )

    return per_amr, pd.DataFrame(route_rows)


def build_lift_busy_intervals(
    lift_rows: pd.DataFrame,
    ctx: Context,
) -> Tuple[Dict[str, List[Tuple[float, float]]], pd.DataFrame]:
    """Return lift busy intervals using the same merge model as AMRs."""
    columns = ["lift_id", "busy_start", "busy_finish", "busy_time_s"]
    if (
        lift_rows is None
        or lift_rows.empty
        or "_lift_id" not in lift_rows.columns
        or "lift_time_s" not in lift_rows.columns
    ):
        return {}, pd.DataFrame(columns=columns)

    rows = lift_rows[
        lift_rows["_lift_id"].notna()
        & pd.to_numeric(lift_rows["lift_time_s"], errors="coerce").fillna(0).gt(0)
    ].copy()
    if rows.empty:
        return {}, pd.DataFrame(columns=columns)

    per_lift: Dict[str, List[Tuple[float, float]]] = {}
    busy_rows: List[dict] = []
    for lift_id, sub in rows.groupby("_lift_id", dropna=False):
        intervals: List[Tuple[float, float]] = []
        for _, row in sub.iterrows():
            start = event_time_to_float(row.get(ctx.time_col), ctx.has_datetime)
            duration = _to_float(row.get("lift_time_s"), 0.0)
            if start is None or duration <= 0:
                continue
            intervals.append((start, start + duration))

        merged = merge_intervals(intervals, gap_tolerance=1.0)
        lift_name = safe_text(lift_id)
        per_lift[lift_name] = merged
        for start, end in merged:
            busy_rows.append(
                {
                    "lift_id": lift_name,
                    "busy_start": start,
                    "busy_finish": end,
                    "busy_time_s": end - start,
                }
            )

    return per_lift, pd.DataFrame(busy_rows, columns=columns)


def interval_total(intervals: Iterable[Tuple[float, float]]) -> float:
    return float(sum(max(0.0, float(end) - float(start)) for start, end in intervals))


def choose_task_endpoint(
    g: pd.DataFrame,
    _ctx: Context,
    from_col: Optional[str],
    to_col: Optional[str],
    which: str,
):
    """Prefer generated/assigned task endpoints over completion/segment state.

    Multi-stop completion rows can describe the route final AMR location.  For
    the task detail table, From/To should describe the delivery request itself.
    """
    if g is None or g.empty:
        return None
    col = from_col if which == "from" else to_col
    if not col or col not in g.columns:
        return None

    preferred = g[
        g.get("_event_text", pd.Series("", index=g.index))
        .astype(str)
        .str.contains(r"task_generated|task_assigned|multi_stop_task_assigned|waste_task_generated|return_task_generated", case=False, na=False)
    ]
    for source in (preferred, g):
        values = [str(v).strip() for v in source[col].dropna().tolist() if str(v).strip() and str(v).strip() != "-"]
        if values:
            return values[0] if which == "from" else values[-1]
    return None



def build_lift_usage_profile(
    lift_rows: pd.DataFrame,
    ctx: Context,
    interval_minutes: int = 30,
) -> pd.DataFrame:
    """Return lift trip counts by time-of-day interval for each lift.

    The profile is aggregated by clock time across the simulated period. This
    keeps multi-day runs readable by showing the typical daily lift demand
    pattern rather than one very long date-time axis.
    """
    columns = ["interval", "interval_start_min", "lift_id", "trips"]
    if (
        lift_rows is None
        or lift_rows.empty
        or "_lift_id" not in lift_rows.columns
        or ctx.time_col not in lift_rows.columns
    ):
        return pd.DataFrame(columns=columns)

    interval_minutes = max(1, int(interval_minutes or 30))
    bucket_count = int(math.ceil((24 * 60) / interval_minutes))
    buckets = list(range(bucket_count))

    rows = lift_rows[lift_rows["_lift_id"].notna()].copy()
    if rows.empty:
        return pd.DataFrame(columns=columns)

    lift_ids = sorted({safe_text(value) for value in rows["_lift_id"].dropna()}, key=natural_key)
    if not lift_ids:
        return pd.DataFrame(columns=columns)

    if ctx.has_datetime:
        event_time = pd.to_datetime(rows[ctx.time_col], errors="coerce")
        minute_of_day = event_time.dt.hour * 60 + event_time.dt.minute
    else:
        event_seconds = pd.to_numeric(rows[ctx.time_col], errors="coerce")
        minute_of_day = ((event_seconds % 86400) // 60).astype("float")

    rows = rows.assign(
        _profile_lift_id=rows["_lift_id"].map(safe_text),
        _profile_minute=minute_of_day,
    )
    rows = rows[rows["_profile_minute"].notna()].copy()
    if rows.empty:
        return pd.DataFrame(columns=columns)

    rows["_profile_bucket"] = (
        rows["_profile_minute"].astype(float) // interval_minutes
    ).astype(int).clip(lower=0, upper=bucket_count - 1)

    counts = (
        rows.groupby(["_profile_bucket", "_profile_lift_id"], dropna=False)
        .size()
        .rename("trips")
        .reset_index()
    )

    index = pd.MultiIndex.from_product(
        [buckets, lift_ids], names=["_profile_bucket", "_profile_lift_id"]
    )
    counts = (
        counts.set_index(["_profile_bucket", "_profile_lift_id"])
        .reindex(index, fill_value=0)
        .reset_index()
    )

    def _label(start_minute: int) -> str:
        hour = int(start_minute // 60)
        minute = int(start_minute % 60)
        return f"{hour:02d}:{minute:02d}"

    counts["interval_start_min"] = counts["_profile_bucket"] * interval_minutes
    counts["interval"] = counts["interval_start_min"].map(_label)
    counts["lift_id"] = counts["_profile_lift_id"].map(safe_text)
    counts["trips"] = pd.to_numeric(counts["trips"], errors="coerce").fillna(0).astype(int)
    return counts[columns].sort_values(["interval_start_min", "lift_id"], key=lambda col: col.map(natural_key) if col.name == "lift_id" else col).reset_index(drop=True)

def extract_lift_and_floor(value) -> Tuple[Optional[str], Optional[int]]:
    if value is None or pd.isna(value):
        return None, None
    text = str(value).strip()
    if not text:
        return None, None
    lift_match = re.search(r"([Ll]ift\s*[-_ ]?\d+|\bL\d+\b)", text)
    floor_match = re.search(r"(?:^|[^A-Za-z])(B?\d+)(?:$|[^A-Za-z])", text)
    lift_id = lift_match.group(1).replace(" ", "") if lift_match else None
    floor_no = None
    if floor_match:
        token = floor_match.group(1).upper()
        floor_no = -int(token[1:]) if token.startswith("B") else int(token)
    return lift_id, floor_no


def derive_lift_columns(
    df: pd.DataFrame, cols: Dict[str, Optional[str]]
) -> pd.DataFrame:
    df = df.copy()
    derived_lift: List[Optional[str]] = []
    derived_from_floor: List[Optional[int]] = []
    derived_to_floor: List[Optional[int]] = []
    for _, row in df.iterrows():
        from_lift, from_floor = extract_lift_and_floor(
            row.get(cols["from"]) if cols["from"] else None
        )
        to_lift, to_floor = extract_lift_and_floor(
            row.get(cols["to"]) if cols["to"] else None
        )
        final_lift = (
            safe_text(row.get(cols["lift"]))
            if cols["lift"] and pd.notna(row.get(cols["lift"]))
            else None
        )
        if final_lift == "-":
            final_lift = None
        final_lift = final_lift or from_lift or to_lift
        derived_lift.append(final_lift)
        derived_from_floor.append(from_floor)
        derived_to_floor.append(to_floor)
    df["_lift_id"] = derived_lift
    df["_lift_from_floor"] = derived_from_floor
    df["_lift_to_floor"] = derived_to_floor
    return df


def load_payload_weights(json_path: Path) -> Dict[str, float]:
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    payloads = data.get("payloads", [])
    weights: Dict[str, float] = {}

    for item in payloads:
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        try:
            weights[name] = float(item.get("weight_kg", 0))
        except (TypeError, ValueError):
            weights[name] = 0.0

    return weights


def _to_float(value, default: float = 0.0) -> float:
    try:
        if value is None or pd.isna(value):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def polygon_area(points: List[dict]) -> float:
    if not points or len(points) < 3:
        return 0.0
    coords = [(_to_float(p.get("dx")), _to_float(p.get("dy"))) for p in points]
    total = 0.0
    for i, (x1, y1) in enumerate(coords):
        x2, y2 = coords[(i + 1) % len(coords)]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def polygon_dimensions(points: List[dict]) -> Tuple[float, float]:
    if not points:
        return 0.0, 0.0
    xs = [_to_float(p.get("dx")) for p in points]
    ys = [_to_float(p.get("dy")) for p in points]
    return max(xs) - min(xs), max(ys) - min(ys)


def load_payload_dimensions(json_path: Path) -> pd.DataFrame:
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    rows: List[dict] = []
    for item in data.get("payloads", []):
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        length = _to_float(item.get("length_m"))
        width = _to_float(item.get("width_m"))
        height = _to_float(item.get("height_m"))
        rows.append(
            {
                "payload": name,
                "payload_weight_kg": _to_float(item.get("weight_kg")),
                "payload_length_m": length,
                "payload_width_m": width,
                "payload_height_m": height,
                "payload_area_m2": round(length * width, 3),
            }
        )
    return pd.DataFrame(rows)


def _config_display_text(item: dict, fallback: str = "") -> str:
    for key in ("display_name", "descriptive_name", "friendly_name", "label", "title", "name"):
        text = str((item or {}).get(key, "") or "").strip()
        if text:
            return text
    return str(fallback or "").strip()


def _to_bool(value, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on", "required", "enabled"}:
        return True
    if text in {"0", "false", "no", "n", "off", "none", "disabled"}:
        return False
    return bool(default)


def _config_bool(item: dict, keys: Iterable[str]) -> Optional[bool]:
    for key in keys:
        if key not in (item or {}):
            continue
        value = item.get(key)
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "y", "on", "required"}:
            return True
        if text in {"0", "false", "no", "n", "off", "none"}:
            return False
    return None


def _normalise_hhmm_text(value) -> str:
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if not match:
        return ""
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return ""
    return f"{hour:02d}:{minute:02d}"


def _configured_handling_minutes(item: dict) -> float:
    if not isinstance(item, dict):
        return 0.0
    for key in (
        "staff_handling_minutes",
        "handling_duration_minutes",
        "person_handling_minutes",
        "set_down_and_pack_away_minutes",
        "return_delay_minutes",
    ):
        value = _to_float(item.get(key), 0.0)
        if value > 0.0:
            return float(value)
    component_total = sum(
        max(0.0, _to_float(item.get(key), 0.0))
        for key in (
            "set_down_minutes",
            "setdown_minutes",
            "person_pack_away_minutes",
            "pack_away_minutes",
            "packaway_minutes",
        )
    )
    return float(component_total)


def _configured_staff_weekly_hours(item: dict) -> Optional[dict]:
    if not isinstance(item, dict) or not _to_bool(
        item.get("staff_use_custom_working_hours", False), False
    ):
        return None
    source = item.get("staff_working_hours", {})
    if not isinstance(source, dict):
        return None
    result = {}
    for day_key in ("mon", "tue", "wed", "thu", "fri", "sat", "sun"):
        raw = source.get(day_key, {})
        raw = raw if isinstance(raw, dict) else {}
        enabled = _to_bool(raw.get("enabled", False), False)
        start = _normalise_hhmm_text(raw.get("start_time"))
        end = _normalise_hhmm_text(raw.get("end_time"))
        result[day_key] = {
            "enabled": enabled,
            "start": start or "09:00",
            "end": end or "17:00",
        }
    return result


def _configured_staff_hours(
    item: dict, allow_timeframe: bool = False, staff_config: Optional[dict] = None
) -> Optional[Tuple[str, str]]:
    if not isinstance(item, dict):
        return None
    weekly = _configured_staff_weekly_hours(item)
    if weekly:
        for day_key in ("mon", "tue", "wed", "thu", "fri", "sat", "sun"):
            value = weekly.get(day_key, {})
            if value.get("enabled", False):
                return value.get("start", "09:00"), value.get("end", "17:00")
    start_keys = (
        "staff_hours_start",
        "staff_start_time",
        "staff_work_start",
        "staff_shift_start",
    )
    end_keys = (
        "staff_hours_end",
        "staff_end_time",
        "staff_work_end",
        "staff_shift_end",
    )
    start = next((_normalise_hhmm_text(item.get(key)) for key in start_keys if _normalise_hhmm_text(item.get(key))), "")
    end = next((_normalise_hhmm_text(item.get(key)) for key in end_keys if _normalise_hhmm_text(item.get(key))), "")
    if allow_timeframe and (not start or not end):
        start = start or _normalise_hhmm_text(item.get("timeframe_start"))
        end = end or _normalise_hhmm_text(item.get("timeframe_end"))
    if (not start or not end) and isinstance(staff_config, dict):
        pattern_key = str(item.get("staff_shift_pattern", "none") or "none").strip().lower()
        if pattern_key in {"4_on_4_off_12h", "four_on_four_off", "four_on_four_off_12_hour"}:
            pattern_key = "four_on_four_off_12h"
        patterns = staff_config.get("shift_patterns", {}) or {}
        pattern = patterns.get(pattern_key, {}) if isinstance(patterns, dict) else {}
        if isinstance(pattern, dict):
            start = start or _normalise_hhmm_text(pattern.get("start_time"))
            end = end or _normalise_hhmm_text(pattern.get("end_time"))
    return (start, end) if start and end else None


def load_task_generation_report_metadata(json_path: Path) -> dict:
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    task_generation = data.get("task_generation", {}) or {}
    categories = task_generation.get("categories", {}) or {}
    staff_config = task_generation.get("staff_config", {}) or {}
    category_labels: Dict[str, str] = {}
    category_human_assist: Dict[str, str] = {}
    category_staff_initial: Dict[str, int] = {}
    category_staff_shift_multiplier: Dict[str, float] = {}
    category_schedule_times: Dict[str, List[str]] = {}
    category_handling_duration_minutes: Dict[str, float] = {}
    department_handling_duration_minutes: Dict[str, Dict[str, float]] = {}
    category_staff_hours: Dict[str, dict] = {}
    department_staff_hours: Dict[str, Dict[str, dict]] = {}
    category_staff_weekly_hours: Dict[str, dict] = {}
    department_staff_weekly_hours: Dict[str, Dict[str, dict]] = {}
    assist_keys = (
        "human_assist_required",
        "requires_human_assist",
        "person_required",
        "requires_person",
        "attendant_required",
        "porter_required",
        "manual_unload_required",
        "requires_manual_unload",
        "requires_staff",
        "staff_required",
    )
    configured_assist_categories = {
        str(x).strip().lower()
        for x in task_generation.get(
            "human_assist_categories",
            task_generation.get("staff_assisted_categories", []),
        )
        or []
        if str(x).strip()
    }
    # Catering and stores pre-date the generic category staff controls. Keep
    # these as a compatibility fallback only when the category contains no
    # explicit staff setting at any level.
    legacy_assist_categories = {"catering", "stores"}

    for key, cfg in categories.items():
        if not isinstance(cfg, dict):
            continue
        key_text = str(key).strip()
        key_lower = key_text.lower()
        category_labels[key_lower] = _config_display_text(cfg, key_text.title())

        assist_true = False
        assist_false = False
        assist_setting_seen = False

        def staff_shift_multiplier(item: dict) -> float:
            pattern = str(item.get("staff_shift_pattern", "") or "").strip().lower()
            if pattern in {
                "4_on_4_off_12h",
                "four_on_four_off",
                "four_on_four_off_12_hour",
            }:
                pattern = "four_on_four_off_12h"
            return 2.0 if pattern == "four_on_four_off_12h" else 1.0

        def note_staff_shift(item: dict) -> None:
            category_staff_shift_multiplier[key_lower] = max(
                category_staff_shift_multiplier.get(key_lower, 1.0),
                staff_shift_multiplier(item),
            )

        def add_schedule_values(item: dict) -> None:
            schedule_values = item.get("scheduled_times", item.get("schedule_times", []))
            if isinstance(schedule_values, str):
                schedule_values = [x.strip() for x in schedule_values.split(",")]
            if not isinstance(schedule_values, list):
                return
            existing = category_schedule_times.setdefault(key_lower, [])
            seen = set(existing)
            for value in schedule_values:
                text = str(value).strip()
                if re.fullmatch(r"\d{1,2}:\d{2}", text) and text not in seen:
                    existing.append(text)
                    seen.add(text)

        def inspect_staff_settings(item: dict, department_ids: Optional[Iterable[str]] = None) -> None:
            nonlocal assist_true, assist_false, assist_setting_seen
            if not isinstance(item, dict):
                return
            add_schedule_values(item)
            explicit = _config_bool(item, assist_keys)
            if explicit is not None:
                assist_setting_seen = True
                if explicit:
                    assist_true = True
                else:
                    assist_false = True
            if explicit is True:
                note_staff_shift(item)
                try:
                    category_staff_initial[key_lower] = max(
                        category_staff_initial.get(key_lower, 1),
                        int(float(item.get("staff_initial_count", 1) or 1)),
                    )
                except Exception:
                    category_staff_initial.setdefault(key_lower, 1)

            handling_minutes = _configured_handling_minutes(item)
            if handling_minutes > 0.0:
                category_handling_duration_minutes[key_lower] = max(
                    category_handling_duration_minutes.get(key_lower, 0.0),
                    handling_minutes,
                )

            weekly_hours = _configured_staff_weekly_hours(item)
            hours = _configured_staff_hours(
                item,
                allow_timeframe=(key_lower == "linen"),
                staff_config=staff_config,
            )
            if weekly_hours and key_lower not in category_staff_weekly_hours:
                category_staff_weekly_hours[key_lower] = weekly_hours
            if hours and key_lower not in category_staff_hours:
                category_staff_hours[key_lower] = {
                    "start": hours[0],
                    "end": hours[1],
                }

            for department_id in department_ids or []:
                department_id = str(department_id or "").strip()
                if not department_id:
                    continue
                if handling_minutes > 0.0:
                    per_department = department_handling_duration_minutes.setdefault(
                        key_lower, {}
                    )
                    per_department[department_id] = max(
                        per_department.get(department_id, 0.0), handling_minutes
                    )
                if weekly_hours:
                    department_staff_weekly_hours.setdefault(key_lower, {})[
                        department_id
                    ] = weekly_hours
                if hours:
                    department_staff_hours.setdefault(key_lower, {})[department_id] = {
                        "start": hours[0],
                        "end": hours[1],
                    }

        inspect_staff_settings(cfg)
        for dept_id, dept_cfg in (cfg.get("departments", {}) or {}).items():
            inspect_staff_settings(dept_cfg, [dept_id])
        for group in cfg.get("department_groups", []) or []:
            if not isinstance(group, dict):
                continue
            inspect_staff_settings(
                group.get("payload", {}) or {},
                group.get("departments", []) or [],
            )

        if key_lower == "linen" and key_lower not in category_staff_hours:
            category_staff_hours[key_lower] = {"start": "09:00", "end": "17:00"}

        if assist_true:
            category_human_assist[key_lower] = "Yes"
        elif assist_false:
            category_human_assist[key_lower] = "No"
        elif key_lower in configured_assist_categories:
            category_human_assist[key_lower] = "Yes"
        elif not assist_setting_seen and key_lower in legacy_assist_categories:
            category_human_assist[key_lower] = "Yes"
        else:
            category_human_assist[key_lower] = "No"

        if key_lower in category_schedule_times:
            category_schedule_times[key_lower] = sorted(
                category_schedule_times[key_lower],
                key=lambda value: _hhmm_to_seconds(value) or 0,
            )

    department_by_location: Dict[str, str] = {}
    category_by_location: Dict[str, str] = {}
    department_names: Dict[str, str] = {}
    for dept in data.get("departments", []) or []:
        dept_id = str(dept.get("id", "") or "").strip()
        dept_name = _config_display_text(dept, dept_id or "-")
        if dept_id:
            department_names[dept_id] = dept_name
        task_locations = dept.get("task_generation_locations", {}) or {}
        for category, cfg in task_locations.items():
            category_key = str(category).strip().lower()
            category_label = category_labels.get(category_key, str(category).title())
            for loc_name in (cfg or {}).get("pickup_dropoff_locations", []) or []:
                if not loc_name:
                    continue
                loc_text = str(loc_name).strip()
                department_by_location[loc_text] = dept_name
                category_by_location[loc_text] = category_label

    location_display_names: Dict[str, str] = {}
    location_points: Dict[str, dict] = {}
    for loc in data.get("locations", []) or []:
        name = str(loc.get("name", "") or "").strip()
        if not name:
            continue
        location_points[name] = {
            "x": _to_float(loc.get("x"), 0.0),
            "y": _to_float(loc.get("y"), 0.0),
            "floor": loc.get("floor", ""),
        }
        display_name = _config_display_text(loc, "")
        if display_name and display_name != name:
            location_display_names[name] = display_name
            continue
        dept_name = department_by_location.get(name, "")
        category_label = category_by_location.get(name, "")
        if dept_name and category_label:
            location_display_names[name] = f"{dept_name} ({category_label})"
        elif dept_name:
            location_display_names[name] = dept_name
        else:
            location_display_names[name] = name

    return {
        "category_labels": category_labels,
        "category_human_assist": category_human_assist,
        "category_staff_initial": category_staff_initial,
        "category_staff_shift_multiplier": category_staff_shift_multiplier,
        "category_schedule_times": category_schedule_times,
        "category_handling_duration_minutes": category_handling_duration_minutes,
        "department_handling_duration_minutes": department_handling_duration_minutes,
        "category_staff_hours": category_staff_hours,
        "department_staff_hours": department_staff_hours,
        "category_staff_weekly_hours": category_staff_weekly_hours,
        "department_staff_weekly_hours": department_staff_weekly_hours,
        "department_names": department_names,
        "location_display_names": location_display_names,
        "location_points": location_points,
    }


def load_location_catalog(json_path: Path) -> pd.DataFrame:
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    location_to_department: Dict[str, str] = {}
    location_to_category: Dict[str, str] = {}
    for dept in data.get("departments", []):
        dept_name = str(dept.get("name") or dept.get("id") or "-").strip() or "-"
        task_locations = dept.get("task_generation_locations", {}) or {}
        for category, cfg in task_locations.items():
            for loc_name in (cfg or {}).get("pickup_dropoff_locations", []) or []:
                if not loc_name:
                    continue
                location_to_department[str(loc_name)] = dept_name
                location_to_category[str(loc_name)] = str(category).title()

    rows: List[dict] = []
    for loc in data.get("locations", []):
        name = str(loc.get("name", "")).strip()
        if not name:
            continue
        bbox = loc.get("bounding_box", []) or []
        length, width = polygon_dimensions(bbox)
        area = polygon_area(bbox)
        spaces = loc.get("inventory_spaces", None)
        spaces_list = spaces if isinstance(spaces, list) else []
        explicit_spaces = spaces is not None
        inventory_area = sum(
            polygon_area((space or {}).get("points", []) or []) for space in spaces_list
        )
        rows.append(
            {
                "location": name,
                "department": location_to_department.get(name, "-"),
                "category": location_to_category.get(name, "-"),
                "floor": loc.get("floor", "-"),
                "length_m": round(length, 2),
                "width_m": round(width, 2),
                "area_m2": round(area, 2),
                "inventory_spaces_current": len(spaces_list),
                "inventory_area_m2": round(inventory_area, 2),
                "inventory_spaces_defined": explicit_spaces,
            }
        )
    return pd.DataFrame(rows)


def _empty_location_outputs() -> (
    Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]
):
    return (
        pd.DataFrame(
            columns=[
                "department",
                "category",
                "location",
                "floor",
                "length_m",
                "width_m",
                "area_m2",
                "inventory_spaces_current",
                "deliveries_completed",
                "failed_delivery_attempts",
                "capacity_related_failures",
                "utilisation_pct",
                "recommended_area_m2",
                "recommended_inventory_spaces",
            ]
        ),
        pd.DataFrame(
            columns=[
                "time",
                "task_id",
                "amr",
                "department",
                "category",
                "location",
                "payload",
                "payload_length_m",
                "payload_width_m",
                "payload_height_m",
                "payload_area_m2",
                "failure_reason",
            ]
        ),
        pd.DataFrame(
            columns=[
                "department",
                "category",
                "location",
                "current_area_m2",
                "recommended_area_m2",
                "additional_area_m2",
                "current_inventory_spaces",
                "recommended_inventory_spaces",
                "additional_inventory_spaces",
                "reason",
            ]
        ),
        pd.DataFrame(
            columns=[
                "location",
                "payload",
                "payload_length_m",
                "payload_width_m",
                "payload_height_m",
                "failed_count",
            ]
        ),
    )


def build_location_space_analysis(
    tasks: pd.DataFrame,
    location_catalog: Optional[pd.DataFrame],
    payload_dimensions: Optional[pd.DataFrame],
    has_datetime: bool,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if location_catalog is None or location_catalog.empty:
        return _empty_location_outputs()

    loc = location_catalog.copy()
    payload_dims = (
        payload_dimensions.copy() if payload_dimensions is not None else pd.DataFrame()
    )
    if payload_dims.empty:
        payload_dims = pd.DataFrame(
            columns=[
                "payload",
                "payload_length_m",
                "payload_width_m",
                "payload_height_m",
                "payload_area_m2",
            ]
        )

    task_ext = tasks.copy()
    task_ext = task_ext.merge(payload_dims, on="payload", how="left")
    loc_meta = loc[["location", "department", "category"]].copy()
    task_ext = task_ext.merge(
        loc_meta, left_on="destination", right_on="location", how="left"
    )
    task_ext["location"] = task_ext["location"].fillna(task_ext["destination"])
    task_ext["department"] = task_ext["department"].fillna("-")
    task_ext["category"] = task_ext["category"].fillna("-")

    reason_col = "failure_reason" if "failure_reason" in task_ext.columns else None
    if reason_col is None:
        task_ext["failure_reason"] = task_ext.apply(
            lambda r: (
                "Task failed; no detailed reason was present in the CSV event log."
                if r.get("outcome") == "failed"
                else "-"
            ),
            axis=1,
        )

    failed_attempts = task_ext[task_ext["outcome"] == "failed"].copy()
    failed_attempts["capacity_related_failure"] = (
        failed_attempts["failure_reason"]
        .astype(str)
        .str.contains(
            r"invent|space|capacity|full|store|storage|dimension|fit|area",
            case=False,
            na=False,
        )
    )

    delivery_counts = (
        task_ext[task_ext["outcome"] == "completed"]
        .groupby("location", dropna=False)
        .size()
        .reset_index(name="deliveries_completed")
    )
    failed_counts = (
        failed_attempts.groupby("location", dropna=False)
        .size()
        .reset_index(name="failed_delivery_attempts")
    )
    capacity_failed_counts = (
        failed_attempts[failed_attempts["capacity_related_failure"]]
        .groupby("location", dropna=False)
        .size()
        .reset_index(name="capacity_related_failures")
    )
    failed_area = (
        failed_attempts.groupby("location", dropna=False)["payload_area_m2"]
        .sum()
        .reset_index(name="failed_payload_area_m2")
    )
    max_failed_area = (
        failed_attempts.groupby("location", dropna=False)["payload_area_m2"]
        .max()
        .reset_index(name="max_failed_payload_area_m2")
    )

    util = loc.merge(delivery_counts, on="location", how="left")
    util = util.merge(failed_counts, on="location", how="left")
    util = util.merge(capacity_failed_counts, on="location", how="left")
    util = util.merge(failed_area, on="location", how="left")
    util = util.merge(max_failed_area, on="location", how="left")
    for col in [
        "deliveries_completed",
        "failed_delivery_attempts",
        "capacity_related_failures",
    ]:
        util[col] = util[col].fillna(0).astype(int)
    for col in ["failed_payload_area_m2", "max_failed_payload_area_m2", "area_m2"]:
        util[col] = pd.to_numeric(util[col], errors="coerce").fillna(0.0)

    util["inventory_spaces_current"] = (
        pd.to_numeric(util["inventory_spaces_current"], errors="coerce")
        .fillna(0)
        .astype(int)
    )
    inventory_area_numeric = pd.to_numeric(
        util["inventory_area_m2"], errors="coerce"
    ).fillna(0.0)
    location_area_numeric = pd.to_numeric(util["area_m2"], errors="coerce").fillna(0.0)
    util["utilisation_pct"] = (
        (
            inventory_area_numeric.div(
                location_area_numeric.where(location_area_numeric > 0)
            )
            * 100
        )
        .fillna(0)
        .round(1)
    )

    # Capacity recommendation baseline.
    #
    # Do not convert every failed delivery attempt into another permanent
    # inventory space.  A burst of repeated failures at a hub location can turn
    # 19 trolley destinations into hundreds of "recommended" spaces.  Space
    # count is corrected later from simulator peak simultaneous occupancy rows
    # when those rows are available.  This fallback only protects older CSVs
    # that do not contain peak-location metrics.
    util["recommended_area_m2"] = util.apply(
        lambda r: max(
            float(r["area_m2"]),
            float(r["area_m2"]) + (float(r["max_failed_payload_area_m2"]) * 1.30),
        ),
        axis=1,
    ).round(2)
    util["recommended_inventory_spaces"] = util.apply(
        lambda r: max(
            int(r["inventory_spaces_current"]),
            int(r["inventory_spaces_current"]) + (
                1 if int(r["capacity_related_failures"]) > 0 else 0
            ),
            (
                1
                if int(r["failed_delivery_attempts"]) > 0
                else int(r["inventory_spaces_current"])
            ),
        ),
        axis=1,
    ).astype(int)

    failed_detail = (
        failed_attempts[
            [
                "start",
                "task_id",
                "amr",
                "department",
                "category",
                "location",
                "payload",
                "payload_length_m",
                "payload_width_m",
                "payload_height_m",
                "payload_area_m2",
                "failure_reason",
            ]
        ].copy()
        if not failed_attempts.empty
        else pd.DataFrame(
            columns=[
                "start",
                "task_id",
                "amr",
                "department",
                "category",
                "location",
                "payload",
                "payload_length_m",
                "payload_width_m",
                "payload_height_m",
                "payload_area_m2",
                "failure_reason",
            ]
        )
    )
    failed_detail = failed_detail.rename(columns={"start": "time"})
    if not failed_detail.empty:
        failed_detail["time"] = failed_detail["time"].map(
            lambda v: fmt_ts(v, has_datetime) if not pd.isna(v) else "-"
        )
        for c in [
            "payload_length_m",
            "payload_width_m",
            "payload_height_m",
            "payload_area_m2",
        ]:
            failed_detail[c] = (
                pd.to_numeric(failed_detail[c], errors="coerce").fillna(0).round(2)
            )

    failed_payload_sizes = (
        failed_attempts.groupby(
            [
                "location",
                "payload",
                "payload_length_m",
                "payload_width_m",
                "payload_height_m",
            ],
            dropna=False,
        )
        .size()
        .reset_index(name="failed_count")
        .sort_values(["location", "failed_count"], ascending=[True, False])
        if not failed_attempts.empty
        else pd.DataFrame(
            columns=[
                "location",
                "payload",
                "payload_length_m",
                "payload_width_m",
                "payload_height_m",
                "failed_count",
            ]
        )
    )

    recommendations = util.copy()
    recommendations["additional_area_m2"] = (
        (recommendations["recommended_area_m2"] - recommendations["area_m2"])
        .clip(lower=0)
        .round(2)
    )
    recommendations["additional_inventory_spaces"] = (
        (
            recommendations["recommended_inventory_spaces"]
            - recommendations["inventory_spaces_current"]
        )
        .clip(lower=0)
        .astype(int)
    )
    recommendations["reason"] = recommendations.apply(
        lambda r: (
            "Increase storage for failed capacity-related delivery attempts."
            if int(r["capacity_related_failures"]) > 0
            else (
                "Review location: failed deliveries occurred but no capacity keyword was logged."
                if int(r["failed_delivery_attempts"]) > 0
                else "No failed delivery pressure identified."
            )
        ),
        axis=1,
    )
    recommendations = recommendations[
        [
            "department",
            "category",
            "location",
            "area_m2",
            "recommended_area_m2",
            "additional_area_m2",
            "inventory_spaces_current",
            "recommended_inventory_spaces",
            "additional_inventory_spaces",
            "reason",
        ]
    ].rename(
        columns={
            "area_m2": "current_area_m2",
            "inventory_spaces_current": "current_inventory_spaces",
        }
    )

    util = (
        util[
            [
                "department",
                "category",
                "location",
                "floor",
                "length_m",
                "width_m",
                "area_m2",
                "inventory_spaces_current",
                "deliveries_completed",
                "failed_delivery_attempts",
                "capacity_related_failures",
                "utilisation_pct",
                "recommended_area_m2",
                "recommended_inventory_spaces",
            ]
        ]
        .sort_values(["department", "category", "location"])
        .reset_index(drop=True)
    )

    return (
        util,
        failed_detail.reset_index(drop=True),
        recommendations.reset_index(drop=True),
        failed_payload_sizes.reset_index(drop=True),
    )



def build_location_peak_occupancy(
    df: pd.DataFrame,
    ctx: Context,
    location_catalog: Optional[pd.DataFrame] = None,
    payload_dimensions: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """Calculate peak location occupancy from payload enter/exit events.

    The simulator is now responsible only for logging physical movements using
    ``location_payload_enter`` and ``location_payload_exit`` rows.  The report
    reconstructs the live set of payload instances at each location over time and
    takes the maximum simultaneous count, area and volume.

    Older CSVs that only contain ``location_space_recommendation`` rows are still
    supported as a fallback.
    """
    columns = [
        "department",
        "category",
        "location",
        "inventory_spaces_disabled",
        "configured_inventory_area_m2",
        "peak_payload_count",
        "peak_area_used_m2",
        "peak_volume_m3",
        "current_area_used_m2",
        "current_volume_m3",
        "recommended_area_m2",
        "recommended_volume_m3",
    ]
    if df is None or df.empty:
        return pd.DataFrame(columns=columns)

    loc_meta = pd.DataFrame(columns=["location", "department", "category", "inventory_area_m2"])
    if location_catalog is not None and not location_catalog.empty:
        keep = [
            c
            for c in ["location", "department", "category", "inventory_area_m2"]
            if c in location_catalog.columns
        ]
        if "location" in keep:
            loc_meta = location_catalog[keep].drop_duplicates("location").copy()

    payload_lookup: Dict[str, Tuple[float, float]] = {}
    if payload_dimensions is not None and not payload_dimensions.empty:
        for _, row in payload_dimensions.iterrows():
            payload_name = str(row.get("payload", "") or "").strip()
            if not payload_name:
                continue
            length = _to_float(row.get("payload_length_m"), 0.0)
            width = _to_float(row.get("payload_width_m"), 0.0)
            height = _to_float(row.get("payload_height_m"), 0.0)
            area = _to_float(row.get("payload_area_m2"), length * width)
            if area <= 0.0:
                area = max(0.0, length) * max(0.0, width)
            payload_lookup[payload_name] = (area, area * max(0.0, height))

    def _clean_location(value) -> str:
        text = str(value or "").strip()
        return "" if text.lower() in {"", "-", "nan", "none", "null"} else text

    def _clean_payload(value) -> str:
        text = str(value or "").strip()
        return "" if text.lower() in {"", "-", "nan", "none", "null", "empty", "__empty_payload__"} else text

    def _instance_key(row: pd.Series, location: str, payload: str, counter: int) -> str:
        instance = str(row.get("payload_instance_id", "") or "").strip()
        if instance and instance.lower() not in {"nan", "none", "null", "-"}:
            return instance
        task_id = str(row.get("task_id", "") or "").strip()
        if task_id and task_id.lower() not in {"nan", "none", "null", "-"}:
            return f"task:{task_id}:{payload}"
        return f"synthetic:{location}:{payload}:{counter}"

    def _configured_area(location: str) -> float:
        if loc_meta.empty or "inventory_area_m2" not in loc_meta.columns:
            return 0.0
        match = loc_meta[loc_meta["location"] == location]
        if match.empty:
            return 0.0
        return round(_to_float(match.iloc[0].get("inventory_area_m2"), 0.0), 2)

    event_text = df.get("_event_text", pd.Series("", index=df.index)).astype(str)
    movement_rows = df[
        event_text.str.fullmatch(r"location_payload_(enter|exit)", case=False, na=False)
    ].copy()

    if movement_rows.empty:
        # Backwards compatibility for reports run against older simulator CSVs.
        final_rows = df[
            event_text.str.fullmatch("location_space_recommendation", case=False, na=False)
        ].copy()
        if final_rows.empty:
            return pd.DataFrame(columns=columns)
        location_source = next(
            (c for c in ("to_location", "end_node", "from_location") if c in final_rows.columns),
            None,
        )
        if location_source is None:
            return pd.DataFrame(columns=columns)
        final_rows["_report_location"] = final_rows[location_source].map(_clean_location)
        final_rows = final_rows[final_rows["_report_location"] != ""].sort_values(ctx.time_col, kind="stable")
        latest_rows = final_rows.drop_duplicates("_report_location", keep="last")

        def num(row: pd.Series, name: str) -> float:
            if name not in row.index:
                return 0.0
            value = pd.to_numeric(pd.Series([row.get(name)]), errors="coerce").iloc[0]
            return 0.0 if pd.isna(value) else float(value)

        rows = []
        for _, row in latest_rows.iterrows():
            location = _clean_location(row.get("_report_location", ""))
            peak_area = num(row, "location_peak_footprint_area_m2") or num(row, "location_payload_footprint_area_m2")
            peak_volume = num(row, "location_peak_volume_m3") or num(row, "location_payload_volume_m3")
            rows.append(
                {
                    "location": location,
                    "inventory_spaces_disabled": str(row.get("location_inventory_spaces_disabled", "")).strip().lower() in {"true", "1", "yes", "y"},
                    "configured_inventory_area_m2": round(num(row, "location_configured_inventory_area_m2") or _configured_area(location), 2),
                    "peak_payload_count": int(round(num(row, "location_peak_payload_count"))),
                    "peak_area_used_m2": round(peak_area, 2),
                    "peak_volume_m3": round(peak_volume, 2),
                    "current_area_used_m2": round(num(row, "location_payload_footprint_area_m2"), 2),
                    "current_volume_m3": round(num(row, "location_payload_volume_m3"), 2),
                    "recommended_area_m2": round((num(row, "location_recommended_area_m2") or peak_area * 1.30), 2),
                    "recommended_volume_m3": round((num(row, "location_recommended_volume_m3") or peak_volume * 1.30), 2),
                }
            )
    else:
        location_source = next(
            (c for c in ("to_location", "end_node", "from_location") if c in movement_rows.columns),
            None,
        )
        if location_source is None:
            return pd.DataFrame(columns=columns)
        movement_rows["_report_location"] = movement_rows[location_source].map(_clean_location)
        movement_rows["_report_payload"] = movement_rows.get("payload", pd.Series("", index=movement_rows.index)).map(_clean_payload)
        movement_rows = movement_rows[
            (movement_rows["_report_location"] != "") & (movement_rows["_report_payload"] != "")
        ].sort_values(ctx.time_col, kind="stable")

        states: Dict[str, Dict[str, Tuple[str, float, float]]] = {}
        peaks: Dict[str, dict] = {}
        instance_locations: Dict[str, str] = {}
        synthetic_counter = 0

        def snapshot(location: str) -> None:
            live = states.get(location, {})
            count = len(live)
            area = sum(v[1] for v in live.values())
            volume = sum(v[2] for v in live.values())
            item = peaks.setdefault(
                location,
                {
                    "peak_payload_count": 0,
                    "peak_area_used_m2": 0.0,
                    "peak_volume_m3": 0.0,
                    "current_area_used_m2": 0.0,
                    "current_volume_m3": 0.0,
                },
            )
            item["current_area_used_m2"] = area
            item["current_volume_m3"] = volume
            if count > int(item.get("peak_payload_count", 0)):
                item["peak_payload_count"] = count
            if area > float(item.get("peak_area_used_m2", 0.0)):
                item["peak_area_used_m2"] = area
            if volume > float(item.get("peak_volume_m3", 0.0)):
                item["peak_volume_m3"] = volume

        for _, row in movement_rows.iterrows():
            location = row["_report_location"]
            payload_name = row["_report_payload"]
            area, volume = payload_lookup.get(payload_name, (0.0, 0.0))
            synthetic_counter += 1
            key = _instance_key(row, location, payload_name, synthetic_counter)
            event = str(row.get("_event_text", "") or "").lower()
            live = states.setdefault(location, {})
            if event.endswith("enter"):
                # A physical payload instance can only occupy one location at a
                # time.  Some logs record the arrival more reliably than the
                # departure, so clear the same instance from any previous
                # location before adding it here.
                previous_location = instance_locations.get(key)
                if previous_location and previous_location != location:
                    previous_live = states.get(previous_location, {})
                    if key in previous_live:
                        previous_live.pop(key, None)
                        snapshot(previous_location)
                live[key] = (payload_name, area, volume)
                instance_locations[key] = location
            elif event.endswith("exit"):
                # Prefer exact instance removal, but fall back to the first matching
                # payload at that location so legacy rows without instance IDs still
                # form a useful occupancy timeline.
                removed_key = ""
                if key in live:
                    live.pop(key, None)
                    removed_key = key
                else:
                    match = next((k for k, v in live.items() if v[0] == payload_name), None)
                    if match is not None:
                        live.pop(match, None)
                        removed_key = match
                if removed_key:
                    instance_locations.pop(removed_key, None)
            snapshot(location)

        for location in list(states):
            snapshot(location)

        rows = []
        for location, item in peaks.items():
            peak_count = int(item.get("peak_payload_count", 0) or 0)
            peak_area = float(item.get("peak_area_used_m2", 0.0) or 0.0)
            peak_volume = float(item.get("peak_volume_m3", 0.0) or 0.0)
            configured_area = _configured_area(location)
            if peak_count <= 0 and peak_area <= 0.0 and configured_area <= 0.0:
                continue
            rows.append(
                {
                    "location": location,
                    "inventory_spaces_disabled": False,
                    "configured_inventory_area_m2": configured_area,
                    "peak_payload_count": peak_count,
                    "peak_area_used_m2": round(peak_area, 2),
                    "peak_volume_m3": round(peak_volume, 2),
                    "current_area_used_m2": round(float(item.get("current_area_used_m2", 0.0) or 0.0), 2),
                    "current_volume_m3": round(float(item.get("current_volume_m3", 0.0) or 0.0), 2),
                    "recommended_area_m2": round(peak_area * 1.30, 2),
                    "recommended_volume_m3": round(peak_volume * 1.30, 2),
                }
            )

    if not rows:
        return pd.DataFrame(columns=columns)
    peak = pd.DataFrame(rows).drop_duplicates("location", keep="last")
    if not loc_meta.empty:
        merge_cols = [c for c in ["location", "department", "category"] if c in loc_meta.columns]
        peak = peak.merge(loc_meta[merge_cols].drop_duplicates("location"), on="location", how="left")
    else:
        peak["department"] = "-"
        peak["category"] = "-"
    peak["department"] = peak["department"].fillna("-")
    peak["category"] = peak["category"].fillna("-")
    return peak[columns].sort_values(["department", "category", "location"]).reset_index(drop=True)


def apply_peak_occupancy_to_location_outputs(
    location_space_utilisation: pd.DataFrame,
    location_recommendations: pd.DataFrame,
    location_peak_occupancy: pd.DataFrame,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    if location_peak_occupancy is None or location_peak_occupancy.empty:
        return location_space_utilisation, location_recommendations

    peak_cols = [
        "location",
        "inventory_spaces_disabled",
        "configured_inventory_area_m2",
        "peak_payload_count",
        "peak_area_used_m2",
        "peak_volume_m3",
        "recommended_area_m2",
        "recommended_volume_m3",
    ]
    peak = location_peak_occupancy[[c for c in peak_cols if c in location_peak_occupancy.columns]].copy()
    peak = peak.rename(columns={"recommended_area_m2": "peak_recommended_area_m2"})

    util = location_space_utilisation.copy()
    if not util.empty and "location" in util.columns:
        util = util.merge(peak, on="location", how="left")
        for c in ["peak_payload_count", "peak_area_used_m2", "peak_volume_m3", "peak_recommended_area_m2", "recommended_volume_m3", "configured_inventory_area_m2"]:
            if c in util.columns:
                util[c] = pd.to_numeric(util[c], errors="coerce").fillna(0)
        if "peak_recommended_area_m2" in util.columns:
            util["recommended_area_m2"] = util[["recommended_area_m2", "peak_recommended_area_m2"]].max(axis=1).round(2)
        if "peak_payload_count" in util.columns:
            peak_count = pd.to_numeric(util["peak_payload_count"], errors="coerce").fillna(0).astype(int)
            current_spaces = pd.to_numeric(
                util.get("inventory_spaces_current", pd.Series(0, index=util.index)),
                errors="coerce",
            ).fillna(0).astype(int)
            existing_recommendation = pd.to_numeric(
                util.get("recommended_inventory_spaces", pd.Series(0, index=util.index)),
                errors="coerce",
            ).fillna(0).astype(int)

            # Peak simultaneous payload count is the authoritative space count.
            # This prevents repeated failed delivery attempts at a hub from being
            # interpreted as one permanent inventory space per failure.
            peak_space_recommendation = peak_count.where(peak_count > 0, existing_recommendation)
            util["recommended_inventory_spaces"] = pd.concat(
                [current_spaces, peak_space_recommendation], axis=1
            ).max(axis=1).astype(int)
        if "peak_area_used_m2" in util.columns:
            area = pd.to_numeric(util.get("area_m2", 0), errors="coerce").fillna(0)
            util["peak_utilisation_pct"] = (pd.to_numeric(util["peak_area_used_m2"], errors="coerce").fillna(0).div(area.where(area > 0)) * 100).fillna(0).round(1)

    rec = location_recommendations.copy()
    if not rec.empty and "location" in rec.columns:
        rec = rec.merge(peak, on="location", how="left")
        for c in ["peak_payload_count", "peak_area_used_m2", "peak_volume_m3", "peak_recommended_area_m2", "recommended_volume_m3"]:
            if c in rec.columns:
                rec[c] = pd.to_numeric(rec[c], errors="coerce").fillna(0)
        if "peak_recommended_area_m2" in rec.columns:
            rec["recommended_area_m2"] = rec[["recommended_area_m2", "peak_recommended_area_m2"]].max(axis=1).round(2)
            rec["additional_area_m2"] = (rec["recommended_area_m2"] - pd.to_numeric(rec["current_area_m2"], errors="coerce").fillna(0)).clip(lower=0).round(2)
        if "peak_payload_count" in rec.columns:
            peak_count = pd.to_numeric(rec["peak_payload_count"], errors="coerce").fillna(0).astype(int)
            current_spaces = pd.to_numeric(
                rec.get("current_inventory_spaces", pd.Series(0, index=rec.index)),
                errors="coerce",
            ).fillna(0).astype(int)
            existing_recommendation = pd.to_numeric(
                rec.get("recommended_inventory_spaces", pd.Series(0, index=rec.index)),
                errors="coerce",
            ).fillna(0).astype(int)
            peak_space_recommendation = peak_count.where(peak_count > 0, existing_recommendation)
            rec["recommended_inventory_spaces"] = pd.concat(
                [current_spaces, peak_space_recommendation], axis=1
            ).max(axis=1).astype(int)
            rec["additional_inventory_spaces"] = (
                rec["recommended_inventory_spaces"] - current_spaces
            ).clip(lower=0).astype(int)
        if "peak_area_used_m2" in rec.columns:
            rec["reason"] = rec.apply(
                lambda r: "Recommended from peak simultaneous payload storage demand recorded by the simulator; failed delivery attempts are not counted as one space each."
                if float(r.get("peak_area_used_m2", 0) or 0) > 0
                else r.get("reason", "-"),
                axis=1,
            )
    return util, rec

def load_amr_parameters(json_path: Path) -> pd.DataFrame:
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    amrs = data.get("amrs", [])
    rows: List[dict] = []

    for item in amrs:
        rows.append(
            {
                "amr": str(item.get("id", "")).strip() or "-",
                "quantity": int(item.get("quantity", 1) or 1),
                "payload_capacity_kg": item.get("payload_capacity_kg", "-"),
                "payload_capacity_size_units": item.get(
                    "payload_capacity_size_units", "-"
                ),
                "speed_m_per_sec": item.get("speed_m_per_sec", "-"),
                "battery_capacity_kwh": item.get("battery_capacity_kwh", "-"),
                "battery_charge_rate_kw": item.get("battery_charge_rate_kw", "-"),
                "recharge_threshold_percent": item.get(
                    "recharge_threshold_percent", "-"
                ),
                "battery_soc_percent": item.get("battery_soc_percent", "-"),
                "start_location": item.get("start_location", "-"),
            }
        )

    return pd.DataFrame(rows)


def load_floor_dxf_map(json_path: Path) -> Dict[int, str]:
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    rows = data.get("floor_dxf_files", data.get("dxf_files", []))
    floor_map: Dict[int, str] = {}

    for item in rows:
        try:
            floor = int(item.get("floor"))
        except (TypeError, ValueError):
            continue

        path = str(item.get("filepath", "")).strip()
        if not path:
            continue

        floor_map[floor] = path

    return floor_map


def is_lift_wait_row(row: pd.Series) -> bool:
    segment_text = str(row.get("_segment_text", "")).strip().lower()
    event_text = str(row.get("_event_text", "")).strip().lower()

    if "lift" in segment_text and "wait" in segment_text:
        return True
    if "lift" in event_text and "wait" in event_text:
        return True
    if segment_text == "lift_transfer" and float(row.get("_wait_s", 0) or 0) > 0:
        return True
    return False


def extract_congestion_point(
    row: pd.Series,
    cols: Dict[str, Optional[str]],
) -> Tuple[Optional[float], Optional[float], Optional[int]]:
    sx = pd.to_numeric(
        row.get(cols["start_x"]) if cols.get("start_x") else None,
        errors="coerce",
    )
    sy = pd.to_numeric(
        row.get(cols["start_y"]) if cols.get("start_y") else None,
        errors="coerce",
    )
    sf = pd.to_numeric(
        row.get(cols["start_floor"]) if cols.get("start_floor") else None,
        errors="coerce",
    )

    ex = pd.to_numeric(
        row.get(cols["end_x"]) if cols.get("end_x") else None,
        errors="coerce",
    )
    ey = pd.to_numeric(
        row.get(cols["end_y"]) if cols.get("end_y") else None,
        errors="coerce",
    )
    ef = pd.to_numeric(
        row.get(cols["end_floor"]) if cols.get("end_floor") else None,
        errors="coerce",
    )

    if pd.notna(sx) and pd.notna(sy) and pd.notna(sf):
        return float(sx), float(sy), int(sf)

    if pd.notna(ex) and pd.notna(ey) and pd.notna(ef):
        return float(ex), float(ey), int(ef)

    return None, None, None


def _task_generation_category_from_row(row: pd.Series, labels: Dict[str, str]) -> str:
    details = str(row.get("details", "") or "")
    match = re.search(r"Generated\s+(?:scheduled-threshold|scheduled|sporadic|threshold|timeframe)\s+(.+?)\s+task\b", details, re.I)
    if match:
        key = match.group(1).strip().lower()
        return labels.get(key, key.title())

    task_id = str(row.get("task_id", "") or "").strip()
    match = re.match(r"RETURN_GEN_([A-Z0-9]+)", task_id, re.I)
    if match:
        key = match.group(1).strip().lower()
        return f"{labels.get(key, key.title())} return"
    match = re.match(r"GEN_([A-Z0-9]+)", task_id, re.I)
    if match:
        key = match.group(1).strip().lower()
        return labels.get(key, key.title())

    task_source = str(row.get("task_source", "") or "").strip()
    if task_source == "department_waste":
        return labels.get("waste", "Waste")
    if task_source == "task_generation_return":
        return "Generated return"
    return task_source.replace("_", " ").title() if task_source else "Generated task"


def _task_generation_category_key(label: str, labels: Dict[str, str]) -> str:
    label_text = str(label or "").strip().lower()
    if label_text.endswith(" return"):
        label_text = label_text[: -len(" return")].strip()
    for key, display in labels.items():
        if label_text == str(display).strip().lower():
            return str(key).strip().lower()
    return re.sub(r"[^a-z0-9]+", "_", label_text).strip("_")


def _join_limited(values: Iterable[str], limit: int = 4) -> str:
    clean = []
    seen = set()
    for value in values or []:
        text = str(value or "").strip()
        if not text or text == "-" or text in seen:
            continue
        clean.append(text)
        seen.add(text)
    if not clean:
        return "-"
    if len(clean) <= limit:
        return ", ".join(clean)
    return ", ".join(clean[:limit]) + f" +{len(clean) - limit} more"


def build_generated_task_category_summary(
    df: pd.DataFrame,
    ctx: Context,
    metadata: Optional[dict] = None,
) -> pd.DataFrame:
    columns = [
        "category",
        "tasks",
        "first_time",
        "last_time",
        "pickup_locations",
        "dropoff_locations",
        "payloads",
        "human_assist",
    ]
    if df is None or df.empty:
        return pd.DataFrame(columns=columns)

    metadata = metadata or {}
    labels = {
        str(k).strip().lower(): str(v).strip()
        for k, v in (metadata.get("category_labels", {}) or {}).items()
        if str(k).strip()
    }
    location_names = metadata.get("location_display_names", {}) or {}
    human_assist = {
        str(k).strip().lower(): str(v).strip() or "-"
        for k, v in (metadata.get("category_human_assist", {}) or {}).items()
    }

    event_text = df.get("_event_text", pd.Series("", index=df.index)).astype(str)
    generated = df[
        event_text.str.fullmatch(
            r"task_generated|waste_task_generated|return_task_generated",
            case=False,
            na=False,
        )
    ].copy()
    if generated.empty:
        return pd.DataFrame(columns=columns)

    for col in ("details", "task_id", "task_source", "payload", "from_location", "to_location"):
        if col not in generated.columns:
            generated[col] = ""

    generated["_category"] = generated.apply(
        lambda row: _task_generation_category_from_row(row, labels),
        axis=1,
    )
    generated["_category_key"] = generated["_category"].map(
        lambda value: _task_generation_category_key(value, labels)
    )
    generated["_pickup_display"] = generated["from_location"].map(
        lambda value: location_names.get(str(value or "").strip(), safe_text(value))
    )
    generated["_dropoff_display"] = generated["to_location"].map(
        lambda value: location_names.get(str(value or "").strip(), safe_text(value))
    )
    generated["_payload_display"] = generated["payload"].map(safe_text)

    rows: List[dict] = []
    for category, sub in generated.groupby("_category", sort=True):
        sub = sub.sort_values(ctx.time_col)
        category_key = str(sub["_category_key"].iloc[0] or "").strip().lower()
        rows.append(
            {
                "category": category,
                "tasks": int(len(sub)),
                "first_time": fmt_ts(sub[ctx.time_col].iloc[0], ctx.has_datetime),
                "last_time": fmt_ts(sub[ctx.time_col].iloc[-1], ctx.has_datetime),
                "pickup_locations": _join_limited(sub["_pickup_display"].tolist()),
                "dropoff_locations": _join_limited(sub["_dropoff_display"].tolist()),
                "payloads": _join_limited(sub["_payload_display"].tolist(), limit=3),
                "human_assist": human_assist.get(category_key, "-"),
            }
        )

    return pd.DataFrame(rows, columns=columns).sort_values("category").reset_index(drop=True)


def _return_source_task_id(task_id: str) -> str:
    text = str(task_id or "").strip()
    if text.startswith("RETURN_GEN_"):
        body = text[len("RETURN_") :]
        parts = body.rsplit("_", 1)
        return parts[0] if len(parts) == 2 and parts[1].isdigit() else body
    if text.startswith("RETURN-"):
        body = text[len("RETURN-") :]
        parts = body.rsplit("-", 1)
        return parts[0] if len(parts) == 2 and parts[1].isdigit() else body
    return ""


def _event_day_key(value, has_datetime: bool) -> str:
    if pd.isna(value):
        return ""
    if has_datetime:
        return pd.Timestamp(value).strftime("%a")
    day_index = int(float(value) // 86400.0) % 7
    return ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][day_index]


def _event_time_hhmm(value, has_datetime: bool) -> str:
    if pd.isna(value):
        return "-"
    if has_datetime:
        return pd.Timestamp(value).strftime("%H:%M")
    seconds = max(0.0, float(value))
    seconds = seconds % 86400.0
    hour = int(seconds // 3600)
    minute = int((seconds % 3600) // 60)
    return f"{hour:02d}:{minute:02d}"


def _hhmm_to_seconds(value: str) -> Optional[int]:
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return None
    return hour * 3600 + minute * 60


def _scheduled_window_for_record(
    start_value,
    duration_s: float,
    schedule_times: List[str],
    has_datetime: bool,
) -> Optional[str]:
    schedule_seconds = [
        sec for sec in (_hhmm_to_seconds(value) for value in schedule_times) if sec is not None
    ]
    if not schedule_seconds or pd.isna(start_value):
        return None
    if has_datetime:
        ts = pd.Timestamp(start_value)
        seconds_of_day = ts.hour * 3600 + ts.minute * 60 + ts.second
    else:
        seconds_of_day = int(float(start_value or 0.0) % 86400.0)
    scheduled_start_sec = min(schedule_seconds, key=lambda sec: abs(sec - seconds_of_day))
    scheduled_finish_sec = min(86399, scheduled_start_sec + int(max(0.0, duration_s or 0.0)))
    return (
        f"{scheduled_start_sec // 3600:02d}:{(scheduled_start_sec % 3600) // 60:02d}-"
        f"{scheduled_finish_sec // 3600:02d}:{(scheduled_finish_sec % 3600) // 60:02d}"
    )


def _human_assist_category_keys(metadata: Optional[dict]) -> set:
    metadata = metadata or {}
    return {
        str(key).strip().lower()
        for key, value in (metadata.get("category_human_assist", {}) or {}).items()
        if str(value or "").strip().lower() in {"yes", "true", "1", "required"}
    }


def _staff_handling_event_mask(df: pd.DataFrame) -> pd.Series:
    """Return rows that represent actual destination-side staff handling.

    ``person_resource`` is also copied onto related rows such as
    ``return_task_generated``. Treating that metadata alone as a handling
    event incorrectly adds the original pickup location to the person's
    timetable at the same time as the delivery destination. Only explicit
    payload-handling event types are therefore included. The event-name
    pattern remains generic so catering, stores and any other assisted
    category are supported without category-specific code.
    """
    event_text = df.get("_event_text", pd.Series("", index=df.index)).astype(str)
    return event_text.str.fullmatch(
        r"(?:segment_)?(?:[a-z0-9]+(?:_[a-z0-9]+)*)_payload_handling",
        case=False,
        na=False,
    )


def _clean_optional_text(value) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return str(value).strip()


def _staff_category_from_row(
    row: pd.Series,
    labels: Dict[str, str],
) -> Tuple[str, str]:
    for column in ("staff_category_key", "category_key", "task_category"):
        key = _clean_optional_text(row.get(column, "")).lower()
        if key:
            return labels.get(key, key.replace("_", " ").title()), key

    resource = _clean_optional_text(row.get("person_resource", "")).lower()
    if resource.endswith("_payload_handling"):
        key = resource[: -len("_payload_handling")].strip("_")
        if key:
            return labels.get(key, key.replace("_", " ").title()), key

    category = _task_generation_category_from_row(row, labels)
    category_key = _task_generation_category_key(category, labels)
    return labels.get(category_key, category), category_key


def _staff_shift_team_from_row(row: pd.Series, person_id: str = "") -> str:
    """Return a concise shift/team label for a staff handling record."""
    team = _clean_optional_text(row.get("staff_shift_team", ""))
    if not team:
        match = re.search(r"(?:^|[-_\s])shift[-_\s]*([a-z0-9]+)(?:[-_\s]|$)", str(person_id or ""), re.I)
        if match:
            team = match.group(1)
    return team.upper()


def _staff_person_number_from_row(row: pd.Series, person_id: str = "") -> Optional[int]:
    """Extract the simulator's person number without exposing its raw ID."""
    for column in ("person_index", "staff_person_index"):
        value = pd.to_numeric(row.get(column), errors="coerce")
        if pd.notna(value) and int(value) > 0:
            return int(value)

    text = str(person_id or "").strip()
    match = re.search(r"(?:^|[-_\s])person[-_\s]*(\d+)(?:$|[-_\s])", text, re.I)
    if not match:
        match = re.search(r"(\d+)$", text)
    if match:
        return max(1, int(match.group(1)))
    return None


def _friendly_staff_name(category: str, shift_team: str, person_number: int) -> str:
    """Build a report-friendly staff name, e.g. Catering Shift A - Person 1."""
    category_text = str(category or "Staff").strip() or "Staff"
    team_text = str(shift_team or "").strip().upper()
    number = max(1, int(person_number or 1))
    if team_text:
        return f"{category_text} Shift {team_text} - Person {number}"
    return f"{category_text} - Person {number}"


DEFAULT_HUMAN_HANDLING_MINUTES = 15.0


def _positive_seconds(value) -> float:
    value = _to_float(value, 0.0)
    return float(value) if value > 0.0 else 0.0


def _time_pair_duration_seconds(start_value, end_value) -> float:
    if start_value is None or end_value is None:
        return 0.0
    try:
        if pd.isna(start_value) or pd.isna(end_value):
            return 0.0
    except Exception:
        pass

    # Numeric simulator clocks are measured in seconds. Do not run calendar
    # timestamps through pd.to_numeric because pandas represents them as
    # nanoseconds, which would inflate a 5-minute interval to 300 billion s.
    if pd.api.types.is_number(start_value) and pd.api.types.is_number(end_value):
        return max(0.0, float(end_value) - float(start_value))

    try:
        start_ts = pd.to_datetime(start_value, errors="coerce")
        end_ts = pd.to_datetime(end_value, errors="coerce")
        if pd.notna(start_ts) and pd.notna(end_ts):
            return max(
                0.0,
                (pd.Timestamp(end_ts) - pd.Timestamp(start_ts)).total_seconds(),
            )
    except Exception:
        pass

    # Numeric values may arrive as strings in older CSV exports.
    try:
        return max(0.0, float(end_value) - float(start_value))
    except (TypeError, ValueError):
        return 0.0


def _configured_record_handling_seconds(
    metadata: Optional[dict], category_key: str, department_id: str
) -> float:
    metadata = metadata or {}
    category_key = str(category_key or "").strip().lower()
    department_id = str(department_id or "").strip()
    department_values = (
        metadata.get("department_handling_duration_minutes", {}) or {}
    ).get(category_key, {}) or {}
    minutes = _to_float(department_values.get(department_id), 0.0)
    if minutes <= 0.0:
        minutes = _to_float(
            (metadata.get("category_handling_duration_minutes", {}) or {}).get(
                category_key
            ),
            0.0,
        )
    return max(0.0, minutes * 60.0)


def _handling_duration_seconds(
    row: pd.Series,
    metadata: Optional[dict],
    category_key: str,
    department_id: str,
    ready_start=None,
    return_time=None,
) -> float:
    logged_duration = max(
        (
            _positive_seconds(row.get(column))
            for column in (
                "duration_sec",
                "_duration_s",
                "staff_handling_duration_sec",
                "handling_duration_sec",
                "person_handling_duration_sec",
            )
        ),
        default=0.0,
    )

    # Current simulator rows also carry the handling start and finish even when
    # a legacy CSV exporter wrote duration_sec as zero.
    time_pair_duration = max(
        (
            _time_pair_duration_seconds(row.get(start_column), row.get(end_column))
            for start_column, end_column in (
                ("staff_start_time", "staff_end_time"),
                ("handling_start_time", "handling_end_time"),
                ("start_time", "end_time"),
            )
        ),
        default=0.0,
    )

    # Accept logs that separate set-down and pack-away activities. Where a
    # legacy duration contains only one component, the combined component time
    # prevents the report from ending the person's work too early.
    component_duration = sum(
        _positive_seconds(row.get(column))
        for column in (
            "set_down_duration_sec",
            "setdown_duration_sec",
            "set_down_time_sec",
            "person_pack_away_duration_sec",
            "pack_away_duration_sec",
            "packaway_duration_sec",
            "unload_duration_sec",
        )
    )
    component_duration += 60.0 * sum(
        max(0.0, _to_float(row.get(column), 0.0))
        for column in (
            "set_down_minutes",
            "person_pack_away_minutes",
            "pack_away_minutes",
        )
    )

    observed_duration = max(
        logged_duration, time_pair_duration, component_duration
    )
    if observed_duration > 0.0:
        return observed_duration

    # The delayed return release normally marks the end of pack-away time.
    if ready_start is not None and return_time is not None:
        return_duration = _time_pair_duration_seconds(ready_start, return_time)
        if return_duration > 0.0:
            return float(return_duration)

    configured_seconds = _configured_record_handling_seconds(
        metadata, category_key, department_id
    )
    if configured_seconds > 0.0:
        return configured_seconds

    # Human handling should never be displayed as a zero-minute activity.
    return DEFAULT_HUMAN_HANDLING_MINUTES * 60.0


def _record_staff_hours(
    metadata: Optional[dict],
    category_key: str,
    department_id: str,
    event_value=None,
    has_datetime: bool = False,
) -> Optional[Tuple[str, str]]:
    metadata = metadata or {}
    category_key = str(category_key or "").strip().lower()
    department_id = str(department_id or "").strip()
    department_weekly = (
        metadata.get("department_staff_weekly_hours", {}) or {}
    ).get(category_key, {}) or {}
    weekly = department_weekly.get(department_id) or (
        metadata.get("category_staff_weekly_hours", {}) or {}
    ).get(category_key)
    if isinstance(weekly, dict) and event_value is not None:
        try:
            if has_datetime:
                weekday = pd.Timestamp(event_value).weekday()
            else:
                weekday = int(float(event_value) // 86400.0) % 7
            day_key = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[weekday]
            day_value = weekly.get(day_key, {})
            if isinstance(day_value, dict):
                if not _to_bool(day_value.get("enabled", False), False):
                    return ("Closed", "Closed")
                start = _normalise_hhmm_text(day_value.get("start"))
                end = _normalise_hhmm_text(day_value.get("end"))
                if start and end:
                    return start, end
        except Exception:
            pass
    department_hours = (metadata.get("department_staff_hours", {}) or {}).get(
        category_key, {}
    ) or {}
    value = department_hours.get(department_id) or (
        metadata.get("category_staff_hours", {}) or {}
    ).get(category_key)
    if not value and category_key == "linen":
        value = {"start": "09:00", "end": "17:00"}
    if not isinstance(value, dict):
        return None
    start = _normalise_hhmm_text(value.get("start"))
    end = _normalise_hhmm_text(value.get("end"))
    return (start, end) if start and end else None


def _event_seconds_of_day(value, has_datetime: bool) -> Optional[float]:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if has_datetime:
        ts = pd.Timestamp(value)
        return float(ts.hour * 3600 + ts.minute * 60 + ts.second)
    return float(value) % 86400.0


def _within_staff_hours(
    start_value, end_value, hours: Tuple[str, str], has_datetime: bool
) -> bool:
    if any(str(value or "").strip().lower() == "closed" for value in hours):
        return False
    start_sec = _event_seconds_of_day(start_value, has_datetime)
    end_sec = _event_seconds_of_day(end_value, has_datetime)
    opening = _hhmm_to_seconds(hours[0])
    closing = _hhmm_to_seconds(hours[1])
    if start_sec is None or end_sec is None or opening is None or closing is None:
        return True
    if closing >= opening:
        return start_sec >= opening and end_sec <= closing and end_sec >= start_sec
    # Overnight staff window.
    in_start = start_sec >= opening or start_sec <= closing
    in_end = end_sec >= opening or end_sec <= closing
    return in_start and in_end


def _ward_collection_reason(
    start_value, end_value, hours: Tuple[str, str], has_datetime: bool
) -> str:
    if any(str(value or "").strip().lower() == "closed" for value in hours):
        return "No category staff are rostered on this day."
    start_sec = _event_seconds_of_day(start_value, has_datetime)
    end_sec = _event_seconds_of_day(end_value, has_datetime)
    opening = _hhmm_to_seconds(hours[0]) or 0
    closing = _hhmm_to_seconds(hours[1]) or 0
    if start_sec is not None and closing >= opening and start_sec < opening:
        return "Delivery is before the linen staff shift starts."
    if end_sec is not None and closing >= opening and end_sec > closing:
        return "Set-down and pack-away would finish after the linen staff shift ends."
    return "Delivery handling falls outside the fixed linen staff hours."


def _event_date_label(value, has_datetime: bool) -> str:
    if has_datetime and value is not None and not pd.isna(value):
        return pd.Timestamp(value).strftime("%d/%m/%Y")
    seconds = _to_float(value, 0.0)
    return f"Simulation day {int(seconds // 86400.0) + 1}"


def build_staff_hours_summary(metadata: Optional[dict]) -> pd.DataFrame:
    metadata = metadata or {}
    labels = metadata.get("category_labels", {}) or {}
    rows = []
    all_categories = set((metadata.get("category_staff_hours", {}) or {}).keys())
    all_categories.update((metadata.get("category_staff_weekly_hours", {}) or {}).keys())
    for category_key in sorted(all_categories):
        value = (metadata.get("category_staff_hours", {}) or {}).get(category_key, {})
        value = value if isinstance(value, dict) else {}
        start = _normalise_hhmm_text(value.get("start"))
        end = _normalise_hhmm_text(value.get("end"))
        weekly = (metadata.get("category_staff_weekly_hours", {}) or {}).get(
            category_key, {}
        )
        weekly_parts = []
        if isinstance(weekly, dict):
            labels_by_day = {
                "mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu",
                "fri": "Fri", "sat": "Sat", "sun": "Sun",
            }
            for day_key in labels_by_day:
                day_value = weekly.get(day_key, {})
                if isinstance(day_value, dict) and _to_bool(
                    day_value.get("enabled", False), False
                ):
                    weekly_parts.append(
                        f"{labels_by_day[day_key]} {day_value.get('start', '09:00')}-"
                        f"{day_value.get('end', '17:00')}"
                    )
        staff_hours_text = "; ".join(weekly_parts)
        if not staff_hours_text and start and end:
            staff_hours_text = f"{start}-{end}"
        if not staff_hours_text:
            continue
        rows.append(
            {
                "category": labels.get(category_key, str(category_key).title()),
                "category_key": str(category_key),
                "staff_start": start,
                "staff_end": end,
                "staff_hours": staff_hours_text,
            }
        )
    return pd.DataFrame(
        rows,
        columns=["category", "category_key", "staff_start", "staff_end", "staff_hours"],
    )


def build_payload_handling_timetable(
    df: pd.DataFrame,
    ctx: Context,
    metadata: Optional[dict] = None,
    ward_collection_rows: Optional[List[dict]] = None,
) -> pd.DataFrame:
    day_cols = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    columns = ["category", "category_key", "batch"] + day_cols
    if df is None or df.empty:
        return pd.DataFrame(columns=columns)

    df = df.copy()
    metadata = metadata or {}
    ward_collection_rows = ward_collection_rows if ward_collection_rows is not None else []
    labels = {
        str(k).strip().lower(): str(v).strip()
        for k, v in (metadata.get("category_labels", {}) or {}).items()
        if str(k).strip()
    }
    assisted_category_keys = _human_assist_category_keys(metadata)
    department_names = metadata.get("department_names", {}) or {}
    location_names = metadata.get("location_display_names", {}) or {}
    location_points = metadata.get("location_points", {}) or {}
    for col in (
        "details",
        "task_id",
        "task_source",
        "department_id",
        "payload",
        "to_location",
        "from_location",
    ):
        if col not in df.columns:
            df[col] = ""

    event_text = df.get("_event_text", pd.Series("", index=df.index)).astype(str)
    completed = df[
        event_text.str.fullmatch(
            r"task_complete|multi_stop_task_complete", case=False, na=False
        )
        & df["task_id"].notna()
    ].copy()
    returns = df[
        event_text.str.fullmatch(r"return_task_generated", case=False, na=False)
        & df["task_id"].notna()
    ].copy()
    return_times_by_source: Dict[str, List] = {}
    for _, return_row in returns.sort_values(ctx.time_col).iterrows():
        source_id = _return_source_task_id(str(return_row.get("task_id", "") or ""))
        if source_id:
            return_times_by_source.setdefault(source_id, []).append(
                return_row.get(ctx.time_col)
            )

    staff_rows = df[
        _staff_handling_event_mask(df) & df["task_id"].notna()
    ].copy()
    if not staff_rows.empty and "person_id" in staff_rows.columns:
        staff_rows["_staff_start_float"] = staff_rows[ctx.time_col].map(
            lambda value: event_time_to_float(value, ctx.has_datetime)
        )
        staff_rows = staff_rows.sort_values(ctx.time_col).drop_duplicates(
            ["task_id", "person_id", "_staff_start_float"], keep="first"
        )

    records: List[dict] = []
    explicit_task_ids = set()
    if not staff_rows.empty:
        for _, row in staff_rows.sort_values(ctx.time_col).iterrows():
            ready_start = row.get(ctx.time_col)
            if pd.isna(ready_start):
                continue
            category, category_key = _staff_category_from_row(row, labels)
            department_id = _clean_optional_text(row.get("department_id", ""))
            task_id = _clean_optional_text(row.get("task_id", ""))
            return_time = None
            for candidate in return_times_by_source.get(task_id, []):
                delta = time_delta_seconds(ready_start, candidate, ctx.has_datetime)
                if delta is not None and delta > 0.0:
                    return_time = candidate
                    break
            duration_s = _handling_duration_seconds(
                row,
                metadata,
                category_key,
                department_id,
                ready_start=ready_start,
                return_time=return_time,
            )
            ready_end = add_seconds_to_event_time(
                ready_start, duration_s, ctx.has_datetime
            )
            department = department_names.get(department_id, department_id or "-")
            # Human assistance occurs only at the final delivery destination.
            # Never substitute the task origin when a handling row has no
            # destination; doing so duplicates the pickup location in the
            # person's timetable.
            location_id = _clean_optional_text(row.get("to_location", ""))
            if not location_id:
                continue
            location = location_names.get(location_id, location_id)
            point = location_points.get(location_id, {}) or {}
            payload = safe_text(row.get("payload", "") or row.get("container_type", ""))
            person_id = _clean_optional_text(row.get("person_id", ""))
            shift_team = _staff_shift_team_from_row(row, person_id)
            person_number = _staff_person_number_from_row(row, person_id)
            day = _event_day_key(ready_start, ctx.has_datetime)
            if not day:
                continue
            window = (
                f"{_event_time_hhmm(ready_start, ctx.has_datetime)}-"
                f"{_event_time_hhmm(ready_end, ctx.has_datetime)}"
            )
            if task_id:
                explicit_task_ids.add(task_id)
            record = {
                "category": category,
                "category_key": category_key,
                "department": department,
                "location": location,
                "location_id": location_id,
                "floor": str(point.get("floor", "") or ""),
                "x": _to_float(point.get("x"), 0.0),
                "y": _to_float(point.get("y"), 0.0),
                "has_point": location_id in location_points,
                "day": day,
                "window": window,
                "_start_value": ready_start,
                "_end_value": ready_end,
                "duration_s": duration_s,
                "payload": payload,
                "person_id": person_id,
                "person": person_id,
                "person_number": person_number,
                "shift_team": shift_team,
                "task_id": task_id,
            }
            staff_hours = _record_staff_hours(metadata, category_key, department_id, ready_start, ctx.has_datetime)
            if (
                category_key == "linen"
                and staff_hours
                and not _within_staff_hours(
                    ready_start, ready_end, staff_hours, ctx.has_datetime
                )
            ):
                ward_collection_rows.append(
                    {
                        "date": _event_date_label(ready_start, ctx.has_datetime),
                        "day": day,
                        "delivery_time": _event_time_hhmm(ready_start, ctx.has_datetime),
                        "handling_finish": _event_time_hhmm(ready_end, ctx.has_datetime),
                        "department": department,
                        "location": location,
                        "payload": payload,
                        "task_id": task_id,
                        "linen_staff_hours": f"{staff_hours[0]}-{staff_hours[1]}",
                        "collection_by": "Ward staff",
                        "reason": _ward_collection_reason(
                            ready_start, ready_end, staff_hours, ctx.has_datetime
                        ),
                    }
                )
                continue
            records.append(record)

    # Supplement explicit staff rows with the legacy completed-to-return window
    # model on a per-task basis. This keeps older CSVs useful and, unlike the old
    # all-or-nothing fallback, does not omit a newly assisted category merely
    # because catering or stores already produced explicit staff rows.
    if not completed.empty and not returns.empty:
        completed = completed.sort_values(ctx.time_col).drop_duplicates(
            "task_id", keep="last"
        )
        completed_by_task = {
            str(row["task_id"]).strip(): row for _, row in completed.iterrows()
        }
        allow_unconfigured_legacy_fallback = not assisted_category_keys and not records

        for _, return_row in returns.sort_values(ctx.time_col).iterrows():
            source_id = _return_source_task_id(str(return_row.get("task_id", "") or ""))
            if (
                not source_id
                or source_id not in completed_by_task
                or source_id in explicit_task_ids
            ):
                continue

            complete_row = completed_by_task[source_id]
            category, category_key = _staff_category_from_row(complete_row, labels)
            if (
                category_key not in assisted_category_keys
                and not allow_unconfigured_legacy_fallback
            ):
                continue

            ready_start = complete_row.get(ctx.time_col)
            ready_end = return_row.get(ctx.time_col)
            if pd.isna(ready_start) or pd.isna(ready_end):
                continue

            department_id = _clean_optional_text(
                complete_row.get("department_id", "")
            ) or _clean_optional_text(return_row.get("department_id", ""))
            department = department_names.get(department_id, department_id or "-")
            location_id = _clean_optional_text(
                complete_row.get("to_location", "")
            ) or _clean_optional_text(return_row.get("from_location", ""))
            location = location_names.get(location_id, location_id)
            point = location_points.get(location_id, {}) or {}
            payload = safe_text(
                complete_row.get("payload", "") or return_row.get("payload", "")
            )
            day = _event_day_key(ready_start, ctx.has_datetime)
            if not day:
                continue

            duration_s = _handling_duration_seconds(
                complete_row,
                metadata,
                category_key,
                department_id,
                ready_start=ready_start,
                return_time=ready_end,
            )
            ready_end = add_seconds_to_event_time(
                ready_start, duration_s, ctx.has_datetime
            )
            window = (
                f"{_event_time_hhmm(ready_start, ctx.has_datetime)}-"
                f"{_event_time_hhmm(ready_end, ctx.has_datetime)}"
            )
            shift_source = complete_row if _clean_optional_text(complete_row.get("staff_shift_team", "")) else return_row
            shift_team = _staff_shift_team_from_row(shift_source, "")

            record = {
                "category": category,
                "category_key": category_key,
                "department": department,
                "location": location,
                "location_id": location_id,
                "floor": str(point.get("floor", "") or ""),
                "x": _to_float(point.get("x"), 0.0),
                "y": _to_float(point.get("y"), 0.0),
                "has_point": location_id in location_points,
                "day": day,
                "window": window,
                "_start_value": ready_start,
                "_end_value": ready_end,
                "duration_s": duration_s,
                "payload": payload,
                "person_id": "",
                "person": "",
                "person_number": None,
                "shift_team": shift_team,
                "task_id": source_id,
            }
            staff_hours = _record_staff_hours(metadata, category_key, department_id, ready_start, ctx.has_datetime)
            if (
                category_key == "linen"
                and staff_hours
                and not _within_staff_hours(
                    ready_start, ready_end, staff_hours, ctx.has_datetime
                )
            ):
                ward_collection_rows.append(
                    {
                        "date": _event_date_label(ready_start, ctx.has_datetime),
                        "day": day,
                        "delivery_time": _event_time_hhmm(ready_start, ctx.has_datetime),
                        "handling_finish": _event_time_hhmm(ready_end, ctx.has_datetime),
                        "department": department,
                        "location": location,
                        "payload": payload,
                        "task_id": source_id,
                        "linen_staff_hours": f"{staff_hours[0]}-{staff_hours[1]}",
                        "collection_by": "Ward staff",
                        "reason": _ward_collection_reason(
                            ready_start, ready_end, staff_hours, ctx.has_datetime
                        ),
                    }
                )
                continue
            records.append(record)

    if not records:
        return pd.DataFrame(columns=columns)

    synthetic_available: Dict[Tuple[str, str, str], List[float]] = {}
    unnamed_explicit_people: Dict[Tuple[str, str, str], int] = {}
    next_explicit_number: Dict[Tuple[str, str], int] = {}
    for record in sorted(
        records,
        key=lambda item: (
            natural_key(item.get("category", "")),
            natural_key(item.get("person", "")),
            event_time_to_float(item.get("_start_value"), ctx.has_datetime),
            event_time_to_float(item.get("_end_value"), ctx.has_datetime),
            natural_key(item.get("location", "")),
            natural_key(item.get("task_id", "")),
        ),
    ):
        category = str(record.get("category", "") or "Staff")
        category_key = str(record.get("category_key", "") or category)
        shift_team = str(record.get("shift_team", "") or "").strip().upper()

        if record.get("person"):
            person_number = record.get("person_number")
            if person_number is None:
                raw_person_id = str(record.get("person", "") or "").strip()
                person_key = (category_key, shift_team, raw_person_id)
                if person_key not in unnamed_explicit_people:
                    number_key = (category_key, shift_team)
                    next_number = next_explicit_number.get(number_key, 1)
                    unnamed_explicit_people[person_key] = next_number
                    next_explicit_number[number_key] = next_number + 1
                person_number = unnamed_explicit_people[person_key]
            else:
                number_key = (category_key, shift_team)
                next_explicit_number[number_key] = max(
                    next_explicit_number.get(number_key, 1), int(person_number) + 1
                )
            record["batch"] = _friendly_staff_name(
                category, shift_team, int(person_number)
            )
            continue

        key = (category, category_key, shift_team)
        start_value = event_time_to_float(record.get("_start_value"), ctx.has_datetime)
        end_value = event_time_to_float(record.get("_end_value"), ctx.has_datetime)
        lanes = synthetic_available.setdefault(key, [])
        lane_index = None
        for idx, available_time in enumerate(lanes):
            if available_time <= start_value + 1e-9:
                lane_index = idx
                break
        if lane_index is None:
            lanes.append(float("-inf"))
            lane_index = len(lanes) - 1
        lanes[lane_index] = max(end_value, start_value)
        record["batch"] = _friendly_staff_name(
            category, shift_team, lane_index + 1
        )

    cells: Dict[Tuple[str, str, str], Dict[str, List[dict]]] = {}
    for record in records:
        key = (record["category"], record["category_key"], record["batch"])
        cells.setdefault(key, {day_col: [] for day_col in day_cols})[
            record["day"]
        ].append(record)

    rows = []
    for category, category_key, batch in sorted(
        cells,
        key=lambda item: (natural_key(item[0]), natural_key(item[2])),
    ):
        row = {"category": category, "category_key": category_key, "batch": batch}
        for day in day_cols:
            day_records = sorted(
                cells[(category, category_key, batch)].get(day, []),
                key=lambda item: (
                    event_time_to_float(item.get("_start_value"), ctx.has_datetime),
                    event_time_to_float(item.get("_end_value"), ctx.has_datetime),
                    natural_key(item.get("location", "")),
                    natural_key(item.get("task_id", "")),
                ),
            )
            entries = []
            for record in day_records:
                label = record.get("location") or "Location"
                entries.append(f"{record['window']}|{label}")
            row[day] = "\n".join(entries) if entries else "-"
        rows.append(row)
    return pd.DataFrame(rows, columns=columns)


def build_staff_handling_summary(
    df: pd.DataFrame,
    ctx: Context,
    metadata: Optional[dict] = None,
) -> pd.DataFrame:
    columns = [
        "category",
        "handling_tasks",
        "people_required",
        "initial_people",
        "added_people",
        "total_handling_time_s",
    ]
    if df is None or df.empty:
        return pd.DataFrame(columns=columns)

    metadata = metadata or {}
    labels = {
        str(k).strip().lower(): str(v).strip()
        for k, v in (metadata.get("category_labels", {}) or {}).items()
        if str(k).strip()
    }
    configured_initial = {
        str(k).strip().lower(): int(v or 1)
        for k, v in (metadata.get("category_staff_initial", {}) or {}).items()
        if str(k).strip()
    }
    configured_shift_multiplier = {
        str(k).strip().lower(): max(1.0, float(v or 1.0))
        for k, v in (metadata.get("category_staff_shift_multiplier", {}) or {}).items()
        if str(k).strip()
    }
    staff_rows = df[_staff_handling_event_mask(df)].copy()
    if staff_rows.empty:
        return pd.DataFrame(columns=columns)
    if "person_id" in staff_rows.columns:
        staff_rows["_staff_start_float"] = staff_rows[ctx.time_col].map(
            lambda value: event_time_to_float(value, ctx.has_datetime)
        )
        staff_rows = staff_rows.sort_values(ctx.time_col).drop_duplicates(
            ["task_id", "person_id", "_staff_start_float"], keep="first"
        )

    category_pairs = staff_rows.apply(
        lambda row: _staff_category_from_row(row, labels),
        axis=1,
    )
    staff_rows["_staff_category"] = [pair[0] for pair in category_pairs]
    staff_rows["_staff_category_key"] = [pair[1] for pair in category_pairs]
    staff_rows["_report_handling_duration_s"] = staff_rows.apply(
        lambda row: _handling_duration_seconds(
            row,
            metadata,
            row.get("_staff_category_key", ""),
            _clean_optional_text(row.get("department_id", "")),
            ready_start=row.get(ctx.time_col),
        ),
        axis=1,
    )

    def include_staff_row(row: pd.Series) -> bool:
        category_key = str(row.get("_staff_category_key", "") or "").strip().lower()
        if category_key != "linen":
            return True
        department_id = _clean_optional_text(row.get("department_id", ""))
        start_value = row.get(ctx.time_col)
        hours = _record_staff_hours(
            metadata, category_key, department_id, start_value, ctx.has_datetime
        )
        if not hours:
            return True
        end_value = add_seconds_to_event_time(
            start_value,
            _to_float(row.get("_report_handling_duration_s"), 0.0),
            ctx.has_datetime,
        )
        return _within_staff_hours(start_value, end_value, hours, ctx.has_datetime)

    staff_rows = staff_rows[staff_rows.apply(include_staff_row, axis=1)].copy()
    if staff_rows.empty:
        return pd.DataFrame(columns=columns)

    rows = []
    for category_key, group in staff_rows.groupby("_staff_category_key", sort=True):
        category_key = str(category_key or "staff").strip().lower()
        category_values = [
            str(x).strip()
            for x in group["_staff_category"].dropna().tolist()
            if str(x).strip()
        ]
        category = category_values[0] if category_values else labels.get(
            category_key, category_key.replace("_", " ").title()
        )
        people_col = (
            "people_required"
            if "people_required" in group.columns
            else "staff_rostered_people_required"
        )
        people_values = (
            group[people_col]
            if people_col in group.columns
            else pd.Series(0, index=group.index, dtype=float)
        )
        people_required = int(
            pd.to_numeric(people_values, errors="coerce").fillna(0).max() or 0
        )
        on_shift_people_required = 0
        if "staff_on_shift_people_required" in group.columns:
            on_shift_people_required = int(
                pd.to_numeric(
                    group.get("staff_on_shift_people_required", 0),
                    errors="coerce",
                )
                .fillna(0)
                .max()
                or 0
            )
        person_ids = {
            str(x).strip()
            for x in group.get("person_id", pd.Series(dtype=str)).fillna("").tolist()
            if str(x).strip()
        }
        if people_required <= 0:
            people_required = on_shift_people_required or len(person_ids)
        shift_multiplier = configured_shift_multiplier.get(category_key, 1.0)
        if "staff_shift_multiplier" in group.columns:
            shift_multiplier = max(
                shift_multiplier,
                float(
                    pd.to_numeric(
                        group.get("staff_shift_multiplier", 1.0), errors="coerce"
                    )
                    .fillna(1.0)
                    .max()
                    or 1.0
                ),
            )
        if (
            "staff_on_shift_people_required" in group.columns
            and shift_multiplier > 1.0
        ):
            people_required = max(
                people_required,
                int(
                    math.ceil(
                        (on_shift_people_required or len(person_ids))
                        * shift_multiplier
                    )
                ),
            )
        initial_people = max(1, int(configured_initial.get(category_key, 1) or 1))
        if "staff_initial_rostered_people" in group.columns:
            initial_people = max(
                initial_people,
                int(
                    pd.to_numeric(
                        group.get("staff_initial_rostered_people", 0),
                        errors="coerce",
                    )
                    .fillna(0)
                    .max()
                    or 0
                ),
            )
        elif shift_multiplier > 1.0:
            initial_people = int(math.ceil(initial_people * shift_multiplier))
        duration = pd.to_numeric(
            group.get("_report_handling_duration_s", 0.0),
            errors="coerce",
        ).fillna(0.0)
        rows.append(
            {
                "category": category,
                "handling_tasks": int(len(group)),
                "people_required": people_required,
                "initial_people": initial_people,
                "added_people": max(0, people_required - initial_people),
                "total_handling_time_s": float(duration.sum()),
            }
        )

    return pd.DataFrame(rows, columns=columns).sort_values(
        "category", key=lambda col: col.map(natural_key)
    )


def analyse(
    csv_path: Path,
    target_amr_util: float,
    target_lift_util: float,
    payload_weights: Optional[Dict[str, float]] = None,
    amr_parameters: Optional[pd.DataFrame] = None,
    floor_dxf_map: Optional[Dict[int, str]] = None,
    location_catalog: Optional[pd.DataFrame] = None,
    payload_dimensions: Optional[pd.DataFrame] = None,
    task_generation_metadata: Optional[dict] = None,
) -> Dict[str, pd.DataFrame]:
    payload_weights = payload_weights or {}
    raw = pd.read_csv(csv_path, low_memory=False)
    df, ctx = parse_time_column(raw)
    df = df.sort_values(ctx.time_col).reset_index(drop=True)
    cols = ctx.cols

    amr_col = cols["amr"]
    task_col = cols["task"]
    if not amr_col or not task_col:
        raise ValueError("CSV must contain AMR and task identifiers.")

    event_col = cols["event"]
    seg_col = cols["segment_type"]
    duration_col = cols["duration"]
    wait_col = cols["wait"]
    from_col = cols["from"]
    to_col = cols["to"]
    outcome_col = cols["outcome"]
    reason_col = cols.get("reason")

    payload_col = cols["payload"]
    distance_col = cols["distance"]
    energy_col = cols["energy"]
    resource_kind_col = "delivery_resource_kind" if "delivery_resource_kind" in df.columns else None
    resource_id_col = "delivery_resource_id" if "delivery_resource_id" in df.columns else None
    resource_type_col = "delivery_resource_type" if "delivery_resource_type" in df.columns else None

    df["_event_text"] = df[event_col].astype(str) if event_col else ""
    df["_segment_text"] = df[seg_col].astype(str) if seg_col else ""
    df["_duration_s"] = (
        pd.to_numeric(df[duration_col], errors="coerce") if duration_col else pd.NA
    )
    df["_distance_m"] = (
        pd.to_numeric(df[distance_col], errors="coerce") if distance_col else 0.0
    )
    df["_energy_kwh"] = (
        pd.to_numeric(df[energy_col], errors="coerce").fillna(0.0)
        if energy_col
        else 0.0
    )
    df["_wait_s"] = (
        pd.to_numeric(df[wait_col], errors="coerce").fillna(0) if wait_col else 0.0
    )
    task_generation_summary = build_generated_task_category_summary(
        df, ctx, task_generation_metadata
    )
    ward_collection_rows: List[dict] = []
    payload_handling_timetable = build_payload_handling_timetable(
        df, ctx, task_generation_metadata, ward_collection_rows
    )
    linen_ward_collection = pd.DataFrame(
        ward_collection_rows,
        columns=[
            "date",
            "day",
            "delivery_time",
            "handling_finish",
            "department",
            "location",
            "payload",
            "task_id",
            "linen_staff_hours",
            "collection_by",
            "reason",
        ],
    )
    if not linen_ward_collection.empty:
        linen_ward_collection = linen_ward_collection.sort_values(
            ["date", "delivery_time", "location", "task_id"]
        ).reset_index(drop=True)
    staff_hours_summary = build_staff_hours_summary(task_generation_metadata)
    staff_handling_summary = build_staff_handling_summary(
        df, ctx, task_generation_metadata
    )

    if not wait_col and duration_col:
        wait_mask = df["_event_text"].str.contains(WAIT_PATTERNS, na=False)
        df.loc[wait_mask, "_wait_s"] = df.loc[wait_mask, "_duration_s"].fillna(0)

    staff_handling_mask = _staff_handling_event_mask(df)
    if staff_handling_mask.any():
        # Staff handling has its own resource timeline. Older simulator logs
        # stored staff delay in the generic AMR wait column, which made task and
        # fleet waiting metrics look wildly inflated.
        df.loc[staff_handling_mask, "_wait_s"] = 0.0

    t0 = df[ctx.time_col].dropna().min()
    t1 = df[ctx.time_col].dropna().max()
    horizon_s = max(time_delta_seconds(t0, t1, ctx.has_datetime) or 0.0, 1.0)

    multi_stop_task_paths = build_multi_stop_task_paths(
        df, ctx, amr_col, task_col, from_col, to_col
    )
    multi_stop_task_leg_overrides = build_multi_stop_task_leg_overrides(
        df, ctx, amr_col, task_col, from_col, to_col
    )

    task_rows: List[dict] = []
    raw_task_ids = [str(x).strip() for x in df[task_col].dropna().tolist() if str(x).strip()]
    single_task_ids = {task_id for task_id in raw_task_ids if "," not in task_id}

    for task_id, g in df[df[task_col].notna()].groupby(task_col, sort=False):
        task_id_text = str(task_id).strip()
        if "," in task_id_text:
            component_ids = [x.strip() for x in task_id_text.split(",") if x.strip()]
            # Grouped multi-pickup segment rows are visualiser state rows, not
            # extra tasks.  When their component task IDs also exist as normal
            # assignment/completion rows, skip the grouped pseudo-task so reports
            # do not count the same payloads multiple times.
            if component_ids and all(x in single_task_ids for x in component_ids):
                continue
        g = g.sort_values(ctx.time_col)
        first = g.iloc[0]
        last = g.iloc[-1]
        start_row = g[g["_event_text"].str.contains(ASSIGN_PATTERNS, na=False)].head(1)
        end_row = g[g["_event_text"].str.contains(COMPLETE_PATTERNS, na=False)].tail(1)
        fail_row = g[g["_event_text"].str.contains(FAIL_PATTERNS, na=False)].tail(1)
        start = (
            start_row.iloc[0][ctx.time_col]
            if not start_row.empty
            else first[ctx.time_col]
        )
        end = last[ctx.time_col]
        outcome = "incomplete"
        if not fail_row.empty:
            end = fail_row.iloc[-1][ctx.time_col]
            outcome = "failed"
        if not end_row.empty:
            end = end_row.iloc[-1][ctx.time_col]
            outcome = "completed"
        if outcome_col and g[outcome_col].notna().any():
            last_outcome = str(g[outcome_col].dropna().iloc[-1]).strip().lower()
            if COMPLETE_PATTERNS.search(last_outcome):
                outcome = "completed"
            elif FAIL_PATTERNS.search(last_outcome):
                outcome = "failed"
        duration_s = time_delta_seconds(start, end, ctx.has_datetime)
        wait_s = float(pd.to_numeric(g["_wait_s"], errors="coerce").fillna(0).sum())
        amr = g[amr_col].dropna().iloc[0] if g[amr_col].notna().any() else "-"
        resource_kind = ""
        if resource_kind_col:
            values = [str(v).strip().lower() for v in g[resource_kind_col].dropna() if str(v).strip()]
            resource_kind = values[0] if values else ""
        if not resource_kind:
            resource_kind = "porter" if g["_event_text"].str.startswith("staff_").any() else (
                "amr" if is_physical_amr_id(amr) else "unassigned"
            )
        resource_id = safe_text(amr)
        if resource_id_col:
            values = [str(v).strip() for v in g[resource_id_col].dropna() if str(v).strip()]
            if values:
                resource_id = values[0]
        resource_type = "-"
        if resource_type_col:
            values = [str(v).strip() for v in g[resource_type_col].dropna() if str(v).strip()]
            if values:
                resource_type = values[0]
        origin = choose_task_endpoint(g, ctx, from_col, to_col, "from")
        destination = choose_task_endpoint(g, ctx, from_col, to_col, "to")
        multi_stop_leg_override = multi_stop_task_leg_overrides.get(str(task_id))
        if multi_stop_leg_override:
            origin = multi_stop_leg_override.get("origin", origin)
            destination = multi_stop_leg_override.get("destination", destination)
            start = multi_stop_leg_override.get("start", start)
            end = multi_stop_leg_override.get("finish", end)
            duration_s = multi_stop_leg_override.get("duration_s", duration_s)
            wait_s = multi_stop_leg_override.get("wait_s", wait_s)

        payload = (
            primary_payload_name(g[payload_col].dropna().iloc[0])
            if payload_col and g[payload_col].notna().any()
            else "-"
        )
        distance_m = float(
            pd.to_numeric(g["_distance_m"], errors="coerce").fillna(0).sum()
        )
        payload_weight_kg = float(payload_weights.get(str(payload), 0.0))
        failure_reason = "-"
        pending_reason = "-"
        if outcome == "failed":
            reason_values: List[str] = []
            if reason_col and reason_col in g.columns:
                reason_values.extend(
                    [
                        str(v).strip()
                        for v in g[reason_col].dropna().tolist()
                        if str(v).strip()
                    ]
                )
            event_reasons = [
                str(v).strip()
                for v in g["_event_text"].dropna().tolist()
                if FAIL_PATTERNS.search(str(v))
            ]
            if reason_values:
                failure_reason = reason_values[-1]
            elif event_reasons:
                failure_reason = event_reasons[-1]
            else:
                failure_reason = (
                    "Task failed; no detailed reason was present in the CSV event log."
                )
        elif outcome == "incomplete":
            reason_values = []
            if "pending_reason" in g.columns:
                reason_values.extend(
                    [
                        str(v).strip()
                        for v in g["pending_reason"].dropna().tolist()
                        if str(v).strip()
                    ]
                )
            if reason_col and reason_col in g.columns:
                reason_values.extend(
                    [
                        str(v).strip()
                        for v in g[reason_col].dropna().tolist()
                        if str(v).strip()
                    ]
                )
            wait_reasons = [
                str(v).strip()
                for v in g.get("details", pd.Series(dtype=str)).dropna().tolist()
                if str(v).strip()
                and WAIT_PATTERNS.search(str(v))
            ]
            if reason_values:
                pending_reason = reason_values[-1]
            elif wait_reasons:
                pending_reason = wait_reasons[-1]
            elif start_row.empty:
                pending_reason = "Generated but not assigned before the simulation ended."
            else:
                pending_reason = "Assigned but not completed before the simulation ended."
        task_rows.append(
            {
                "amr": safe_text(amr),
                "resource_kind": resource_kind,
                "resource_id": resource_id,
                "resource_type": resource_type,
                "task_id": safe_text(task_id),
                "outcome": outcome,
                "failure_reason": failure_reason,
                "pending_reason": pending_reason,
                "start": start,
                "finish": end,
                "duration_s": duration_s,
                "wait_s": wait_s,
                "origin": safe_text(origin),
                "destination": safe_text(destination),
                "route_path": multi_stop_task_paths.get(
                    str(task_id),
                    f"{safe_text(origin)} → {safe_text(destination)}",
                ),
                "payload": safe_text(payload),
                "distance_m": distance_m,
                "payload_weight_kg": payload_weight_kg,
            }
        )
        # Do not add per-task active intervals here.  A multi-stop route can
        # complete several tasks at the same time, so task intervals would count
        # the same AMR route once per payload slot.  Resource recommendations are
        # derived later from actual AMR segment busy intervals.

    # How many tasks did each AMR complete

    tasks = pd.DataFrame(task_rows)
    completed = tasks[tasks["outcome"] == "completed"].copy()
    failed = tasks[tasks["outcome"] == "failed"].copy()
    pending_tasks = tasks[tasks["outcome"] == "incomplete"].copy()
    if pending_tasks.empty:
        pending_tasks = pd.DataFrame(
            columns=[
                "task_id",
                "amr",
                "origin",
                "destination",
                "payload",
                "start",
                "finish",
                "pending_reason",
            ]
        )
    else:
        pending_tasks = pending_tasks[
            [
                "task_id",
                "amr",
                "origin",
                "destination",
                "payload",
                "start",
                "finish",
                "pending_reason",
            ]
        ].sort_values(["start", "task_id"], key=lambda col: col.map(natural_key) if col.name == "task_id" else col)

    # Fleet statistics must use physical AMRs only. Pending generated tasks,
    # staff-handling rows and other task records without an AMR are represented
    # by the placeholder ``-`` and previously inflated the observed fleet by one.
    physical_amr_task_mask = tasks["resource_kind"].eq("amr")
    fleet_tasks = tasks.loc[physical_amr_task_mask].copy()
    tasks_without_delivery_resource = int(tasks["resource_kind"].eq("unassigned").sum())

    amr_busy_intervals_by_amr, amr_route_summary = build_amr_busy_intervals(
        df, ctx, amr_col
    )
    amr_busy_intervals_by_amr = {
        amr: intervals
        for amr, intervals in amr_busy_intervals_by_amr.items()
        if is_physical_amr_id(amr)
    }
    if not amr_route_summary.empty and "amr" in amr_route_summary.columns:
        amr_route_summary = amr_route_summary[
            amr_route_summary["amr"].map(is_physical_amr_id)
        ].reset_index(drop=True)
    active_intervals = [
        interval
        for intervals in amr_busy_intervals_by_amr.values()
        for interval in intervals
    ]
    amr_busy_time_by_amr = {
        amr: interval_total(intervals)
        for amr, intervals in amr_busy_intervals_by_amr.items()
    }
    amr_route_count_by_amr = {
        amr: len(intervals) for amr, intervals in amr_busy_intervals_by_amr.items()
    }

    amr_summary = (
        fleet_tasks.groupby("amr", dropna=False)
        .agg(
            tasks_total=("task_id", "count"),
            tasks_completed=("outcome", lambda s: int((s == "completed").sum())),
            tasks_failed=("outcome", lambda s: int((s == "failed").sum())),
            total_task_time_s=("duration_s", "sum"),
            total_wait_s=("wait_s", "sum"),
            avg_task_time_s=("duration_s", "mean"),
            total_distance_km=("distance_m", "sum"),
        )
        .reset_index()
    )
    # Replace duplicated task-duration workload with actual AMR route busy time.
    # For multi-slot AMRs, several completed payload tasks can share one route;
    # summing task durations would overstate AMR demand and fleet size.
    amr_summary["routes"] = amr_summary["amr"].map(amr_route_count_by_amr).fillna(0).astype(int)
    amr_summary["total_route_time_s"] = amr_summary["amr"].map(amr_busy_time_by_amr).fillna(0.0)
    amr_summary["total_task_time_s"] = amr_summary["total_route_time_s"]

    amr_summary["total_distance_km"] = (amr_summary["total_distance_km"] / 1000).round(
        2
    )

    # Utilisation

    amr_utilisation = (
        fleet_tasks.groupby("amr", dropna=False)
        .agg(
            tasks_total=("task_id", "count"),
            total_task_time_s=("duration_s", "sum"),
            total_wait_s=("wait_s", "sum"),
        )
        .reset_index()
    )
    amr_utilisation["routes"] = amr_utilisation["amr"].map(amr_route_count_by_amr).fillna(0).astype(int)
    amr_utilisation["total_route_time_s"] = amr_utilisation["amr"].map(amr_busy_time_by_amr).fillna(0.0)
    amr_utilisation["total_task_time_s"] = amr_utilisation["total_route_time_s"]

    amr_utilisation["utilisation_pct"] = (
        amr_utilisation["total_task_time_s"] / horizon_s * 100
    ).round(1)
    amr_utilisation["idle_pct"] = (
        (100 - amr_utilisation["utilisation_pct"]).clip(lower=0).round(1)
    )
    amr_utilisation["wait_share_pct"] = (
        (
            amr_utilisation["total_wait_s"]
            / amr_utilisation["total_task_time_s"].replace(0, pd.NA)
            * 100
        )
        .fillna(0)
        .round(1)
    )

    porter_tasks = tasks[tasks["resource_kind"].eq("porter")].copy()
    porter_summary = (
        porter_tasks.groupby(["resource_id", "resource_type"], dropna=False)
        .agg(
            tasks_total=("task_id", "count"),
            tasks_completed=("outcome", lambda s: int((s == "completed").sum())),
            tasks_failed=("outcome", lambda s: int((s == "failed").sum())),
            total_delivery_time_s=("duration_s", "sum"),
            total_wait_s=("wait_s", "sum"),
            avg_task_time_s=("duration_s", "mean"),
            total_distance_m=("distance_m", "sum"),
        ).reset_index()
    )
    if not porter_summary.empty:
        porter_summary["utilisation_pct"] = (
            porter_summary["total_delivery_time_s"] / horizon_s * 100
        ).round(1)

    allocation = (
        tasks[tasks["resource_kind"].isin(["amr", "porter"])]
        .groupby("resource_kind")
        .agg(
            tasks=("task_id", "count"),
            completed=("outcome", lambda s: int((s == "completed").sum())),
            failed=("outcome", lambda s: int((s == "failed").sum())),
            pending=("outcome", lambda s: int((s == "incomplete").sum())),
        ).reset_index()
    )
    if not allocation.empty:
        allocation["allocation_pct"] = (allocation["tasks"] / allocation["tasks"].sum() * 100).round(1)

    operating_model_key = "amr_only"
    if "operating_model" in df.columns:
        model_values = [str(v).strip().lower() for v in df["operating_model"].dropna() if str(v).strip()]
        if model_values:
            operating_model_key = model_values[0]
    operating_model_labels = {
        "amr_only": "AMR only",
        "porter_only": "Porter only",
        "hybrid_amr_preference": "Hybrid (AMR preference)",
        "hybrid_porter_preference": "Hybrid (Porter preference)",
    }
    operating_model = pd.DataFrame([{
        "key": operating_model_key,
        "label": operating_model_labels.get(operating_model_key, operating_model_key.replace("_", " ").title()),
    }])

    # How many recharges did the AMR undergo - battery wear

    # The simulator records a charging interval as:
    #
    #     event_type   = "segment_charge"
    #     segment_type = "charge"
    #
    # Older report logic searched segment_type for "segment_charge", which
    # excluded valid recharge rows. Accept both the event and segment forms so
    # reports remain compatible with current and older simulator CSV files.
    charge_mask = (
        df["_event_text"].astype(str).str.fullmatch(
            r"segment_charge|charge_cycle_start",
            case=False,
            na=False,
        )
        | df["_segment_text"].astype(str).str.fullmatch(
            r"charge|segment_charge",
            case=False,
            na=False,
        )
    )

    charge_rows = df.loc[charge_mask].copy()

    # Remove rows that cannot identify an AMR. This also prevents blank summary
    # rows when legacy CSV files contain generic charging status events.
    charge_rows = charge_rows[
        charge_rows[amr_col].map(is_physical_amr_id)
    ].copy()

    def _amr_parameter_value(
        runtime_amr_id: str,
        column_name: str,
        default: float = 0.0,
    ) -> float:
        """Return an AMR-type parameter for a runtime AMR instance.

        Runtime AMRs are normally named AMR-TYPE-1, AMR-TYPE-2, etc., while
        the configuration table contains the base AMR type, such as AMR-TYPE.
        Prefer an exact match, then use the longest matching type prefix.
        """
        if amr_parameters is None or amr_parameters.empty:
            return float(default)

        if "amr" not in amr_parameters.columns:
            return float(default)

        if column_name not in amr_parameters.columns:
            return float(default)

        runtime_id = str(runtime_amr_id or "").strip()
        if not runtime_id:
            return float(default)

        parameters = amr_parameters.copy()
        parameters["_report_amr_type"] = (
            parameters["amr"].fillna("").astype(str).str.strip()
        )

        exact = parameters[
            parameters["_report_amr_type"].str.casefold()
            == runtime_id.casefold()
        ]

        if not exact.empty:
            value = pd.to_numeric(
                exact.iloc[0][column_name],
                errors="coerce",
            )
            return float(value) if pd.notna(value) else float(default)

        candidates = []
        runtime_folded = runtime_id.casefold()

        for _, parameter_row in parameters.iterrows():
            amr_type = str(
                parameter_row.get("_report_amr_type", "") or ""
            ).strip()

            if not amr_type:
                continue

            amr_type_folded = amr_type.casefold()

            if (
                runtime_folded.startswith(amr_type_folded + "-")
                or runtime_folded.startswith(amr_type_folded + "_")
                or runtime_folded.startswith(amr_type_folded + " ")
            ):
                candidates.append((len(amr_type), parameter_row))

        if not candidates:
            return float(default)

        # Longest prefix wins where AMR type names overlap.
        _, parameter_row = max(candidates, key=lambda item: item[0])

        value = pd.to_numeric(
            parameter_row.get(column_name),
            errors="coerce",
        )

        return float(value) if pd.notna(value) else float(default)

    if charge_rows.empty:
        recharge_summary = pd.DataFrame(
            columns=[
                "amr",
                "recharges",
                "recharge_energy_kwh",
                "recharge_time_s",
            ]
        )
    else:
        charge_rows["_charge_duration_s"] = pd.to_numeric(
            charge_rows["_duration_s"],
            errors="coerce",
        ).fillna(0.0).clip(lower=0.0)

        charge_rows["_logged_recharge_energy_kwh"] = pd.to_numeric(
            charge_rows["_energy_kwh"],
            errors="coerce",
        ).fillna(0.0).clip(lower=0.0)

        charge_rows["_charge_rate_kw"] = charge_rows[amr_col].map(
            lambda amr_id: _amr_parameter_value(
                amr_id,
                "battery_charge_rate_kw",
                0.0,
            )
        )

        # Prefer energy explicitly written by the simulator. For existing CSV
        # files, where segment_charge energy was recorded as zero, derive the
        # recharge energy from charge rate multiplied by charging duration.
        charge_rows["_derived_recharge_energy_kwh"] = (
            charge_rows["_charge_rate_kw"]
            * charge_rows["_charge_duration_s"]
            / 3600.0
        )

        charge_rows["_recharge_energy_kwh"] = charge_rows[
            "_logged_recharge_energy_kwh"
        ].where(
            charge_rows["_logged_recharge_energy_kwh"] > 0.0,
            charge_rows["_derived_recharge_energy_kwh"],
        )

        recharge_summary = (
            charge_rows.groupby(amr_col, dropna=False)
            .agg(
                recharges=("_event_text", "size"),
                recharge_energy_kwh=("_recharge_energy_kwh", "sum"),
                recharge_time_s=("_charge_duration_s", "sum"),
            )
            .reset_index()
            .rename(columns={amr_col: "amr"})
        )

        recharge_summary["recharges"] = (
            pd.to_numeric(
                recharge_summary["recharges"],
                errors="coerce",
            )
            .fillna(0)
            .astype(int)
        )

        recharge_summary["recharge_energy_kwh"] = (
            pd.to_numeric(
                recharge_summary["recharge_energy_kwh"],
                errors="coerce",
            )
            .fillna(0.0)
            .round(3)
        )

        recharge_summary["recharge_time_s"] = (
            pd.to_numeric(
                recharge_summary["recharge_time_s"],
                errors="coerce",
            )
            .fillna(0.0)
        )

    recharge_energy = recharge_summary[
        ["amr", "recharge_energy_kwh"]
    ].copy()

    recharge_counts = recharge_summary[
        ["amr", "recharges"]
    ].copy()

    amr_summary = amr_summary.merge(
        recharge_energy,
        on="amr",
        how="left",
    )

    amr_summary["recharge_energy_kwh"] = (
        pd.to_numeric(
            amr_summary["recharge_energy_kwh"],
            errors="coerce",
        )
        .fillna(0.0)
        .round(3)
    )

    amr_summary = amr_summary.merge(
        recharge_counts,
        on="amr",
        how="left",
    )

    amr_summary["recharges"] = (
        pd.to_numeric(
            amr_summary["recharges"],
            errors="coerce",
        )
        .fillna(0)
        .astype(int)
    )


    df = derive_lift_columns(df, cols)
    lift_mask = df["_segment_text"].str.fullmatch(
        r"lift_transfer|lift_reposition", case=False, na=False
    )
    lift_rows = df.loc[lift_mask].copy()
    lift_rows = lift_rows[lift_rows["_lift_id"].notna()].copy()
    lift_rows["lift_time_s"] = pd.to_numeric(lift_rows["_duration_s"], errors="coerce")
    lift_rows = lift_rows[
        lift_rows["lift_time_s"].notna() & (lift_rows["lift_time_s"] >= 0)
    ].copy()
    lift_usage_profile = build_lift_usage_profile(lift_rows, ctx, interval_minutes=30)
    lift_busy_intervals_by_lift, lift_busy_summary = build_lift_busy_intervals(
        lift_rows, ctx
    )
    lift_busy_time_by_lift = {
        lift: interval_total(intervals)
        for lift, intervals in lift_busy_intervals_by_lift.items()
    }
    lift_trip_count_by_lift = (
        lift_rows.groupby("_lift_id", dropna=False)
        .size()
        .rename(index=safe_text)
        .to_dict()
        if not lift_rows.empty
        else {}
    )

    if lift_rows.empty:
        lift_summary = pd.DataFrame(
            columns=[
                "lift_id",
                "trips",
                "total_lift_time_s",
                "avg_trip_s",
                "utilisation_pct",
                "idle_pct",
                "lift_energy_kwh",
            ]
        )
    else:
        lift_summary = (
            lift_rows.groupby("_lift_id", dropna=False)
            .agg(
                trips=("lift_time_s", "count"),
                total_lift_time_s=("lift_time_s", "sum"),
                avg_trip_s=("lift_time_s", "mean"),
                lift_energy_kwh=("_energy_kwh", "sum"),
            )
            .reset_index()
            .rename(columns={"_lift_id": "lift_id"})
        )
        lift_summary["lift_id"] = lift_summary["lift_id"].map(safe_text)
        lift_summary["trips"] = (
            lift_summary["lift_id"].map(lift_trip_count_by_lift).fillna(0).astype(int)
        )
        lift_summary["total_lift_time_s"] = (
            lift_summary["lift_id"].map(lift_busy_time_by_lift).fillna(0.0)
        )
        lift_summary["utilisation_pct"] = (
            lift_summary["total_lift_time_s"] / horizon_s * 100
        ).round(1)
        lift_summary["idle_pct"] = (
            (100 - lift_summary["utilisation_pct"]).clip(lower=0).round(1)
        )
        lift_summary["lift_energy_kwh"] = lift_summary["lift_energy_kwh"].round(4)

    # Lift Wait times

    lift_wait_rows = df[df.apply(is_lift_wait_row, axis=1)].copy()
    lift_wait_rows = lift_wait_rows[lift_wait_rows["_lift_id"].notna()].copy()
    lift_wait_rows["lift_wait_s"] = pd.to_numeric(
        lift_wait_rows["_wait_s"], errors="coerce"
    ).fillna(0)

    lift_wait_rows = lift_wait_rows[lift_wait_rows["lift_wait_s"] > 0].copy()

    if lift_wait_rows.empty:
        lift_wait_schedule = pd.DataFrame(
            columns=["time", "amr", "task_id", "lift_id", "from", "to", "wait_s"]
        )
    else:
        lift_wait_schedule = (
            lift_wait_rows[
                [
                    ctx.time_col,
                    amr_col,
                    task_col,
                    "_lift_id",
                    from_col,
                    to_col,
                    "lift_wait_s",
                ]
            ]
            .rename(
                columns={
                    ctx.time_col: "time",
                    amr_col: "amr",
                    task_col: "task_id",
                    "_lift_id": "lift_id",
                    from_col: "from",
                    to_col: "to",
                    "lift_wait_s": "wait_s",
                }
            )
            .sort_values("time")
            .reset_index(drop=True)
        )

    # Congestion heatmap data
    congestion_mask = df["_event_text"].str.contains(WAIT_PATTERNS, na=False) | df[
        "_segment_text"
    ].str.contains(WAIT_PATTERNS, na=False)

    congestion_rows = df.loc[congestion_mask].copy()

    congestion_points: List[dict] = []
    for _, row in congestion_rows.iterrows():
        x, y, floor = extract_congestion_point(row, cols)
        if x is None or y is None or floor is None:
            continue

        weight = float(pd.to_numeric(row.get("_wait_s", 0), errors="coerce") or 0.0)
        if weight <= 0:
            weight = float(
                pd.to_numeric(row.get("_duration_s", 0), errors="coerce") or 0.0
            )
        if weight <= 0:
            weight = 1.0

        congestion_points.append(
            {
                "floor": int(floor),
                "x": float(x),
                "y": float(y),
                "weight": float(weight),
                "event": safe_text(row.get("_event_text")),
                "segment": safe_text(row.get("_segment_text")),
            }
        )

    if congestion_points:
        congestion_df = pd.DataFrame(congestion_points)

        grid_size = 2.0
        congestion_df["grid_x"] = (congestion_df["x"] / grid_size).round().astype(int)
        congestion_df["grid_y"] = (congestion_df["y"] / grid_size).round().astype(int)

        congestion_heatmap = (
            congestion_df.groupby(["floor", "grid_x", "grid_y"], dropna=False)
            .agg(
                x=("x", "mean"),
                y=("y", "mean"),
                congestion_score=("weight", "sum"),
                event_count=("weight", "size"),
            )
            .reset_index()
            .sort_values(["floor", "congestion_score"], ascending=[True, False])
            .reset_index(drop=True)
        )
    else:
        congestion_heatmap = pd.DataFrame(
            columns=[
                "floor",
                "grid_x",
                "grid_y",
                "x",
                "y",
                "congestion_score",
                "event_count",
            ]
        )

    # Congestion path data for rectangular path overlays
    # Use travelled segments, weighted by congestion-related wait/duration
    path_source = df[
        df["_segment_text"].str.contains(
            r"corridor|lift_transfer|lift_reposition",
            case=False,
            na=False,
        )
    ].copy()

    congestion_path_rows: List[dict] = []
    for _, row in path_source.iterrows():
        sx = pd.to_numeric(
            row.get(cols["start_x"]) if cols.get("start_x") else None,
            errors="coerce",
        )
        sy = pd.to_numeric(
            row.get(cols["start_y"]) if cols.get("start_y") else None,
            errors="coerce",
        )
        sf = pd.to_numeric(
            row.get(cols["start_floor"]) if cols.get("start_floor") else None,
            errors="coerce",
        )

        ex = pd.to_numeric(
            row.get(cols["end_x"]) if cols.get("end_x") else None,
            errors="coerce",
        )
        ey = pd.to_numeric(
            row.get(cols["end_y"]) if cols.get("end_y") else None,
            errors="coerce",
        )
        ef = pd.to_numeric(
            row.get(cols["end_floor"]) if cols.get("end_floor") else None,
            errors="coerce",
        )

        if pd.isna(sf) and pd.notna(ef):
            sf = ef
        if pd.isna(ef) and pd.notna(sf):
            ef = sf

        if pd.isna(sx) or pd.isna(sy) or pd.isna(ex) or pd.isna(ey) or pd.isna(sf):
            continue

        if pd.notna(ef) and int(sf) != int(ef):
            continue

        if float(sx) == float(ex) and float(sy) == float(ey):
            continue

        weight = float(pd.to_numeric(row.get("_wait_s", 0), errors="coerce") or 0.0)
        if weight <= 0:
            weight = float(
                pd.to_numeric(row.get("_duration_s", 0), errors="coerce") or 0.0
            )
        if weight <= 0:
            weight = 1.0

        congestion_path_rows.append(
            {
                "floor": int(sf),
                "x1": float(sx),
                "y1": float(sy),
                "x2": float(ex),
                "y2": float(ey),
                "congestion_score": float(weight),
                "event": safe_text(row.get("_event_text")),
                "segment": safe_text(row.get("_segment_text")),
            }
        )

    congestion_paths = pd.DataFrame(
        congestion_path_rows,
        columns=[
            "floor",
            "x1",
            "y1",
            "x2",
            "y2",
            "congestion_score",
            "event",
            "segment",
        ],
    )

    active_amrs = int(fleet_tasks["amr"].nunique())
    configured_amrs = configured_amr_quantity(amr_parameters)
    total_amr_route_time_s = interval_total(active_intervals)
    workload_based_amrs = int(
        math.ceil(
            total_amr_route_time_s
            / (horizon_s * max(target_amr_util, 0.01))
        )
    )
    peak_route_concurrency_amrs = peak_time_weighted_concurrency(
        active_intervals, window_sec=300.0
    )
    recommended_amrs = max(1, workload_based_amrs, peak_route_concurrency_amrs)

    lift_intervals = [
        interval
        for intervals in lift_busy_intervals_by_lift.values()
        for interval in intervals
    ]

    total_lift_time_s = interval_total(lift_intervals)
    avg_lift_util = (
        float(lift_summary["utilisation_pct"].mean()) if not lift_summary.empty else 0.0
    )
    workload_based_lifts = (
        int(math.ceil(total_lift_time_s / (horizon_s * max(target_lift_util, 0.01))))
        if total_lift_time_s
        else 0
    )
    peak_lift_concurrency = peak_time_weighted_concurrency(
        lift_intervals, window_sec=300.0
    )
    recommended_lifts = max(
        1 if total_lift_time_s else 0,
        workload_based_lifts,
        peak_lift_concurrency,
    )

    summary_rows = [
            {"metric": "Delivery operating model", "value": operating_model.iloc[0]["label"]},
            {"metric": "Simulation start", "value": fmt_ts(t0, ctx.has_datetime)},
            {"metric": "Simulation finish", "value": fmt_ts(t1, ctx.has_datetime)},
            {"metric": "Simulation duration", "value": fmt_duration(horizon_s)},
            {"metric": "AMRs observed", "value": f"{active_amrs}"},
            {
                "metric": "Tasks without a delivery resource assignment",
                "value": f"{tasks_without_delivery_resource}",
            },
            {"metric": "Tasks total", "value": f"{len(tasks)}"},
            {"metric": "Tasks completed", "value": f"{len(completed)}"},
            {"metric": "Tasks failed", "value": f"{len(failed)}"},
            {"metric": "Tasks pending", "value": f"{len(pending_tasks)}"},
            {"metric": "AMR routes observed", "value": f"{len(amr_route_summary)}"},
            {
                "metric": "Total AMR route time",
                "value": fmt_duration(total_amr_route_time_s),
            },
            {
                "metric": "AMR workload model requirement",
                "value": f"{workload_based_amrs}",
            },
            {
                "metric": "AMR 5-minute peak demand",
                "value": f"{peak_route_concurrency_amrs}",
            },
            {
                "metric": "Total waiting time",
                "value": fmt_duration(tasks["wait_s"].sum()),
            },
            {"metric": "Total lift time", "value": fmt_duration(total_lift_time_s)},
            {"metric": "Average lift utilisation", "value": f"{avg_lift_util:.1f}%"},
            {
                "metric": "Lift 5-minute peak demand",
                "value": f"{peak_lift_concurrency}",
            },
            {"metric": "Recommended AMRs", "value": f"{recommended_amrs}"},
            {"metric": "Recommended lifts", "value": f"{recommended_lifts}"},
        ]
    if operating_model_key == "porter_only":
        amr_only_metrics = {
            "AMRs observed", "AMR routes observed", "Total AMR route time",
            "AMR workload model requirement", "AMR 5-minute peak demand",
            "Recommended AMRs",
        }
        summary_rows = [row for row in summary_rows if row.get("metric") not in amr_only_metrics]
    if configured_amrs is not None and operating_model_key != "porter_only":
        summary_rows.insert(3, {"metric": "AMRs configured", "value": f"{configured_amrs}"})
    summary = pd.DataFrame(summary_rows)

    methodology = pd.DataFrame(
        [
            {
                "item": "Recommended AMRs",
                "detail": f"Maximum of actual AMR route-time workload and 5-minute time-weighted AMR route demand using target utilisation {target_amr_util:.0%}. Multi-stop payload tasks sharing one route are counted once for fleet demand.",
            },
            {
                "item": "Recommended lifts",
                "detail": f"Maximum of merged lift busy-time workload and 5-minute time-weighted lift demand using target utilisation {target_lift_util:.0%}. A single lift is treated as carrying one AMR at a time.",
            },
            {
                "item": "Lift parsing",
                "detail": "Uses segment_type = lift_transfer and parses lift/floor from from_location and to_location.",
            },
            {
                "item": "Idle percentage",
                "detail": "Calculated against the full simulation duration for each AMR and each lift.",
            },
            {
                "item": "Payload schedule",
                "detail": "Uses simulator payload_population_summary rows for total runtime payloads, and keeps unique transported instances and task count as separate diagnostics.",
            },
            {
                "item": "Peak location occupancy",
                "detail": "Reconstructs live payload instances per location from enter/exit rows. The same payload_instance_id is only allowed to occupy one location at a time before peak counts are calculated.",
            },
        ]
    )

    completed_payload_movements = _completed_transport_movement_rows(df)
    payload_population = build_payload_population_summary(df)
    payload_schedule = build_payload_schedule(
        tasks, payload_weights, completed_payload_movements, payload_population
    )

    (
        location_space_utilisation,
        failed_delivery_summary,
        location_recommendations,
        failed_payload_sizes,
    ) = build_location_space_analysis(
        tasks,
        location_catalog,
        payload_dimensions,
        ctx.has_datetime,
    )

    location_peak_occupancy = build_location_peak_occupancy(
        df, ctx, location_catalog, payload_dimensions
    )
    (
        location_space_utilisation,
        location_recommendations,
    ) = apply_peak_occupancy_to_location_outputs(
        location_space_utilisation,
        location_recommendations,
        location_peak_occupancy,
    )

    if not location_space_utilisation.empty:
        over_capacity = int(
            (location_space_utilisation["capacity_related_failures"] > 0).sum()
        )
        additional_spaces = int(
            location_recommendations.get(
                "additional_inventory_spaces", pd.Series(dtype=int)
            ).sum()
        )
        additional_area = float(
            location_recommendations.get(
                "additional_area_m2", pd.Series(dtype=float)
            ).sum()
        )
        capacity_blocked = int(
            location_space_utilisation["capacity_related_failures"].sum()
        )
        summary = pd.concat(
            [
                summary,
                pd.DataFrame(
                    [
                        {
                            "metric": "Deliveries blocked by location capacity",
                            "value": f"{capacity_blocked}",
                        },
                        {
                            "metric": "Locations with capacity failures",
                            "value": f"{over_capacity}",
                        },
                        {
                            "metric": "Additional inventory spaces required",
                            "value": f"{additional_spaces}",
                        },
                        {
                            "metric": "Additional storage area required",
                            "value": f"{additional_area:.2f} m²",
                        },
                    ]
                ),
            ],
            ignore_index=True,
        )

    if not location_peak_occupancy.empty:
        peak_area_required = float(
            pd.to_numeric(
                location_peak_occupancy.get("recommended_area_m2", pd.Series(dtype=float)),
                errors="coerce",
            ).fillna(0.0).sum()
        )
        if not payload_schedule.empty and "total_runtime_payloads" in payload_schedule.columns:
            peak_payload_count = int(
                pd.to_numeric(
                    payload_schedule.get("total_runtime_payloads", pd.Series(dtype=float)),
                    errors="coerce",
                ).fillna(0).sum()
            )
        else:
            peak_payload_count = int(
                pd.to_numeric(
                    location_peak_occupancy.get("peak_payload_count", pd.Series(dtype=float)),
                    errors="coerce",
                ).fillna(0).max()
            )
        summary = pd.concat(
            [
                summary,
                pd.DataFrame(
                    [
                        {
                            "metric": "Total runtime payload population",
                            "value": f"{peak_payload_count}",
                        },
                        {
                            "metric": "Storage area recommended from peak occupancy",
                            "value": f"{peak_area_required:.2f} m²",
                        },
                    ]
                ),
            ],
            ignore_index=True,
        )

    # Scenario, pedestrian, wash, lane and charger impact summary.  The
    # simulator emits a single scenario_impact_summary row at the end of the
    # event log; fall back to aggregating segment columns for older CSVs.
    scenario_details = {}
    if event_col and "details" in df.columns:
        scenario_rows = df[
            df[event_col].astype(str).str.lower().eq("scenario_impact_summary")
        ]
        if not scenario_rows.empty:
            try:
                parsed = json.loads(str(scenario_rows.iloc[-1].get("details", "") or "{}"))
                if isinstance(parsed, dict):
                    scenario_details = parsed
            except Exception:
                scenario_details = {}

    def _numeric_column_total(column_name: str) -> float:
        if column_name not in df.columns:
            return 0.0
        values = pd.to_numeric(df[column_name], errors="coerce").fillna(0.0)
        if event_col:
            summary_mask = df[event_col].astype(str).str.lower().eq("scenario_impact_summary")
            values = values.loc[~summary_mask]
        return float(values.sum())

    scenario_name = str(scenario_details.get("scenario_name", "") or "").strip()
    if not scenario_name and "scenario_name" in df.columns:
        candidates = [str(x).strip() for x in df["scenario_name"].dropna().tolist() if str(x).strip()]
        scenario_name = candidates[-1] if candidates else "Normal operation"
    scenario_name = scenario_name or "Normal operation"
    scenario_mode = bool(
        scenario_details.get(
            "scenario_mode",
            scenario_name.lower() not in {"normal operation", "normal", "baseline"},
        )
    )
    scenario_delay = float(scenario_details.get("scenario_delay_sec", _numeric_column_total("scenario_delay_sec")) or 0.0)
    people_delay = float(scenario_details.get("people_delay_sec", _numeric_column_total("people_delay_sec")) or 0.0)
    scenario_segments = int(float(scenario_details.get("scenario_affected_segments", 0) or 0))
    people_segments = int(float(scenario_details.get("people_affected_segments", 0) or 0))
    if scenario_segments <= 0 and "scenario_delay_sec" in df.columns:
        scenario_segments = int((pd.to_numeric(df["scenario_delay_sec"], errors="coerce").fillna(0.0) > 0).sum())
    if people_segments <= 0 and "people_count" in df.columns:
        people_segments = int((pd.to_numeric(df["people_count"], errors="coerce").fillna(0.0) > 0).sum())
    wash_cycles = int(float(scenario_details.get("wash_cycles", 0) or 0))
    if wash_cycles <= 0 and "wash_cycle" in df.columns:
        wash_cycles = int(df["wash_cycle"].astype(str).str.lower().isin({"true", "1", "yes"}).sum())
    lane_constrained = 0
    if "route_lane_count" in df.columns:
        lane_constrained = int((pd.to_numeric(df["route_lane_count"], errors="coerce").fillna(0) == 1).sum())
    lift_health_min = None
    lift_unavailable_rows = 0
    if "lift_health_percent" in df.columns:
        health_values = pd.to_numeric(df["lift_health_percent"], errors="coerce").dropna()
        if not health_values.empty:
            lift_health_min = float(health_values.min())
    if "lift_operational" in df.columns:
        lift_unavailable_rows = int(df["lift_operational"].astype(str).str.lower().isin({"false", "0", "no"}).sum())
    configured_chargers = int(float(scenario_details.get("configured_chargers", 0) or 0))
    required_chargers = int(float(scenario_details.get("required_peak_concurrent_chargers", 0) or 0))
    charger_shortfall = int(float(scenario_details.get("charger_shortfall", max(0, required_chargers - configured_chargers)) or 0))
    reduced_logging = bool(scenario_mode and payload_col and df[payload_col].fillna("").astype(str).str.strip().eq("").mean() > 0.8)

    scenario_impact = pd.DataFrame(
        [
            {"metric": "Operating mode", "value": "Scenario" if scenario_mode else "Normal operation", "unit": "", "interpretation": scenario_name},
            {"metric": "Scenario-attributed delay", "value": round(scenario_delay, 3), "unit": "s", "interpretation": f"Across {scenario_segments} affected route segments"},
            {"metric": "Pedestrian-attributed delay", "value": round(people_delay, 3), "unit": "s", "interpretation": f"Across {people_segments} occupied route segments"},
            {"metric": "Wash cycles", "value": wash_cycles, "unit": "cycles", "interpretation": "Infection/hazard location decontamination cycles"},
            {"metric": "Single-lane constrained traversals", "value": lane_constrained, "unit": "segments", "interpretation": "Bidirectional routes serialised by corridor or carried-payload width"},
            {"metric": "Minimum observed lift health", "value": "" if lift_health_min is None else round(lift_health_min, 2), "unit": "%", "interpretation": f"{lift_unavailable_rows} logged lift-unavailable rows"},
            {"metric": "Configured chargers", "value": configured_chargers, "unit": "chargers", "interpretation": "Only AMR bays explicitly marked as charging spaces"},
            {"metric": "Peak chargers required", "value": required_chargers, "unit": "chargers", "interpretation": f"Shortfall: {charger_shortfall}"},
            {"metric": "Scenario CSV logging", "value": "Reduced" if reduced_logging else "Enhanced/full", "unit": "", "interpretation": "Reduced mode suppresses payload transition detail but retains trip records"},
        ]
    )

    return {
        "summary": summary,
        "scenario_impact": scenario_impact,
        "task_generation_summary": task_generation_summary,
        "payload_handling_timetable": payload_handling_timetable,
        "linen_ward_collection": linen_ward_collection,
        "staff_hours_summary": staff_hours_summary,
        "staff_handling_summary": staff_handling_summary,
        "amr_summary": amr_summary,
        "porter_summary": porter_summary,
        "delivery_allocation_summary": allocation,
        "operating_model": operating_model,
        "amr_route_summary": amr_route_summary,
        "utilisation_summary": amr_utilisation,
        "lift_summary": lift_summary,
        "lift_usage_profile": lift_usage_profile,
        "tasks": tasks.sort_values(["amr", "start", "task_id"]).reset_index(drop=True),
        "pending_tasks": pending_tasks.reset_index(drop=True),
        "methodology": methodology,
        "payload_schedule": payload_schedule,
        "location_space_utilisation": location_space_utilisation,
        "failed_delivery_summary": failed_delivery_summary,
        "location_recommendations": location_recommendations,
        "location_peak_occupancy": location_peak_occupancy,
        "failed_payload_sizes": failed_payload_sizes,
        "lift_wait_schedule": lift_wait_schedule,
        "recharge_summary": recharge_summary,
        "amr_list": (
            amr_parameters.copy()
            if amr_parameters is not None
            else pd.DataFrame(
                columns=[
                    "amr",
                    "quantity",
                    "payload_capacity_kg",
                    "payload_capacity_size_units",
                    "speed_m_per_sec",
                    "battery_capacity_kwh",
                    "battery_charge_rate_kw",
                    "recharge_threshold_percent",
                    "battery_soc_percent",
                    "start_location",
                ]
            )
        ),
        "congestion_heatmap": congestion_heatmap,
        "congestion_paths": congestion_paths,
        "floor_dxf_map": floor_dxf_map or {},
    }
