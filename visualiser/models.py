import json
from copy import deepcopy
from pathlib import Path
from typing import Dict, List, Optional, Tuple

LOGISTICS_TASK_GENERATION_CATEGORIES = [
    ("catering", "Catering"),
    ("pharmacy", "Pharmacy"),
    ("linen", "Linen"),
    ("waste", "Waste"),
    ("stores", "Stores"),
    ("ssd", "SSD"),
]


def _parse_hhmm_to_minutes(value, default=None):
    text = str(value or "").strip()
    if not text:
        return default
    try:
        parts = text.split(":")
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        if hour == 24 and minute == 0:
            return 24 * 60
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return (hour * 60) + minute
    except Exception:
        return default
    return default


def _minutes_to_hhmm(value):
    value = int(value)
    if value >= 24 * 60:
        return "24:00"
    value = value % (24 * 60)
    return f"{value // 60:02d}:{value % 60:02d}"


def calculated_operating_hours_per_day(dept: dict) -> float:
    dept = dept or {}
    start = _parse_hhmm_to_minutes(dept.get("operating_start_time"), 0)
    end = _parse_hhmm_to_minutes(dept.get("operating_end_time"), None)
    if end is None:
        legacy_hours = max(
            0.0, min(float(dept.get("hours_operated_per_day", 24.0) or 24.0), 24.0)
        )
        if legacy_hours >= 24.0:
            end = start + (24 * 60)
            dept["operating_end_time"] = "24:00"
        else:
            end = start + int(round(legacy_hours * 60.0))
            dept["operating_end_time"] = _minutes_to_hhmm(end)
    if end == start:
        return 24.0
    if end < start:
        end += 24 * 60
    return round(max(0.0, min((end - start) / 60.0, 24.0)), 4)


def normalise_amr_payload_slots(amr: dict) -> list:
    """Return payload slot definitions, migrating legacy single-capacity AMRs."""
    if not isinstance(amr, dict):
        amr = {}

    slots = amr.get("payload_slots", [])
    clean = []

    if isinstance(slots, list):
        for idx, slot in enumerate(slots, start=1):
            if not isinstance(slot, dict):
                continue
            clean_slot = {
                "name": str(slot.get("name", "")).strip() or f"Slot {idx}",
                "payload_capacity_kg": float(
                    slot.get("payload_capacity_kg", 0.0) or 0.0
                ),
                "payload_length_capacity_m": float(
                    slot.get("payload_length_capacity_m", 0.0) or 0.0
                ),
                "payload_width_capacity_m": float(
                    slot.get("payload_width_capacity_m", 0.0) or 0.0
                ),
                "payload_height_capacity_m": float(
                    slot.get("payload_height_capacity_m", 0.0) or 0.0
                ),
                "allowed_payload_orientations": [
                    str(x).strip().lower()
                    for x in (slot.get("allowed_payload_orientations") or ["lengthways", "sideways"])
                    if str(x).strip().lower() in {"lengthways", "sideways"}
                ] or ["lengthways"],
            }
            clean.append(clean_slot)

    if not clean:
        clean = [
            {
                "name": "Slot 1",
                "payload_capacity_kg": float(
                    amr.get("payload_capacity_kg", 100) or 100
                ),
                "payload_length_capacity_m": float(
                    amr.get("payload_length_capacity_m", 1.0) or 1.0
                ),
                "payload_width_capacity_m": float(
                    amr.get("payload_width_capacity_m", 1.0) or 1.0
                ),
                "payload_height_capacity_m": float(
                    amr.get("payload_height_capacity_m", 1.0) or 1.0
                ),
                "allowed_payload_orientations": ["lengthways", "sideways"],
            }
        ]

    return clean


def amr_supports_manual_tasks(amr: dict) -> bool:
    return len(normalise_amr_payload_slots(amr)) == 1


DELIVERY_RESOURCE_MODES = {"amr", "staff", "either"}


def default_delivery_resource_policy() -> dict:
    return {"mode": "amr"}


def normalise_delivery_resource_policy(value) -> dict:
    if isinstance(value, str):
        source = {"mode": value}
    elif isinstance(value, dict):
        source = dict(value)
    else:
        source = {}
    mode = str(source.get("mode", source.get("delivery_method", "amr")) or "amr").strip().lower()
    if mode not in DELIVERY_RESOURCE_MODES:
        mode = "amr"
    result = {"mode": mode}
    for key in ("allowed_amr_types", "allowed_staff_types", "required_capabilities"):
        values = source.get(key, [])
        if isinstance(values, str):
            values = [item.strip() for item in values.split(",")]
        result[key] = list(dict.fromkeys(str(item).strip() for item in (values or []) if str(item).strip()))
    return result


def normalise_staff_delivery_resource(value: Optional[dict], index: int = 1) -> dict:
    source = dict(value or {})
    days = source.get("days_active", ["mon", "tue", "wed", "thu", "fri"])
    if isinstance(days, str):
        days = [item.strip().lower() for item in days.split(",")]
    capabilities = source.get("capabilities", [])
    if isinstance(capabilities, str):
        capabilities = [item.strip() for item in capabilities.split(",")]
    breaks = []
    for raw in source.get("breaks", []) or []:
        if isinstance(raw, dict):
            breaks.append({
                "name": str(raw.get("name", "Break") or "Break").strip(),
                "start_time": str(raw.get("start_time", "") or "").strip(),
                "end_time": str(raw.get("end_time", "") or "").strip(),
            })
    return {
        "id": str(source.get("id", f"PORTER-{index}") or f"PORTER-{index}").strip(),
        "quantity": max(1, int(float(source.get("quantity", 1) or 1))),
        "base_location": str(source.get("base_location", "") or "").strip(),
        "speed_m_per_sec": max(0.01, float(source.get("speed_m_per_sec", 1.2) or 1.2)),
        "payload_capacity_kg": max(0.0, float(source.get("payload_capacity_kg", 25.0) or 0.0)),
        "payload_length_capacity_m": max(0.0, float(source.get("payload_length_capacity_m", 1.0) or 0.0)),
        "payload_width_capacity_m": max(0.0, float(source.get("payload_width_capacity_m", 0.8) or 0.0)),
        "payload_height_capacity_m": max(0.0, float(source.get("payload_height_capacity_m", 1.5) or 0.0)),
        "turnaround_time_sec": max(0.0, float(source.get("turnaround_time_sec", 300.0) or 0.0)),
        "shift_start_time": str(source.get("shift_start_time", "07:00") or "07:00").strip(),
        "shift_end_time": str(source.get("shift_end_time", "15:00") or "15:00").strip(),
        "days_active": list(dict.fromkeys(str(item).strip().lower() for item in (days or []) if str(item).strip())),
        "breaks": breaks,
        "capabilities": list(dict.fromkeys(str(item).strip() for item in (capabilities or []) if str(item).strip())),
    }


def default_task_generation_category(label: str) -> dict:
    return {
        "enabled": False,
        "display_name": label,
        "generation_mode": "scheduled",
        "priority": 100,
        "pickup_location": "",
        "dropoff_location": "",
        "dropoff_locations": [],
        "payload": "",
        "tracked_item_exchange": False,
        "exchange_mode": "top_up_only",
        "return_enabled": False,
        "return_payload": "",
        "requires_staff": False,
        "staff_initial_count": 1,
        "staff_resource_name": "",
        "staff_movement_policy": "batch_same_location",
        "staff_shift_pattern": "none",
        "staff_handling_minutes": 15.0,
        "staff_use_custom_working_hours": False,
        "staff_working_hours": {},
        "reusable_return_pool_enabled": False,
        "reusable_return_pool_multiplier": 2.0,
        "reusable_return_pool_max": 0,
        "route_profile": "",
        "days_active": ["mon", "tue", "wed", "thu", "fri"],
        "run_every_fortnight": False,
        "schedule_times": [],
        "frequency_per_day": 0.0,
        "volume_per_event_m3": 0.0,
        "threshold_volume_m3": 0.0,
        "base_daily_volume_m3": 0.0,
        "timeframe_start": "09:00",
        "timeframe_end": "17:00",
        "timeframe_payload_multiple": 1,
        "payload_multiple": 1,
        "delivery_resource": default_delivery_resource_policy(),
        "notes": "",
    }


def default_staff_task_generation_config() -> dict:
    """Return global staff working patterns used by staff-assisted generators."""
    return {
        "enabled": True,
        "spread_timeframe_tasks": True,
        "walking_speed_m_per_sec": 1.2,
        "lift_wait_seconds": 30.0,
        "default_handling_minutes": 15.0,
        "shift_patterns": {
            # ``none`` is the existing category value for a non-rotating team.
            # It now uses these global fixed hours without changing legacy JSON.
            "none": {
                "display_name": "Fixed working hours",
                "start_time": "09:00",
                "end_time": "17:00",
                "days_active": ["mon", "tue", "wed", "thu", "fri"],
                "work_days": 0,
                "rest_days": 0,
            },
            "four_on_four_off_12h": {
                "display_name": "4 on / 4 off, 12-hour days",
                "start_time": "07:00",
                "end_time": "19:00",
                "days_active": [
                    "mon",
                    "tue",
                    "wed",
                    "thu",
                    "fri",
                    "sat",
                    "sun",
                ],
                "work_days": 4,
                "rest_days": 4,
            },
        },
    }


def normalise_staff_task_generation_config(value: Optional[dict]) -> dict:
    defaults = default_staff_task_generation_config()
    incoming = value if isinstance(value, dict) else {}
    result = deepcopy(defaults)
    result["enabled"] = bool(incoming.get("enabled", result["enabled"]))
    result["spread_timeframe_tasks"] = bool(
        incoming.get(
            "spread_timeframe_tasks",
            incoming.get("space_timeframe_tasks", result["spread_timeframe_tasks"]),
        )
    )
    for field_name, minimum in (
        ("walking_speed_m_per_sec", 0.1),
        ("lift_wait_seconds", 0.0),
        ("default_handling_minutes", 0.0),
    ):
        try:
            result[field_name] = max(
                minimum, float(incoming.get(field_name, result[field_name]))
            )
        except Exception:
            pass

    incoming_patterns = incoming.get("shift_patterns", {})
    if not isinstance(incoming_patterns, dict):
        incoming_patterns = {}

    for key, raw in incoming_patterns.items():
        if not isinstance(raw, dict):
            continue
        pattern_key = str(key or "").strip().lower()
        if pattern_key in {
            "4_on_4_off_12h",
            "four_on_four_off",
            "four_on_four_off_12_hour",
        }:
            pattern_key = "four_on_four_off_12h"
        if not pattern_key:
            continue
        target = result["shift_patterns"].setdefault(pattern_key, {})
        target.update(raw)

    valid_days = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
    for key, pattern in list(result["shift_patterns"].items()):
        if not isinstance(pattern, dict):
            pattern = {}
            result["shift_patterns"][key] = pattern
        fallback = defaults["shift_patterns"].get(key, defaults["shift_patterns"]["none"])
        pattern["display_name"] = str(
            pattern.get("display_name", fallback.get("display_name", key.title()))
            or fallback.get("display_name", key.title())
        ).strip()
        for field in ("start_time", "end_time"):
            candidate = str(pattern.get(field, fallback.get(field, "")) or "").strip()
            if _parse_hhmm_to_minutes(candidate, None) is None:
                candidate = str(fallback.get(field, "09:00" if field == "start_time" else "17:00"))
            pattern[field] = candidate
        days = pattern.get("days_active", fallback.get("days_active", []))
        if isinstance(days, str):
            days = [x.strip() for x in days.split(",")]
        clean_days = []
        for day in days or []:
            day_key = str(day or "").strip().lower()[:3]
            if day_key in valid_days and day_key not in clean_days:
                clean_days.append(day_key)
        pattern["days_active"] = clean_days or list(fallback.get("days_active", []))
        try:
            pattern["work_days"] = max(0, int(float(pattern.get("work_days", fallback.get("work_days", 0)) or 0)))
        except Exception:
            pattern["work_days"] = int(fallback.get("work_days", 0) or 0)
        try:
            pattern["rest_days"] = max(0, int(float(pattern.get("rest_days", fallback.get("rest_days", 0)) or 0)))
        except Exception:
            pattern["rest_days"] = int(fallback.get("rest_days", 0) or 0)

    return result


def default_task_generation_config() -> dict:
    categories = {
        key: default_task_generation_category(label)
        for key, label in LOGISTICS_TASK_GENERATION_CATEGORIES
    }

    categories["catering"].update(
        {
            "generation_mode": "scheduled",
            "priority": 40,
            "schedule_times": ["07:30", "11:45", "16:45"],
            "return_enabled": True,
            "return_delay_minutes": 0,
        }
    )
    categories["pharmacy"].update(
        {
            "generation_mode": "scheduled_sporadic",
            "priority": 30,
            "schedule_times": ["10:00", "15:00"],
            "frequency_per_day": 2.0,
        }
    )
    categories["linen"].update(
        {
            "generation_mode": "threshold",
            "priority": 55,
            "threshold_volume_m3": 0.8,
            "base_daily_volume_m3": 0.0,
            "return_enabled": True,
            "return_delay_minutes": 0,
        }
    )
    categories["waste"].update(
        {
            "enabled": True,
            "generation_mode": "threshold",
            "uses_department_waste_streams": True,
            "priority": 60,
            "threshold_volume_m3": 0.0,
            "base_daily_volume_m3": 0.0,
            "return_enabled": True,
            "return_delay_minutes": 0,
        }
    )
    categories["stores"].update(
        {
            "generation_mode": "scheduled",
            "priority": 70,
            "schedule_times": ["09:30", "14:30"],
            "requires_staff": True,
        }
    )
    categories["ssd"].update(
        {
            "generation_mode": "scheduled_threshold",
            "priority": 35,
            "schedule_times": ["08:00", "12:00", "17:00"],
            "return_enabled": True,
            "return_delay_minutes": 0,
        }
    )

    return {
        "enabled": True,
        "staff_config": default_staff_task_generation_config(),
        # Legacy compatibility only. Runtime Waste generation is now driven by
        # task_generation.categories.waste plus departments[].waste_streams[].
        "department_waste": {
            "enabled": True,
            "priority": 60,
        },
        "categories": categories,
    }


def merge_task_generation_defaults(value: Optional[dict]) -> dict:
    merged = default_task_generation_config()
    if not isinstance(value, dict):
        return merged

    merged["enabled"] = bool(value.get("enabled", merged["enabled"]))
    merged["staff_config"] = normalise_staff_task_generation_config(
        value.get("staff_config", value.get("staff", {}))
    )

    if isinstance(value.get("department_waste"), dict):
        merged["department_waste"].update(value.get("department_waste", {}))

    incoming_categories = value.get("categories", {})
    if isinstance(incoming_categories, dict):
        for key, incoming in incoming_categories.items():
            if key not in merged["categories"]:
                merged["categories"][key] = (
                    dict(incoming) if isinstance(incoming, dict) else {}
                )
            elif isinstance(incoming, dict):
                merged["categories"][key].update(incoming)

    # Support older experiments where categories were stored directly under task_generation.
    for key, _label in LOGISTICS_TASK_GENERATION_CATEGORIES:
        if isinstance(value.get(key), dict):
            merged["categories"][key].update(value[key])

    for category in merged["categories"].values():
        if not isinstance(category, dict):
            continue
        category["delivery_resource"] = normalise_delivery_resource_policy(
            category.get("delivery_resource", category.get("delivery_method", "amr"))
        )
        dropoff_locations = category.get("dropoff_locations")
        if isinstance(dropoff_locations, list):
            clean = [str(x).strip() for x in dropoff_locations if str(x).strip()]
        else:
            clean = []
        legacy_dropoff = str(category.get("dropoff_location", "")).strip()
        if legacy_dropoff and legacy_dropoff not in clean:
            clean.insert(0, legacy_dropoff)
        category["dropoff_locations"] = clean
        category["dropoff_location"] = clean[0] if clean else legacy_dropoff

        if "timeframe_payload_multiple" not in category:
            category["timeframe_payload_multiple"] = category.get("payload_multiple", 1)
        if "payload_multiple" not in category:
            category["payload_multiple"] = category.get("timeframe_payload_multiple", 1)
        category.setdefault("timeframe_start", "09:00")
        category.setdefault("timeframe_end", "17:00")
        category["requires_staff"] = bool(
            category.get("requires_staff", category.get("staff_required", False))
        )
        try:
            category["staff_initial_count"] = max(
                1, int(float(category.get("staff_initial_count", 1) or 1))
            )
        except Exception:
            category["staff_initial_count"] = 1
        category.setdefault("staff_resource_name", "")
        policy = str(category.get("staff_movement_policy", "") or "").strip().lower()
        if policy == "minimize_movement":
            policy = "minimise_movement"
        if policy not in {"available_first", "batch_same_location", "minimise_movement"}:
            policy = "batch_same_location"
        category["staff_movement_policy"] = policy
        shift_pattern = str(category.get("staff_shift_pattern", "") or "").strip().lower()
        if shift_pattern in {"4_on_4_off_12h", "four_on_four_off", "four_on_four_off_12_hour"}:
            shift_pattern = "four_on_four_off_12h"
        if shift_pattern not in {"none", "four_on_four_off_12h"}:
            shift_pattern = "none"
        category["staff_shift_pattern"] = shift_pattern
        try:
            category["staff_handling_minutes"] = max(
                0.0, float(category.get("staff_handling_minutes", 15.0) or 0.0)
            )
        except Exception:
            category["staff_handling_minutes"] = 15.0
        category["staff_use_custom_working_hours"] = bool(
            category.get("staff_use_custom_working_hours", False)
        )
        weekly = category.get("staff_working_hours", {})
        category["staff_working_hours"] = (
            dict(weekly) if isinstance(weekly, dict) else {}
        )
        category["run_every_fortnight"] = bool(category.get("run_every_fortnight", False))

    # Keep the legacy department_waste mirror in step for older configs/tools.
    # It is not a separate editor workflow; Waste stream volume settings live on
    # departments[].waste_streams[] and are consumed by the Waste task generator.
    if "waste" in merged["categories"]:
        merged["categories"]["waste"]["uses_department_waste_streams"] = True
        merged["department_waste"]["enabled"] = bool(
            merged["categories"]["waste"].get(
                "enabled", merged["department_waste"].get("enabled", True)
            )
        )
        merged["department_waste"]["priority"] = int(
            float(
                merged["categories"]["waste"].get(
                    "priority", merged["department_waste"].get("priority", 60)
                )
            )
        )

    return merged


DEFAULT_JSON = {
    "simulation": {
        "start_datetime": "2026-01-05T06:00:00",
        "end_datetime": "2026-01-06T06:00:00",
        "tick_rate": 1000,
        "generated_task_release_stagger_sec": 0.25,
        "precompute_static_routes": True,
        "route_precompute_max_pairs": 100000,
        "max_multi_stop_candidate_tasks": 8,
        "max_single_candidate_tasks": 8,
        "max_assignments_per_tick": 25,
        "assignment_continue_delay_sec": 0.001,
        "seed_waste_stream_containers_at_start": False,
        "disable_inventory_spaces": False,
        "scenario_mode": False,
        "scenario_enhanced_logging": False,
    },
    "building": {
        "load_unload_time_sec": 20.0,
        "floor_height_m": 4.0,
        "charge_locations": ["AMR-CENTRE"],
        "default_corridor_width_m": 2.4,
        "default_door_clear_width_m": 0.9,
        "people_slowdown_per_person": 0.03,
        "minimum_people_speed_factor": 0.25,
    },
    "locations": [],
    "corridors": {
        "nodes": [],
        "edges": [],
        "auto_connect": False,
    },
    "payloads": [],
    "waste_streams": [],
    "mass_collections": [],
    "departments": [],
    "amrs": [],
    "staff_delivery_resources": [],
    "lifts": [],
    "people_movements": [],
    "scenario_testing": {
        "enabled": False,
        "active_scenario": "Normal operation",
        "enhanced_logging": False,
        "scenarios": [],
    },
    "floor_dxf_files": [],
    "tasks": [],
    "task_generation": default_task_generation_config(),
    "route_profiles": {
        "default": {
            "allowed_lifts": [],
            "allowed_nodes": [],
            "allowed_edges": [],
        }
    },
}


class JsonStore:
    def __init__(self, data: Optional[dict] = None):
        self.data = deepcopy(data) if data else deepcopy(DEFAULT_JSON)
        self.ensure_simulation_defaults()
        self.ensure_task_generation_defaults()
        self.ensure_payload_defaults()
        self.ensure_amr_defaults()
        self.ensure_delivery_resource_defaults()
        self.ensure_department_defaults()
        self.ensure_mass_collection_defaults()
        self.ensure_location_defaults()
        self.ensure_corridor_defaults()
        self.ensure_people_movement_defaults()
        self.ensure_scenario_defaults()

    def ensure_simulation_defaults(self) -> None:
        simulation = self.data.setdefault("simulation", {})
        default_simulation = DEFAULT_JSON.get("simulation", {})
        simulation.setdefault(
            "start_datetime",
            default_simulation.get("start_datetime", "2026-01-05T06:00:00"),
        )
        simulation.setdefault(
            "end_datetime",
            default_simulation.get("end_datetime", "2026-01-06T06:00:00"),
        )
        simulation.setdefault("tick_rate", default_simulation.get("tick_rate", 1000))
        simulation.setdefault(
            "generated_task_release_stagger_sec",
            default_simulation.get("generated_task_release_stagger_sec", 0.25),
        )
        simulation.setdefault(
            "precompute_static_routes",
            default_simulation.get("precompute_static_routes", True),
        )
        simulation.setdefault(
            "route_precompute_max_pairs",
            default_simulation.get("route_precompute_max_pairs", 100000),
        )
        simulation.setdefault(
            "max_multi_stop_candidate_tasks",
            default_simulation.get("max_multi_stop_candidate_tasks", 8),
        )
        simulation.setdefault(
            "max_single_candidate_tasks",
            default_simulation.get("max_single_candidate_tasks", 8),
        )
        simulation.setdefault(
            "max_assignments_per_tick",
            default_simulation.get("max_assignments_per_tick", 25),
        )
        simulation.setdefault(
            "assignment_continue_delay_sec",
            default_simulation.get("assignment_continue_delay_sec", 0.001),
        )
        simulation.setdefault(
            "seed_waste_stream_containers_at_start",
            default_simulation.get("seed_waste_stream_containers_at_start", False),
        )
        simulation.setdefault(
            "disable_inventory_spaces",
            default_simulation.get("disable_inventory_spaces", False),
        )

    def ensure_department_defaults(self) -> None:
        for dept in self.data.setdefault("departments", []):
            dept.setdefault("enabled", True)
            dept.setdefault("operating_start_time", "00:00")
            if not str(dept.get("operating_end_time", "") or "").strip():
                calculated_operating_hours_per_day(dept)
            dept["hours_operated_per_day"] = calculated_operating_hours_per_day(dept)
            dept.setdefault("days_active", ["mon", "tue", "wed", "thu", "fri"])
            dept.setdefault("waste_streams", [])

    def ensure_mass_collection_defaults(self) -> None:
        clean = []
        for index, item in enumerate(
            self.data.setdefault("mass_collections", []), start=1
        ):
            if not isinstance(item, dict):
                continue
            location = str(
                item.get("location", item.get("store_location", "")) or ""
            ).strip()
            if not location:
                continue
            payloads = item.get("payloads", item.get("payload_names", []))
            if isinstance(payloads, str):
                payloads = [x.strip() for x in payloads.split(",")]
            if not isinstance(payloads, list):
                payloads = []
            times = item.get("scheduled_times", item.get("schedule_times", []))
            if isinstance(times, str):
                times = [x.strip() for x in times.split(",")]
            if not isinstance(times, list):
                times = []
            days = item.get("days_active", item.get("active_days", []))
            if isinstance(days, str):
                days = [x.strip() for x in days.split(",")]
            if not isinstance(days, list) or not days:
                days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
            clean.append(
                {
                    "id": str(
                        item.get("id", item.get("name", f"MASS-COLLECTION-{index}"))
                        or f"MASS-COLLECTION-{index}"
                    ).strip(),
                    "enabled": bool(item.get("enabled", True)),
                    "location": location,
                    "payloads": [str(x).strip() for x in payloads if str(x).strip()],
                    "days_active": [
                        str(x).strip().lower()[:3] for x in days if str(x).strip()
                    ],
                    "scheduled_times": [
                        str(x).strip() for x in times if str(x).strip()
                    ],
                    "capacity_trigger_fraction": float(
                        item.get(
                            "capacity_trigger_fraction",
                            item.get("trigger_fraction", 0.0),
                        )
                        or 0.0
                    ),
                    "capacity_trigger_count": int(
                        float(
                            item.get(
                                "capacity_trigger_count", item.get("trigger_count", 0)
                            )
                            or 0
                        )
                    ),
                    "capacity_check_interval_minutes": float(
                        item.get(
                            "capacity_check_interval_minutes",
                            item.get("check_interval_minutes", 15.0),
                        )
                        or 15.0
                    ),
                    "replace_with_empty_equivalents": bool(
                        item.get("replace_with_empty_equivalents", True)
                    ),
                    "notes": str(item.get("notes", "") or ""),
                }
            )
        self.data["mass_collections"] = clean

    def ensure_payload_defaults(self) -> None:
        for payload in self.data.setdefault("payloads", []):
            payload.setdefault("track_items", False)

            items = payload.get("items", {})

            if isinstance(items, list):
                converted = {}
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("name", "")).strip()
                    if not name:
                        continue
                    converted[name] = item
                items = converted

            if not isinstance(items, dict):
                items = {}

            clean = {}

            for name, cfg in items.items():
                if not str(name).strip():
                    continue

                cfg = cfg if isinstance(cfg, dict) else {}

                clean[str(name).strip()] = {
                    "max": float(cfg.get("max", 100)),
                    "top_up_threshold": float(cfg.get("top_up_threshold", 15)),
                    "usage_rate": str(cfg.get("usage_rate", "scheduled_sporadic")),
                    "consumption_per_day": float(cfg.get("consumption_per_day", 0.0)),
                    "exchange_payload": str(cfg.get("exchange_payload", "")),
                    "source_location": str(cfg.get("source_location", "")),
                }

            payload["items"] = clean
            orientations = payload.get("allowed_carry_orientations", ["lengthways", "sideways"])
            if isinstance(orientations, str):
                orientations = [x.strip() for x in orientations.split(",")]
            payload["allowed_carry_orientations"] = [
                str(x).strip().lower()
                for x in (orientations or [])
                if str(x).strip().lower() in {"lengthways", "sideways"}
            ] or ["lengthways"]

    def ensure_location_defaults(self) -> None:
        for location in self.data.setdefault("locations", []):
            location.setdefault("wash_cycle_required", False)
            location.setdefault("wash_cycle_duration_sec", 300.0)
            location.setdefault("wash_location", "")
            area = str(location.get("people_area_type", "none") or "none").strip().lower()
            location["people_area_type"] = area if area in {"none", "staff", "public", "both"} else "none"
            for space in location.setdefault("inventory_spaces", []):
                if not isinstance(space, dict):
                    continue
                is_amr = bool(space.get("stores_amr", False)) or str(space.get("space_type", "")).lower() == "amr"
                for slot in space.get("payload_slots", []) or []:
                    if isinstance(slot, dict) and (str(slot.get("slot_type", "")).lower() == "amr" or str(slot.get("amr_type", "")).strip()):
                        is_amr = True
                if is_amr:
                    space.setdefault("has_charger", False)

    def ensure_corridor_defaults(self) -> None:
        building = self.data.setdefault("building", {})
        default_width = float(building.get("default_corridor_width_m", 2.4) or 2.4)
        default_door_width = float(building.get("default_door_clear_width_m", 0.9) or 0.9)
        building.setdefault("default_door_clear_width_m", default_door_width)

        corridors = self.data.setdefault("corridors", {})
        for node in corridors.setdefault("nodes", []):
            if not isinstance(node, dict):
                continue
            node["has_door"] = bool(node.get("has_door", False))
            try:
                node["door_clear_width_m"] = max(0.1, float(node.get("door_clear_width_m", default_door_width) or default_door_width))
            except Exception:
                node["door_clear_width_m"] = default_door_width

        for edge in corridors.setdefault("edges", []):
            if not isinstance(edge, dict):
                continue
            edge.setdefault("bidirectional", True)
            try:
                edge["width_m"] = max(0.1, float(edge.get("width_m", default_width) or default_width))
            except Exception:
                edge["width_m"] = default_width
            area = str(edge.get("people_area_type", "none") or "none").strip().lower()
            if area == "mixed":
                area = "both"
            edge["people_area_type"] = area if area in {"none", "staff", "public", "both"} else "none"
            profile_ids = edge.get("people_profile_ids", [])
            if isinstance(profile_ids, str):
                profile_ids = [x.strip() for x in profile_ids.split(",") if x.strip()]
            edge["people_profile_ids"] = list(dict.fromkeys(str(x).strip() for x in (profile_ids or []) if str(x).strip()))

    @staticmethod
    def _normalise_corridor_resource(value) -> str:
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            a, b = str(value[0]).strip(), str(value[1]).strip()
            return f"{a} -> {b}" if a and b else ""
        text = str(value or "").strip().replace("<->", "->")
        parts = [x.strip() for x in text.split("->") if x.strip()]
        return f"{parts[0]} -> {parts[1]}" if len(parts) >= 2 else ""

    def ensure_people_movement_defaults(self) -> None:
        clean = []
        for index, raw in enumerate(self.data.setdefault("people_movements", []), start=1):
            if not isinstance(raw, dict):
                continue
            group = str(raw.get("group_type", "staff") or "staff").strip().lower()
            if group == "mixed":
                group = "both"
            if group not in {"staff", "public", "both"}:
                group = "staff"
            corridor_edges = []
            for value in raw.get("corridor_edges", []) or []:
                normalised = self._normalise_corridor_resource(value)
                if normalised and normalised not in corridor_edges:
                    corridor_edges.append(normalised)
            shape = str(raw.get("profile_shape", "constant") or "constant").strip().lower()
            if shape not in {"constant", "ramp_up", "ramp_down", "ramp_up_down"}:
                shape = "constant"
            peak_people = max(1, int(float(raw.get("people_per_trip", 1) or 1)))
            minimum_people = max(
                0,
                min(
                    peak_people,
                    int(float(raw.get("minimum_people_per_interval", 0) or 0)),
                ),
            )
            clean.append({
                "id": str(raw.get("id", f"PEOPLE-{index}") or f"PEOPLE-{index}"),
                "enabled": bool(raw.get("enabled", True)),
                "group_type": group,
                "start_location": str(raw.get("start_location", "") or ""),
                "end_location": str(raw.get("end_location", "") or ""),
                "corridor_edges": corridor_edges,
                "people_per_trip": peak_people,
                "minimum_people_per_interval": minimum_people,
                "profile_shape": shape,
                "timeframe_name": str(raw.get("timeframe_name", "") or "").strip(),
                "timeframe_preset": str(raw.get("timeframe_preset", "custom") or "custom").strip().lower(),
                "start_time": str(raw.get("start_time", "08:00") or "08:00"),
                "end_time": str(raw.get("end_time", "18:00") or "18:00"),
                "ramp_up_minutes": max(0.0, float(raw.get("ramp_up_minutes", 60.0) or 0.0)),
                "ramp_down_minutes": max(0.0, float(raw.get("ramp_down_minutes", 60.0) or 0.0)),
                "fit_profile_to_timeframe": bool(raw.get("fit_profile_to_timeframe", False)),
                "interval_minutes": max(0.1, float(raw.get("interval_minutes", 15.0) or 15.0)),
                "walking_speed_m_per_sec": max(0.1, float(raw.get("walking_speed_m_per_sec", 1.2) or 1.2)),
                "days_active": list(raw.get("days_active", ["mon", "tue", "wed", "thu", "fri"]) or []),
                "amr_speed_factor": max(0.05, min(1.0, float(raw.get("amr_speed_factor", 0.7) or 0.7))),
            })
        self.data["people_movements"] = clean

    def ensure_scenario_defaults(self) -> None:
        cfg = self.data.setdefault("scenario_testing", {})
        cfg.setdefault("enabled", False)
        cfg.setdefault("active_scenario", "Normal operation")
        cfg.setdefault("enhanced_logging", False)
        clean = []
        for index, scenario in enumerate(cfg.setdefault("scenarios", []), start=1):
            if not isinstance(scenario, dict):
                continue
            events = []
            for event in scenario.get("events", []) or []:
                if not isinstance(event, dict):
                    continue
                resource_type = str(event.get("resource_type", "lift") or "lift").strip().lower()
                if resource_type not in {"lift", "corridor", "corridor_node", "amr"}:
                    resource_type = "lift"
                raw_ids = event.get("resource_ids", [])
                if isinstance(raw_ids, str):
                    raw_ids = [x.strip() for x in raw_ids.split(",") if x.strip()]
                resource_ids = [str(x).strip() for x in (raw_ids or []) if str(x).strip()]
                legacy = str(event.get("resource_id", "") or "").strip()
                if legacy and legacy not in resource_ids:
                    resource_ids.insert(0, legacy)
                if resource_type == "corridor":
                    resource_ids = [self._normalise_corridor_resource(x) for x in resource_ids]
                    resource_ids = [x for x in resource_ids if x]
                resource_ids = list(dict.fromkeys(resource_ids))
                events.append({
                    "resource_type": resource_type,
                    "resource_ids": resource_ids,
                    "resource_id": resource_ids[0] if resource_ids else "",
                    "start_time": str(event.get("start_time", "00:00") or "00:00"),
                    "end_time": str(event.get("end_time", "24:00") or "24:00"),
                    "availability_percent": max(0.0, min(100.0, float(event.get("availability_percent", 0.0) or 0.0))),
                    "speed_factor": max(0.0, min(1.0, float(event.get("speed_factor", 1.0) or 1.0))),
                    "days_active": list(event.get("days_active", ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]) or []),
                    "notes": str(event.get("notes", "") or ""),
                })
            clean.append({
                "name": str(scenario.get("name", f"Scenario {index}") or f"Scenario {index}"),
                "description": str(scenario.get("description", "") or ""),
                "events": events,
            })
        cfg["scenarios"] = clean

    def ensure_amr_defaults(self) -> None:
        for amr in self.data.setdefault("amrs", []):
            slots = normalise_amr_payload_slots(amr)
            amr["payload_slots"] = slots

            # Keep the legacy top-level fields populated from the first slot so older
            # simulator/reporting code continues to read a sensible single-slot value.
            primary = slots[0]
            amr["payload_capacity_kg"] = float(primary.get("payload_capacity_kg", 0.0))
            amr["payload_length_capacity_m"] = float(
                primary.get("payload_length_capacity_m", 0.0)
            )
            amr["payload_width_capacity_m"] = float(
                primary.get("payload_width_capacity_m", 0.0)
            )
            amr["payload_height_capacity_m"] = float(
                primary.get("payload_height_capacity_m", 0.0)
            )
            amr["manual_task_compatible"] = len(slots) == 1
            amr["multi_stop_enabled"] = bool(
                amr.get("multi_stop_enabled", len(slots) > 1) and len(slots) > 1
            )

    def ensure_delivery_resource_defaults(self) -> None:
        clean_staff = []
        for index, item in enumerate(self.data.setdefault("staff_delivery_resources", []), start=1):
            if isinstance(item, dict):
                clean_staff.append(normalise_staff_delivery_resource(item, index))
        self.data["staff_delivery_resources"] = clean_staff
        for task in self.data.setdefault("tasks", []):
            if isinstance(task, dict):
                task["delivery_resource"] = normalise_delivery_resource_policy(
                    task.get("delivery_resource", task.get("delivery_method", "amr"))
                )

    def has_manual_task_compatible_amr(self) -> bool:
        self.ensure_amr_defaults()
        return any(amr_supports_manual_tasks(amr) for amr in self.data.get("amrs", []))

    def multi_stop_enabled_amrs(self) -> list:
        self.ensure_amr_defaults()
        return [
            amr
            for amr in self.data.get("amrs", [])
            if bool(amr.get("multi_stop_enabled", False))
            and len(normalise_amr_payload_slots(amr)) > 1
        ]

    def ensure_task_generation_defaults(self) -> dict:
        self.data["task_generation"] = merge_task_generation_defaults(
            self.data.get("task_generation", {})
        )
        return self.data["task_generation"]

    def task_generation(self) -> dict:
        return self.ensure_task_generation_defaults()

    def set_task_generation(self, value: dict) -> None:
        self.data["task_generation"] = merge_task_generation_defaults(value)

    @classmethod
    def from_file(cls, path: str) -> "JsonStore":
        with open(path, "r", encoding="utf-8") as f:
            return cls(json.load(f))

    def save(self, path: str) -> None:
        self.ensure_simulation_defaults()
        self.ensure_amr_defaults()
        self.ensure_delivery_resource_defaults()
        self.ensure_task_generation_defaults()
        self.ensure_department_defaults()
        self.ensure_mass_collection_defaults()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)

    def floor_dxf_path(self, floor: int) -> Optional[str]:
        for entry in self.data.get("floor_dxf_files", []):
            try:
                if int(entry.get("floor")) == int(floor):
                    path = (entry.get("filepath") or "").strip()
                    return path or None
            except Exception:
                continue
        return None

    def charge_locations(self) -> list:
        building = self.data.setdefault("building", {})
        locations = building.get("charge_locations")

        if isinstance(locations, list):
            return [str(x).strip() for x in locations if str(x).strip()]

        legacy = str(building.get("charge_location", "")).strip()
        return [legacy] if legacy else []

    def set_charge_locations(self, locations: list) -> None:
        self.data.setdefault("building", {})["charge_locations"] = [
            str(x).strip() for x in locations if str(x).strip()
        ]
        self.data["building"].pop("charge_location", None)

    def set_floor_dxf_path(self, floor: int, filepath: str) -> None:
        entries = self.data.setdefault("floor_dxf_files", [])
        payload = {"floor": int(floor), "filepath": str(filepath)}
        for entry in entries:
            try:
                if int(entry.get("floor")) == int(floor):
                    entry.clear()
                    entry.update(payload)
                    return
            except Exception:
                continue
        entries.append(payload)
        entries.sort(key=lambda item: int(item.get("floor", 0)))

    def clear_floor_dxf_path(self, floor: int) -> None:
        self.data["floor_dxf_files"] = [
            entry
            for entry in self.data.get("floor_dxf_files", [])
            if int(entry.get("floor", -(10**9))) != int(floor)
        ]

    def names_in_use(self) -> set:
        names = set()
        for loc in self.data.get("locations", []):
            names.add(loc["name"])
        for node in self.data.get("corridors", {}).get("nodes", []):
            names.add(node["name"])
        for dept in self.data.get("departments", []):
            name = str(dept.get("name", "")).strip()
            if name:
                names.add(name)
        for lift in self.data.get("lifts", []):
            for floor_str in lift.get("floor_locations", {}):
                names.add(f"{lift['id']}-F{floor_str}")
        return names

    def all_points(self) -> Dict[str, dict]:
        result = {}
        for item in self.data.get("locations", []):
            result[item["name"]] = {**item, "kind": "location"}
        for item in self.data.get("corridors", {}).get("nodes", []):
            result[item["name"]] = {**item, "kind": "corridor_node"}
        for lift in self.data.get("lifts", []):
            lift_id = lift["id"]
            for floor_str, pos in lift.get("floor_locations", {}).items():
                result[f"{lift_id}-F{floor_str}"] = {
                    "name": f"{lift_id}-F{floor_str}",
                    "floor": int(floor_str),
                    "x": pos["x"],
                    "y": pos["y"],
                    "kind": "lift_node",
                    "lift_id": lift_id,
                }

        for dept in self.data.get("departments", []):
            name = str(dept.get("name", "")).strip()
            if not name:
                continue
            try:
                floor = int(dept.get("floor", 0))
                x = float(dept.get("x", 0.0))
                y = float(dept.get("y", 0.0))
            except Exception:
                continue

            result[name] = {
                "name": name,
                "floor": floor,
                "x": x,
                "y": y,
                "kind": "department",
                "department_id": str(dept.get("id", "")).strip() or name,
            }

        return result

    def points_for_floor(self, floor: int) -> Dict[str, dict]:
        return {
            name: point
            for name, point in self.all_points().items()
            if int(point["floor"]) == int(floor)
        }

    def locations_for_floor(self, floor: int) -> List[dict]:
        return [
            x for x in self.data.get("locations", []) if int(x["floor"]) == int(floor)
        ]

    def corridor_nodes_for_floor(self, floor: int) -> List[dict]:
        return [
            x
            for x in self.data.get("corridors", {}).get("nodes", [])
            if int(x["floor"]) == int(floor)
        ]

    def lift_nodes_for_floor(self, floor: int) -> List[dict]:
        result = []
        for lift in self.data.get("lifts", []):
            key = str(floor)
            if key in lift.get("floor_locations", {}):
                pos = lift["floor_locations"][key]
                result.append(
                    {
                        "name": f"{lift['id']}-F{floor}",
                        "floor": floor,
                        "x": pos["x"],
                        "y": pos["y"],
                        "kind": "lift_node",
                        "lift_id": lift["id"],
                    }
                )
        return result

    def edges_for_floor(self, floor: int) -> List[dict]:
        points = self.all_points()
        edges = []
        for edge in self.data.get("corridors", {}).get("edges", []):
            a = points.get(edge["from"])
            b = points.get(edge["to"])
            if a and b and int(a["floor"]) == floor and int(b["floor"]) == floor:
                edges.append(edge)
        return edges

    def add_corridor_node(self, name: str, floor: int, x: float, y: float) -> None:
        default_door_width = float(self.data.get("building", {}).get("default_door_clear_width_m", 0.9) or 0.9)
        self.data["corridors"]["nodes"].append(
            {
                "name": name,
                "floor": floor,
                "x": round(x, 3),
                "y": round(y, 3),
                "has_door": False,
                "door_clear_width_m": default_door_width,
            }
        )

    def add_location(self, name: str, floor: int, x: float, y: float) -> None:
        self.data["locations"].append(
            {
                "name": name,
                "floor": floor,
                "x": round(x, 3),
                "y": round(y, 3),
                "bounding_box": [],
                "wash_cycle_required": False,
                "wash_cycle_duration_sec": 300.0,
                "wash_location": "",
                "people_area_type": "none",
            }
        )

    def set_location_bounding_box(self, location_name: str, points: list) -> None:
        for item in self.data.get("locations", []):
            if item.get("name") == location_name:
                lx = float(item.get("x", 0.0))
                ly = float(item.get("y", 0.0))
                item["bounding_box"] = [
                    {
                        "dx": round(float(p["x"]) - lx, 3),
                        "dy": round(float(p["y"]) - ly, 3),
                    }
                    for p in points
                ]
                return

    def location_bounding_box_metrics(self, location_name: str) -> dict:
        points = self.get_location_bounding_box_points(location_name)
        if len(points) < 3:
            return {"length": 0.0, "width": 0.0, "area": 0.0}

        xs = [float(p["x"]) for p in points]
        ys = [float(p["y"]) for p in points]

        length = max(xs) - min(xs)
        width = max(ys) - min(ys)

        area = 0.0
        for i, p1 in enumerate(points):
            p2 = points[(i + 1) % len(points)]
            area += (float(p1["x"]) * float(p2["y"])) - (
                float(p2["x"]) * float(p1["y"])
            )
        area = abs(area) / 2.0

        return {
            "length": round(length, 3),
            "width": round(width, 3),
            "area": round(area, 3),
        }

    def remove_location_bounding_box(self, location_name: str) -> None:
        for item in self.data.get("locations", []):
            if item.get("name") == location_name:
                item.pop("bounding_box", None)
                return

    def get_location_bounding_box_points(self, location_name: str) -> list:
        for item in self.data.get("locations", []):
            if item.get("name") == location_name:
                lx = float(item.get("x", 0.0))
                ly = float(item.get("y", 0.0))

                points = []
                for p in item.get("bounding_box", []):
                    if "dx" in p and "dy" in p:
                        points.append(
                            {
                                "x": round(lx + float(p["dx"]), 3),
                                "y": round(ly + float(p["dy"]), 3),
                            }
                        )
                    else:
                        # Backwards compatibility for old absolute boxes
                        points.append(
                            {
                                "x": round(float(p["x"]), 3),
                                "y": round(float(p["y"]), 3),
                            }
                        )
                return points

        return []

    def get_location(self, location_name: str) -> Optional[dict]:
        for item in self.data.get("locations", []):
            if item.get("name") == location_name:
                return item
        return None

    def get_location_inventory_spaces(self, location_name: str) -> list:
        location = self.get_location(location_name)
        if not location:
            return []
        return deepcopy(location.get("inventory_spaces", []))

    def set_location_inventory_spaces(self, location_name: str, spaces: list) -> None:
        location = self.get_location(location_name)
        if not location:
            return

        lx = float(location.get("x", 0.0))
        ly = float(location.get("y", 0.0))

        clean_spaces = []
        for idx, space in enumerate(spaces, start=1):
            name = str(space.get("name", "")).strip() or f"Inventory {idx}"
            points = []

            for p in space.get("points", []):
                if "dx" in p and "dy" in p:
                    points.append(
                        {
                            "dx": round(float(p["dx"]), 3),
                            "dy": round(float(p["dy"]), 3),
                        }
                    )
                else:
                    points.append(
                        {
                            "dx": round(float(p["x"]) - lx, 3),
                            "dy": round(float(p["y"]) - ly, 3),
                        }
                    )

            payload_slots = []
            for slot in space.get("payload_slots", []):
                if not isinstance(slot, dict):
                    continue
                slot_type = str(slot.get("slot_type", "") or "").strip().lower()
                clean_slot = {
                    "rotation_deg": round(
                        float(slot.get("rotation_deg", 0.0) or 0.0), 3
                    ),
                }
                if slot_type == "amr" or str(slot.get("amr_type", "") or "").strip():
                    amr_type = str(slot.get("amr_type", slot.get("amr", "")) or "").strip()
                    if not amr_type:
                        continue
                    clean_slot["slot_type"] = "amr"
                    clean_slot["amr_type"] = amr_type
                else:
                    payload_name = str(slot.get("payload", "")).strip()
                    if not payload_name:
                        continue
                    clean_slot["payload"] = payload_name
                if "dx" in slot and "dy" in slot:
                    clean_slot["dx"] = round(float(slot.get("dx", 0.0)), 3)
                    clean_slot["dy"] = round(float(slot.get("dy", 0.0)), 3)
                else:
                    clean_slot["dx"] = round(float(slot.get("x", lx)) - lx, 3)
                    clean_slot["dy"] = round(float(slot.get("y", ly)) - ly, 3)
                payload_slots.append(clean_slot)

            if len(points) >= 3:
                clean_space = {
                    "name": name,
                    "points": points,
                    "payload_slots": payload_slots,
                }
                if bool(space.get("stores_amr", False)) or str(space.get("space_type", "") or "").strip().lower() == "amr":
                    clean_space["space_type"] = "amr"
                    clean_space["stores_amr"] = True
                for runtime_key in ("amr_id", "occupied", "reserved_by_amr", "timestamp"):
                    if runtime_key in space:
                        clean_space[runtime_key] = space.get(runtime_key)
                clean_spaces.append(clean_space)

        location["inventory_spaces"] = clean_spaces

    def inventory_space_points_absolute(self, location_name: str, space: dict) -> list:
        location = self.get_location(location_name)
        if not location:
            return []

        lx = float(location.get("x", 0.0))
        ly = float(location.get("y", 0.0))

        result = []
        for p in space.get("points", []):
            if "dx" in p and "dy" in p:
                result.append(
                    {
                        "x": round(lx + float(p["dx"]), 3),
                        "y": round(ly + float(p["dy"]), 3),
                    }
                )
            else:
                result.append(
                    {
                        "x": round(float(p["x"]), 3),
                        "y": round(float(p["y"]), 3),
                    }
                )

        return result

    def is_department_point(self, name: str) -> bool:
        for dept in self.data.get("departments", []):
            if str(dept.get("name", "")).strip() == str(name).strip():
                return True
        return False

    def add_edge(self, from_name: str, to_name: str) -> None:
        if self.is_department_point(from_name) or self.is_department_point(to_name):
            return
        edges = self.data["corridors"]["edges"]
        if not any(e["from"] == from_name and e["to"] == to_name for e in edges):
            default_width = float(self.data.get("building", {}).get("default_corridor_width_m", 2.4) or 2.4)
            edges.append({
                "from": from_name,
                "to": to_name,
                "bidirectional": True,
                "width_m": default_width,
                "people_area_type": "none",
                "people_profile_ids": [],
            })

    def remove_edge(self, from_name: str, to_name: str) -> None:
        self.data["corridors"]["edges"] = [
            e
            for e in self.data["corridors"]["edges"]
            if not (e["from"] == from_name and e["to"] == to_name)
        ]

    def set_point_position(self, name: str, x: float, y: float) -> None:
        x = round(x, 3)
        y = round(y, 3)
        for item in self.data.get("locations", []):
            if item["name"] == name:
                item["x"] = x
                item["y"] = y
                return
        for item in self.data.get("corridors", {}).get("nodes", []):
            if item["name"] == name:
                item["x"] = x
                item["y"] = y
                return
        for item in self.data.get("departments", []):
            if str(item.get("name", "")) == name:
                item["x"] = x
                item["y"] = y
                return

        if "-F" in name:
            lift_id, floor_text = name.rsplit("-F", 1)
            for lift in self.data.get("lifts", []):
                if lift["id"] == lift_id and floor_text in lift.get(
                    "floor_locations", {}
                ):
                    lift["floor_locations"][floor_text]["x"] = x
                    lift["floor_locations"][floor_text]["y"] = y
                    return

    def rename_point(self, old_name: str, new_name: str) -> None:
        if old_name == new_name:
            return
        for item in self.data.get("locations", []):
            if item["name"] == old_name:
                item["name"] = new_name
        for item in self.data.get("corridors", {}).get("nodes", []):
            if item["name"] == old_name:
                item["name"] = new_name
        for edge in self.data.get("corridors", {}).get("edges", []):
            if edge["from"] == old_name:
                edge["from"] = new_name
            if edge["to"] == old_name:
                edge["to"] = new_name
        for item in self.data.get("departments", []):
            if str(item.get("name", "")) == old_name:
                item["name"] = new_name
            item["waste_pickup_locations"] = [
                new_name if x == old_name else x
                for x in item.get("waste_pickup_locations", [])
            ]
            waste_cfg = item.get("waste", {}) or {}
            if waste_cfg.get("pickup_location") == old_name:
                waste_cfg["pickup_location"] = new_name
            if waste_cfg.get("dropoff_location") == old_name:
                waste_cfg["dropoff_location"] = new_name
        for task in self.data.get("tasks", []):
            if task.get("pickup") == old_name:
                task["pickup"] = new_name
            if task.get("dropoff") == old_name:
                task["dropoff"] = new_name
        for profile in self.data.get("route_profiles", {}).values():
            profile["allowed_nodes"] = [
                new_name if x == old_name else x
                for x in profile.get("allowed_nodes", [])
            ]
            profile["allowed_edges"] = [
                [new_name if part == old_name else part for part in edge_pair]
                for edge_pair in profile.get("allowed_edges", [])
            ]

        def rename_corridor_resource(value):
            text = str(value or "").strip().replace("<->", "->")
            parts = [x.strip() for x in text.split("->") if x.strip()]
            if len(parts) < 2:
                return new_name if text == old_name else text
            parts = [new_name if part == old_name else part for part in parts[:2]]
            return f"{parts[0]} -> {parts[1]}"

        for movement in self.data.get("people_movements", []):
            if movement.get("start_location") == old_name:
                movement["start_location"] = new_name
            if movement.get("end_location") == old_name:
                movement["end_location"] = new_name
            movement["corridor_edges"] = [
                rename_corridor_resource(value)
                for value in movement.get("corridor_edges", []) or []
            ]

        scenario_cfg = self.data.get("scenario_testing", {}) or {}
        for scenario in scenario_cfg.get("scenarios", []) or []:
            for event in scenario.get("events", []) or []:
                resource_type = str(event.get("resource_type", "") or "").lower()
                resource_ids = list(event.get("resource_ids", []) or [])
                legacy = str(event.get("resource_id", "") or "").strip()
                if legacy and legacy not in resource_ids:
                    resource_ids.insert(0, legacy)
                if resource_type == "corridor":
                    resource_ids = [rename_corridor_resource(value) for value in resource_ids]
                elif resource_type == "corridor_node":
                    resource_ids = [
                        new_name if str(value) == old_name else str(value)
                        for value in resource_ids
                    ]
                event["resource_ids"] = resource_ids
                event["resource_id"] = resource_ids[0] if resource_ids else ""


    def _remove_location_name_from_value(self, value, location_name: str):
        """Return (cleaned_value, removed_count) for location reference containers."""
        location_name = str(location_name or "").strip()
        if isinstance(value, list):
            cleaned = []
            removed = 0
            for item in value:
                if str(item).strip() == location_name:
                    removed += 1
                else:
                    cleaned.append(item)
            return cleaned, removed
        if isinstance(value, tuple):
            cleaned, removed = self._remove_location_name_from_value(list(value), location_name)
            return cleaned, removed
        if isinstance(value, str):
            return ("", 1) if value.strip() == location_name else (value, 0)
        return value, 0

    def _entry_has_location_references(self, entry) -> bool:
        if isinstance(entry, dict):
            for key in (
                "pickup_dropoff_locations",
                "locations",
                "pickup_locations",
                "dropoff_locations",
                "pickup_location",
                "dropoff_location",
                "location",
            ):
                value = entry.get(key)
                if isinstance(value, list) and any(str(x).strip() for x in value):
                    return True
                if isinstance(value, str) and value.strip():
                    return True
            return False
        if isinstance(entry, list):
            return any(str(x).strip() for x in entry)
        if isinstance(entry, str):
            return bool(entry.strip())
        return False

    def remove_location_references(self, location_name: str) -> dict:
        """Remove references to a deleted location from departments and generators.

        This is intentionally conservative: it removes only references that are
        clearly location names, while preserving the department/category records
        themselves.
        """
        location_name = str(location_name or "").strip()
        result = {
            "location": location_name,
            "department_references_removed": 0,
            "task_generation_references_removed": 0,
            "building_references_removed": 0,
            "mass_collection_references_removed": 0,
        }
        if not location_name:
            return result

        # Department category location assignments, including the newer
        # pickup_dropoff_locations key and the legacy locations key.
        for dept in self.data.get("departments", []):
            task_locations = dept.get("task_generation_locations", {})
            if isinstance(task_locations, dict):
                remove_categories = []
                for category_key, entry in list(task_locations.items()):
                    removed_here = 0
                    if isinstance(entry, dict):
                        for key in (
                            "pickup_dropoff_locations",
                            "locations",
                            "pickup_locations",
                            "dropoff_locations",
                        ):
                            if key in entry:
                                entry[key], removed = self._remove_location_name_from_value(
                                    entry.get(key), location_name
                                )
                                removed_here += removed
                        for key in ("pickup_location", "dropoff_location", "location"):
                            if key in entry:
                                entry[key], removed = self._remove_location_name_from_value(
                                    entry.get(key), location_name
                                )
                                removed_here += removed

                        # Keep the two supported list keys synchronised when either exists.
                        primary = entry.get("pickup_dropoff_locations")
                        legacy = entry.get("locations")
                        if isinstance(primary, list) or isinstance(legacy, list):
                            merged = []
                            seen = set()
                            for value in (primary if isinstance(primary, list) else []):
                                text = str(value).strip()
                                if text and text not in seen:
                                    merged.append(value)
                                    seen.add(text)
                            for value in (legacy if isinstance(legacy, list) else []):
                                text = str(value).strip()
                                if text and text not in seen:
                                    merged.append(value)
                                    seen.add(text)
                            entry["pickup_dropoff_locations"] = list(merged)
                            entry["locations"] = list(merged)

                        if removed_here and not self._entry_has_location_references(entry):
                            remove_categories.append(category_key)
                    else:
                        cleaned, removed_here = self._remove_location_name_from_value(entry, location_name)
                        if removed_here:
                            if self._entry_has_location_references(cleaned):
                                task_locations[category_key] = cleaned
                            else:
                                remove_categories.append(category_key)

                    result["department_references_removed"] += removed_here

                for category_key in remove_categories:
                    task_locations.pop(category_key, None)

            # Legacy department-level waste routing fields.
            if "waste_pickup_locations" in dept:
                dept["waste_pickup_locations"], removed = self._remove_location_name_from_value(
                    dept.get("waste_pickup_locations", []), location_name
                )
                result["department_references_removed"] += removed

            waste_cfg = dept.get("waste", {})
            if isinstance(waste_cfg, dict):
                for key in ("pickup_location", "dropoff_location", "pickup_locations", "dropoff_locations"):
                    if key in waste_cfg:
                        waste_cfg[key], removed = self._remove_location_name_from_value(
                            waste_cfg.get(key), location_name
                        )
                        result["department_references_removed"] += removed

        # Top-level task-generation routing can also point at locations.
        task_generation = self.data.get("task_generation", {})
        categories = task_generation.get("categories", {}) if isinstance(task_generation, dict) else {}
        if isinstance(categories, dict):
            for category in categories.values():
                if not isinstance(category, dict):
                    continue
                for key in ("pickup_location", "dropoff_location", "pickup_locations", "dropoff_locations"):
                    if key in category:
                        category[key], removed = self._remove_location_name_from_value(
                            category.get(key), location_name
                        )
                        result["task_generation_references_removed"] += removed
                overrides = category.get("departments", {})
                if isinstance(overrides, dict):
                    for override in overrides.values():
                        if not isinstance(override, dict):
                            continue
                        for key in ("pickup_location", "dropoff_location", "pickup_locations", "dropoff_locations"):
                            if key in override:
                                override[key], removed = self._remove_location_name_from_value(
                                    override.get(key), location_name
                                )
                                result["task_generation_references_removed"] += removed

        # Charging locations and mass-collection definitions are also location references.
        building = self.data.get("building", {})
        if isinstance(building, dict):
            if "charge_locations" in building:
                building["charge_locations"], removed = self._remove_location_name_from_value(
                    building.get("charge_locations", []), location_name
                )
                result["building_references_removed"] += removed
            if str(building.get("charge_location", "")).strip() == location_name:
                building["charge_location"] = ""
                result["building_references_removed"] += 1

        kept_mass_collections = []
        for item in self.data.get("mass_collections", []):
            if isinstance(item, dict) and str(item.get("location", "")).strip() == location_name:
                result["mass_collection_references_removed"] += 1
                continue
            kept_mass_collections.append(item)
        if result["mass_collection_references_removed"]:
            self.data["mass_collections"] = kept_mass_collections

        return result

    def delete_point(self, name: str) -> dict:
        name = str(name or "").strip()
        cleanup = self.remove_location_references(name)

        self.data["locations"] = [
            x for x in self.data.get("locations", []) if x["name"] != name
        ]
        self.data["corridors"]["nodes"] = [
            x
            for x in self.data.get("corridors", {}).get("nodes", [])
            if x["name"] != name
        ]
        self.data["corridors"]["edges"] = [
            e
            for e in self.data.get("corridors", {}).get("edges", [])
            if e["from"] != name and e["to"] != name
        ]
        self.data["departments"] = [
            x
            for x in self.data.get("departments", [])
            if str(x.get("name", "")).strip() != name
        ]
        for profile in self.data.get("route_profiles", {}).values():
            profile["allowed_nodes"] = [
                x for x in profile.get("allowed_nodes", []) if x != name
            ]
            profile["allowed_edges"] = [
                pair for pair in profile.get("allowed_edges", []) if name not in pair
            ]
        return cleanup

    def suggest_next_department_id(self) -> str:
        nums = []
        for item in self.data.get("departments", []):
            dept_id = str(item.get("id", "")).strip().upper()
            if dept_id.startswith("D") and dept_id[1:].isdigit():
                nums.append(int(dept_id[1:]))
        next_num = (max(nums) + 1) if nums else 1
        return f"D{next_num}"

    def upsert_department(self, payload: dict) -> None:
        payload = dict(payload or {})
        payload.setdefault("operating_start_time", "00:00")
        if not str(payload.get("operating_end_time", "") or "").strip():
            calculated_operating_hours_per_day(payload)
        payload["hours_operated_per_day"] = calculated_operating_hours_per_day(payload)
        payload.setdefault("days_active", ["mon", "tue", "wed", "thu", "fri"])
        items = self.data.setdefault("departments", [])
        dept_id = str(payload.get("id", "")).strip()
        existing = next(
            (x for x in items if str(x.get("id", "")).strip() == dept_id),
            None,
        )
        if existing is None:
            items.append(payload)
        else:
            existing.clear()
            existing.update(payload)

    def delete_department(self, dept_id: str) -> None:
        self.data["departments"] = [
            x
            for x in self.data.get("departments", [])
            if str(x.get("id", "")).strip() != str(dept_id).strip()
        ]

    def upsert_waste_stream(self, payload: dict) -> None:
        items = self.data.setdefault("waste_streams", [])
        name = str(payload.get("name", "")).strip()
        existing = next(
            (x for x in items if str(x.get("name", "")).strip() == name),
            None,
        )
        if existing is None:
            items.append(payload)
        else:
            existing.clear()
            existing.update(payload)

    def delete_waste_stream(self, name: str) -> None:
        name = str(name).strip()
        self.data["waste_streams"] = [
            x
            for x in self.data.get("waste_streams", [])
            if str(x.get("name", "")).strip() != name
        ]
        for dept in self.data.get("departments", []):
            dept["waste_streams"] = [
                x for x in dept.get("waste_streams", []) if str(x).strip() != name
            ]

    def upsert_lift(
        self,
        lift_id: str,
        served_floors: List[int],
        floor_locations: Dict[int, Tuple[float, float]],
        speed_m_per_sec: float = 1.8,
        door_time_sec: float = 4,
        boarding_time_sec: float = 6,
        capacity_length_m: float = 1.0,
        capacity_width_m: float = 1.0,
        capacity_height_m: float = 2.0,
        car_mass_kg: float = 1200.0,
        counterweight_ratio: float = 0.5,
        travel_efficiency: float = 0.75,
        door_power_w: float = 800.0,
        standby_power_w: float = 120.0,
        regen_efficiency: float = 0.2,
        health_percent: float = 100.0,
        health_loss_per_journey_percent: float = 0.05,
        mean_time_between_failures_hours: float = 720.0,
        mean_time_to_repair_hours: float = 4.0,
        minimum_operational_health_percent: float = 20.0,
        health_speed_penalty_at_zero: float = 0.5,
        start_floor: int = 0,
    ) -> None:
        lift = None
        for existing in self.data["lifts"]:
            if existing["id"] == lift_id:
                lift = existing
                break

        floor_height_m = float(self.data.get("building", {}).get("floor_height_m", 4.0) or 4.0)
        payload = {
            "id": lift_id,
            "served_floors": sorted(served_floors),
            "speed_m_per_sec": round(float(speed_m_per_sec), 3),
            "speed_floors_per_sec": round(float(speed_m_per_sec) / max(floor_height_m, 1e-9), 3),
            "door_time_sec": door_time_sec,
            "boarding_time_sec": boarding_time_sec,
            "capacity_length_m": capacity_length_m,
            "capacity_width_m": capacity_width_m,
            "capacity_height_m": capacity_height_m,
            "car_mass_kg": car_mass_kg,
            "counterweight_ratio": counterweight_ratio,
            "travel_efficiency": travel_efficiency,
            "door_power_w": door_power_w,
            "standby_power_w": standby_power_w,
            "regen_efficiency": regen_efficiency,
            "health_percent": round(float(health_percent), 3),
            "health_loss_per_journey_percent": round(
                float(health_loss_per_journey_percent), 3
            ),
            "mean_time_between_failures_hours": round(
                float(mean_time_between_failures_hours), 3
            ),
            "mean_time_to_repair_hours": round(float(mean_time_to_repair_hours), 3),
            "minimum_operational_health_percent": round(float(minimum_operational_health_percent), 3),
            "health_speed_penalty_at_zero": round(float(health_speed_penalty_at_zero), 3),
            "start_floor": start_floor,
            "floor_locations": {
                str(f): {"x": round(pos[0], 3), "y": round(pos[1], 3)}
                for f, pos in floor_locations.items()
            },
        }
        if lift is None:
            self.data["lifts"].append(payload)
        else:
            lift.clear()
            lift.update(payload)

    def delete_lift(self, lift_id: str) -> None:
        names_to_delete = {
            f"{lift_id}-F{floor}"
            for lift in self.data.get("lifts", [])
            if lift["id"] == lift_id
            for floor in lift.get("floor_locations", {}).keys()
        }
        self.data["lifts"] = [
            x for x in self.data.get("lifts", []) if x["id"] != lift_id
        ]
        self.data["corridors"]["edges"] = [
            e
            for e in self.data.get("corridors", {}).get("edges", [])
            if e["from"] not in names_to_delete and e["to"] not in names_to_delete
        ]
        for profile in self.data.get("route_profiles", {}).values():
            profile["allowed_lifts"] = [
                x for x in profile.get("allowed_lifts", []) if x != lift_id
            ]
            profile["allowed_nodes"] = [
                x for x in profile.get("allowed_nodes", []) if x not in names_to_delete
            ]
            profile["allowed_edges"] = [
                pair
                for pair in profile.get("allowed_edges", [])
                if not any(name in pair for name in names_to_delete)
            ]


    def _graph_access_validation_errors(self) -> List[str]:
        """Validate that every configured location is connected to the route graph.

        This is an editor-side structural check only.  It does not apply AMR
        payload dimensions, route profiles, lift capacity, battery state or
        congestion.  It confirms that each location can reach the wider graph
        using same-floor corridor edges and lift floor links.
        """
        errors: List[str] = []
        locations = [x for x in self.data.get("locations", []) if str(x.get("name", "")).strip()]
        if not locations:
            return errors

        points = self.all_points()
        adjacency: Dict[str, set] = {name: set() for name in points.keys()}

        def _point_floor(name: str):
            item = points.get(name)
            if not item:
                return None
            try:
                return int(item.get("floor", 0))
            except Exception:
                return None

        for edge in self.data.get("corridors", {}).get("edges", []):
            a = str(edge.get("from", "")).strip()
            b = str(edge.get("to", "")).strip()
            if a not in points or b not in points:
                continue
            fa = _point_floor(a)
            fb = _point_floor(b)
            if fa is None or fb is None:
                continue
            if fa != fb:
                # Cross-floor movement should be via lift floor nodes, not raw
                # corridor edges between floors.  The existing validation already
                # reports unknown endpoints; this check ignores invalid graph
                # edges rather than making them appear connected.
                continue
            adjacency.setdefault(a, set()).add(b)
            if bool(edge.get("bidirectional", True)):
                adjacency.setdefault(b, set()).add(a)

        for lift in self.data.get("lifts", []):
            lift_id = str(lift.get("id", "")).strip()
            lift_node_names = []
            for floor_key in (lift.get("floor_locations", {}) or {}).keys():
                node_name = f"{lift_id}-F{floor_key}"
                if node_name in points:
                    lift_node_names.append(node_name)
            # Lift travel makes each floor node for the same lift mutually
            # reachable.  Same-floor corridor edges still need to connect
            # locations/corridors to those lift nodes.
            for i, a in enumerate(lift_node_names):
                for b in lift_node_names[i + 1:]:
                    adjacency.setdefault(a, set()).add(b)
                    adjacency.setdefault(b, set()).add(a)

        def _walk(start: str) -> set:
            seen = set()
            stack = [start]
            while stack:
                node = stack.pop()
                if node in seen:
                    continue
                seen.add(node)
                stack.extend(sorted(adjacency.get(node, set()) - seen))
            return seen

        graph_node_names = {
            name
            for name, item in points.items()
            if item.get("kind") in {"corridor_node", "lift_node"}
        }
        location_names = {str(x.get("name", "")).strip() for x in locations}
        routable_targets = graph_node_names | location_names

        component_cache: Dict[str, set] = {}
        for location in locations:
            location_name = str(location.get("name", "")).strip()
            if location_name not in points:
                errors.append(f"Location {location_name} is not present in the editor point map")
                continue
            if location_name not in component_cache:
                component_cache[location_name] = _walk(location_name)
            reachable = component_cache[location_name]
            reachable_others = (reachable & routable_targets) - {location_name}
            if not reachable_others:
                floor = location.get("floor", "?")
                errors.append(
                    f"Location {location_name} on floor {floor} is not connected to the route graph; add an edge from it to a corridor node or lift node"
                )
                continue
            # If there are corridor or lift nodes in the model, require the
            # location to connect to that route infrastructure, not just another
            # isolated location-to-location island.
            if graph_node_names and not (reachable & graph_node_names):
                floor = location.get("floor", "?")
                errors.append(
                    f"Location {location_name} on floor {floor} can only reach other locations, not the corridor/lift graph; connect it to a corridor node or lift node"
                )

        if len(locations) > 1:
            first = str(locations[0].get("name", "")).strip()
            all_reachable = component_cache.get(first) or _walk(first)
            unreachable_locations = sorted(location_names - (all_reachable & location_names))
            if unreachable_locations:
                sample = ", ".join(unreachable_locations[:12])
                if len(unreachable_locations) > 12:
                    sample += f", +{len(unreachable_locations) - 12} more"
                errors.append(
                    "Location graph is split into separate islands; not all locations can route to each other. "
                    f"Unreachable from {first}: {sample}"
                )

        return errors


    def _space_points_dimensions_for_location(self, location: dict, space: dict) -> Tuple[float, float]:
        lx = float(location.get("x", 0.0) or 0.0)
        ly = float(location.get("y", 0.0) or 0.0)
        xs = []
        ys = []
        for point in space.get("points", []) or []:
            try:
                if "dx" in point and "dy" in point:
                    xs.append(lx + float(point.get("dx", 0.0) or 0.0))
                    ys.append(ly + float(point.get("dy", 0.0) or 0.0))
                else:
                    xs.append(float(point.get("x", lx) or lx))
                    ys.append(float(point.get("y", ly) or ly))
            except Exception:
                continue
        if not xs or not ys:
            return 0.0, 0.0
        return abs(max(xs) - min(xs)), abs(max(ys) - min(ys))

    def _inventory_space_accepts_amr_type(self, location: dict, space: dict, amr_type: dict) -> bool:
        if not isinstance(space, dict):
            return False
        stores_amr = bool(space.get("stores_amr", False)) or str(space.get("space_type", "") or "").strip().lower() == "amr"
        slot_amr_type = str(space.get("amr_type", "") or "").strip()
        for slot in space.get("payload_slots", []) or []:
            if not isinstance(slot, dict):
                continue
            if str(slot.get("slot_type", "") or "").strip().lower() == "amr" or str(slot.get("amr_type", "") or "").strip():
                stores_amr = True
                slot_amr_type = slot_amr_type or str(slot.get("amr_type", "") or "").strip()
        if not stores_amr:
            return False
        amr_id = str(amr_type.get("id", "") or "").strip()
        if slot_amr_type and slot_amr_type != amr_id:
            return False
        point_length, point_width = self._space_points_dimensions_for_location(location, space)
        length_m = float(space.get("length_m", space.get("length", point_length)) or point_length or 0.0)
        width_m = float(space.get("width_m", space.get("width", point_width)) or point_width or 0.0)
        height_m = float(space.get("height_m", space.get("height", 999999.0)) or 999999.0)
        amr_length = float(amr_type.get("length_m", 0.8) or 0.8)
        amr_width = float(amr_type.get("width_m", 0.6) or 0.6)
        amr_height = float(amr_type.get("height_m", 1.2) or 1.2)
        # Inventory spaces are often stored as rounded DXF/editor coordinates,
        # so a nominal 1.2 m × 0.6 m AMR bay can become
        # 1.2000000000000028 m × 0.5999999999999943 m after JSON round-trips.
        # Use a small tolerance so validation counts the intended bay instead of
        # falsely rejecting it as fractionally too small.
        tolerance_m = 1e-3

        def _fits(required: float, available: float) -> bool:
            return float(required) <= (float(available) + tolerance_m)

        fits_normal = _fits(amr_length, length_m) and _fits(amr_width, width_m)
        fits_rotated = _fits(amr_length, width_m) and _fits(amr_width, length_m)
        return (fits_normal or fits_rotated) and _fits(amr_height, height_m)

    def _amr_charge_space_validation_errors(self) -> List[str]:
        errors = []
        locations = self.data.get("locations", []) or []
        locations_by_name = {str(loc.get("name", "") or "").strip(): loc for loc in locations}
        building = self.data.get("building", {}) or {}
        charge_locations = building.get("charge_locations")
        if isinstance(charge_locations, str):
            charge_locations = [x.strip() for x in charge_locations.split(",")]
        if not isinstance(charge_locations, list) or not charge_locations:
            legacy = str(building.get("charge_location", "") or "").strip()
            charge_locations = [legacy] if legacy else []
        charge_locations = [str(x).strip() for x in charge_locations if str(x).strip()]
        if not charge_locations:
            return errors

        for loc_name in charge_locations:
            if loc_name not in locations_by_name and loc_name not in self.names_in_use():
                errors.append(f"Charging location not found: {loc_name}")

        charge_location_dicts = [locations_by_name[name] for name in charge_locations if name in locations_by_name]
        for amr_type in self.data.get("amrs", []) or []:
            amr_id = str(amr_type.get("id", "") or "").strip() or "AMR"
            try:
                required = max(0, int(float(amr_type.get("quantity", 1) or 1)))
            except Exception:
                required = 1
            compatible = 0
            compatible_by_location = {}
            configured_slots_by_location = {}
            for location in charge_location_dicts:
                location_name = str(location.get("name", "") or "").strip()
                for space in location.get("inventory_spaces", []) or []:
                    slot_mentions_amr = False
                    for slot in space.get("payload_slots", []) or []:
                        if not isinstance(slot, dict):
                            continue
                        if (
                            str(slot.get("slot_type", "") or "").strip().lower() == "amr"
                            or str(slot.get("amr_type", "") or "").strip()
                        ):
                            slot_mentions_amr = True
                            break
                    if bool(space.get("stores_amr", False)) or str(space.get("space_type", "") or "").strip().lower() == "amr" or slot_mentions_amr:
                        configured_slots_by_location[location_name] = configured_slots_by_location.get(location_name, 0) + 1
                    if self._inventory_space_accepts_amr_type(location, space, amr_type):
                        compatible += 1
                        compatible_by_location[location_name] = compatible_by_location.get(location_name, 0) + 1
            if compatible < required:
                detail_parts = [
                    f"{name}: {compatible_by_location.get(name, 0)} compatible / {configured_slots_by_location.get(name, 0)} AMR slot(s)"
                    for name in charge_locations
                ]
                detail = "; ".join(detail_parts) if detail_parts else "no configured charging locations"
                errors.append(
                    f"AMR type {amr_id} requires {required} compatible charging inventory space(s), "
                    f"but only {compatible} found at configured charging location(s). {detail}."
                )
        return errors

    def people_movements(self) -> list:
        self.ensure_people_movement_defaults()
        return self.data.get("people_movements", [])

    def set_people_movements(self, movements: list) -> None:
        self.data["people_movements"] = list(movements or [])
        self.ensure_people_movement_defaults()

    def scenario_testing(self) -> dict:
        self.ensure_scenario_defaults()
        return self.data.get("scenario_testing", {})

    def set_scenario_testing(self, value: dict) -> None:
        self.data["scenario_testing"] = dict(value or {})
        self.ensure_scenario_defaults()

    def validate(self) -> List[str]:
        errors = []
        names = self.names_in_use()

        for edge in self.data.get("corridors", {}).get("edges", []):
            if edge["from"] not in names:
                errors.append(f"Unknown edge start: {edge['from']}")
            if edge["to"] not in names:
                errors.append(f"Unknown edge end: {edge['to']}")

        location_names = {x["name"] for x in self.data.get("locations", [])}
        payload_names = {x["name"] for x in self.data.get("payloads", [])}
        waste_stream_names = {x["name"] for x in self.data.get("waste_streams", [])}
        route_profile_names = set(self.data.get("route_profiles", {}).keys())
        lift_names = {x["id"] for x in self.data.get("lifts", [])}

        for task in self.data.get("tasks", []):
            raw_policy = task.get("delivery_resource", task.get("delivery_method", "amr"))
            raw_mode = (
                str(raw_policy.get("mode", "amr") or "amr").strip().lower()
                if isinstance(raw_policy, dict)
                else str(raw_policy or "amr").strip().lower()
            )
            if raw_mode not in DELIVERY_RESOURCE_MODES:
                errors.append(
                    f"Task {task.get('id')} has invalid delivery resource mode: {raw_mode}"
                )
            if (
                task.get("pickup") not in location_names
                and task.get("pickup") not in names
            ):
                errors.append(
                    f"Task {task.get('id')} pickup not found: {task.get('pickup')}"
                )
            if (
                task.get("dropoff") not in location_names
                and task.get("dropoff") not in names
            ):
                errors.append(
                    f"Task {task.get('id')} dropoff not found: {task.get('dropoff')}"
                )
            if task.get("payload") not in payload_names:
                errors.append(
                    f"Task {task.get('id')} payload not found: {task.get('payload')}"
                )
            rp = task.get("route_profile", "")
            if rp and rp not in route_profile_names:
                errors.append(f"Task {task.get('id')} route profile not found: {rp}")

        for profile_name, profile in self.data.get("route_profiles", {}).items():
            for lift_id in profile.get("allowed_lifts", []):
                if lift_id not in lift_names:
                    errors.append(
                        f"Route profile {profile_name} has unknown lift: {lift_id}"
                    )
            for node_name in profile.get("allowed_nodes", []):
                if node_name not in names and node_name not in location_names:
                    errors.append(
                        f"Route profile {profile_name} has unknown node: {node_name}"
                    )
            for edge_pair in profile.get("allowed_edges", []):
                if len(edge_pair) != 2:
                    errors.append(
                        f"Route profile {profile_name} has invalid edge pair: {edge_pair}"
                    )
                    continue
                if edge_pair[0] not in names or edge_pair[1] not in names:
                    errors.append(
                        f"Route profile {profile_name} has unknown edge endpoint: {edge_pair}"
                    )

        for stream in self.data.get("waste_streams", []):
            stream_name = str(stream.get("name", "")).strip()
            if not stream_name:
                errors.append("Waste stream has no name")
            payload_name = str(stream.get("payload", "")).strip()
            if payload_name not in payload_names:
                errors.append(
                    f"Waste stream {stream_name or '-'} payload not found: {payload_name}"
                )
            try:
                capacity = float(stream.get("container_capacity_m3", 0))
                if capacity <= 0:
                    errors.append(
                        f"Waste stream {stream_name or '-'} container capacity must be greater than 0"
                    )
            except Exception:
                errors.append(
                    f"Waste stream {stream_name or '-'} has invalid container capacity"
                )
            try:
                threshold = float(stream.get("full_threshold_fraction", 0))
                if not (0.0 < threshold <= 1.0):
                    errors.append(
                        f"Waste stream {stream_name or '-'} full threshold must be between 0 and 1"
                    )
            except Exception:
                errors.append(
                    f"Waste stream {stream_name or '-'} has invalid full threshold"
                )

        for dept in self.data.get("departments", []):
            dept_name = (
                str(dept.get("name", "")).strip() or str(dept.get("id", "")).strip()
            )
            waste = dept.get("waste", {}) or {}

            for loc in dept.get("waste_pickup_locations", []):
                if loc not in location_names:
                    errors.append(
                        f"Department {dept_name} has unknown waste pickup location: {loc}"
                    )

            for stream_item in dept.get("waste_streams", []):
                if isinstance(stream_item, dict):
                    stream_name = str(stream_item.get("name", "")).strip()
                else:
                    stream_name = str(stream_item).strip()

                if stream_name not in waste_stream_names:
                    errors.append(
                        f"Department {dept_name} has unknown waste stream: {stream_name}"
                    )

            pickup_location = str(waste.get("pickup_location", "")).strip()
            if pickup_location and pickup_location not in location_names:
                errors.append(
                    f"Department {dept_name} has unknown waste pickup location: {pickup_location}"
                )

            dropoff_location = str(waste.get("dropoff_location", "")).strip()
            if dropoff_location and dropoff_location not in location_names:
                errors.append(
                    f"Department {dept_name} has unknown waste dropoff location: {dropoff_location}"
                )

        for amr in self.data.get("amrs", []):
            # AMRs are placed at configured charging locations at runtime.
            # Legacy start_location values are ignored and no longer validated.
            slots = normalise_amr_payload_slots(amr)
            if not slots:
                errors.append(
                    f"AMR {amr.get('id')} must have at least one payload slot"
                )
            for slot in slots:
                slot_name = slot.get("name", "Slot")
                try:
                    if float(slot.get("payload_capacity_kg", 0.0)) <= 0:
                        errors.append(
                            f"AMR {amr.get('id')} {slot_name} payload kg must be greater than 0"
                        )
                    for key, label in [
                        ("payload_length_capacity_m", "length"),
                        ("payload_width_capacity_m", "width"),
                        ("payload_height_capacity_m", "height"),
                    ]:
                        if float(slot.get(key, 0.0)) <= 0:
                            errors.append(
                                f"AMR {amr.get('id')} {slot_name} {label} capacity must be greater than 0"
                            )
                except Exception:
                    errors.append(
                        f"AMR {amr.get('id')} {slot_name} has invalid payload slot dimensions"
                    )

        seen_staff_ids = set()
        for staff in self.data.get("staff_delivery_resources", []) or []:
            staff_id = str(staff.get("id", "") or "").strip()
            if not staff_id:
                errors.append("Staff delivery resource has no type name")
            elif staff_id in seen_staff_ids:
                errors.append(f"Duplicate staff delivery resource type: {staff_id}")
            seen_staff_ids.add(staff_id)
            base_location = str(staff.get("base_location", "") or "").strip()
            if not base_location:
                errors.append(
                    f"Staff delivery resource {staff_id or '-'} has no base location"
                )
            elif base_location not in location_names:
                errors.append(
                    f"Staff delivery resource {staff_id or '-'} has unknown base location: {base_location}"
                )
            try:
                if int(staff.get("quantity", 0) or 0) <= 0:
                    errors.append(
                        f"Staff delivery resource {staff_id or '-'} quantity must be greater than 0"
                    )
                if float(staff.get("speed_m_per_sec", 0.0) or 0.0) <= 0.0:
                    errors.append(
                        f"Staff delivery resource {staff_id or '-'} speed must be greater than 0"
                    )
                if float(staff.get("payload_capacity_kg", 0.0) or 0.0) <= 0.0:
                    errors.append(
                        f"Staff delivery resource {staff_id or '-'} payload capacity must be greater than 0"
                    )
            except Exception:
                errors.append(
                    f"Staff delivery resource {staff_id or '-'} has invalid numeric values"
                )

        seen_floors = set()
        for entry in self.data.get("floor_dxf_files", []):
            if not isinstance(entry, dict):
                errors.append(f"Invalid floor_dxf_files entry: {entry}")
                continue

            if "floor" not in entry:
                errors.append("DXF mapping is missing floor")
                continue

            if "filepath" not in entry:
                errors.append(
                    f"DXF mapping for floor {entry.get('floor')} is missing filepath"
                )
                continue

            try:
                floor = int(entry.get("floor"))
            except Exception:
                errors.append(f"DXF mapping has invalid floor: {entry.get('floor')}")
                continue

            filepath = str(entry.get("filepath") or "").strip()
            if not filepath:
                errors.append(f"DXF mapping for floor {floor} has empty filepath")

            if floor in seen_floors:
                errors.append(f"Duplicate DXF mapping for floor {floor}")
            seen_floors.add(floor)

        errors.extend(self._amr_charge_space_validation_errors())
        errors.extend(self._graph_access_validation_errors())

        return errors

    def suggest_next_corridor_name(self, floor: int) -> str:
        prefix = f"C{floor}-"
        nums = []
        for item in self.data.get("corridors", {}).get("nodes", []):
            name = item["name"]
            if name.startswith(prefix):
                tail = name[len(prefix) :]
                if tail.isdigit():
                    nums.append(int(tail))
        next_num = max(nums, default=0) + 1
        return f"C{floor}-{next_num}"

    def suggest_next_task_id(self) -> str:
        nums = []
        for task in self.data.get("tasks", []):
            task_id = str(task.get("id", ""))
            if task_id.startswith("T") and task_id[1:].isdigit():
                nums.append(int(task_id[1:]))
        return f"T{max(nums, default=0) + 1}"

    @staticmethod
    def basename(path: Optional[str]) -> str:
        if not path:
            return "New file"
        return Path(path).name
