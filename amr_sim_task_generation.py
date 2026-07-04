"""
Automatic task generation for the AMR simulator.

"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

from amr_sim_models import Location, PayloadType, Task
from amr_sim_time_utils import SimulationClock

DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
SCHEDULED_MODES = {"scheduled", "scheduled_threshold", "scheduled_sporadic"}
THRESHOLD_MODES = {"threshold", "hybrid", "scheduled_threshold"}
CONTINUOUS_MODES = {"continuous", "hybrid", "threshold", "scheduled_threshold"}
SPORADIC_MODES = {"sporadic", "hybrid", "scheduled_sporadic"}
TIMEFRAME_MODES = {"timeframe"}


@dataclass
class GeneratedTaskRecord:
    """A generated task plus logging metadata for the simulator."""

    task: Task
    event_type: str
    details: str
    pickup_location: str
    dropoff_location: str
    payload_name: str
    task_source: str = ""
    department_id: str = ""
    waste_stream: str = ""
    waste_volume_m3: float = 0.0
    container_type: str = ""


class BaseTaskGenerator:
    """Base class for runtime task generators."""

    generator_type = "base"

    def update_until(self, now: float) -> List[GeneratedTaskRecord]:
        return []

    def task_state_changed(self, task: Task, state: str) -> None:
        """Receive completion/failure notifications for generated tasks."""
        return None


def _clean_text(value) -> str:
    return str(value or "").strip()


def _as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _as_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return int(default)


def _as_bool(value, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on", "enabled", "enable"}:
        return True
    if text in {"0", "false", "no", "n", "off", "disabled", "disable"}:
        return False
    return bool(default)


def _unique_clean(values: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values or []:
        text = _clean_text(value)
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


def _scheduled_times_from_cfg(cfg: dict) -> List[str]:
    values = cfg.get("scheduled_times", cfg.get("schedule_times", []))
    if isinstance(values, str):
        values = [x.strip() for x in values.split(",")]
    clean = []
    for value in values or []:
        text = _clean_text(value)
        if not text:
            continue
        # Accept HH:MM and HH:MM:SS, normalise to HH:MM.
        try:
            parts = [int(x) for x in text.split(":")[:2]]
            if len(parts) == 2 and 0 <= parts[0] <= 23 and 0 <= parts[1] <= 59:
                clean.append(f"{parts[0]:02d}:{parts[1]:02d}")
        except Exception:
            continue
    return sorted(set(clean))


def _timeframe_minutes_from_cfg(cfg: dict) -> Tuple[Optional[int], Optional[int]]:
    start = _parse_hhmm_to_minutes(cfg.get("timeframe_start"), None)
    end = _parse_hhmm_to_minutes(cfg.get("timeframe_end"), None)
    return start, end


def _normalise_staff_shift_pattern(value: str) -> str:
    pattern = _clean_text(value).lower()
    if pattern in {
        "4_on_4_off_12h",
        "four_on_four_off",
        "four_on_four_off_12_hour",
    }:
        pattern = "four_on_four_off_12h"
    return pattern if pattern in {"none", "four_on_four_off_12h"} else "none"


def _normalise_global_staff_config(value: Optional[dict]) -> dict:
    source = value if isinstance(value, dict) else {}
    patterns = source.get("shift_patterns", {})
    if not isinstance(patterns, dict):
        patterns = {}

    defaults = {
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
            "days_active": DAY_KEYS,
            "work_days": 4,
            "rest_days": 4,
        },
    }

    clean_patterns: Dict[str, dict] = {}
    for pattern_key, fallback in defaults.items():
        incoming = patterns.get(pattern_key, {})
        incoming = incoming if isinstance(incoming, dict) else {}
        start_time = _clean_text(incoming.get("start_time", fallback["start_time"]))
        end_time = _clean_text(incoming.get("end_time", fallback["end_time"]))
        if _parse_hhmm_to_minutes(start_time, None) is None:
            start_time = fallback["start_time"]
        if _parse_hhmm_to_minutes(end_time, None) is None:
            end_time = fallback["end_time"]
        days = incoming.get("days_active", fallback["days_active"])
        if isinstance(days, str):
            days = [x.strip() for x in days.split(",")]
        clean_days = []
        for day in days or []:
            day_key = _clean_text(day).lower()[:3]
            if day_key in DAY_KEYS and day_key not in clean_days:
                clean_days.append(day_key)
        clean_patterns[pattern_key] = {
            "display_name": _clean_text(
                incoming.get("display_name", fallback["display_name"])
            )
            or fallback["display_name"],
            "start_time": start_time,
            "end_time": end_time,
            "days_active": clean_days or list(fallback["days_active"]),
            "work_days": max(
                0,
                _as_int(
                    incoming.get("work_days", fallback["work_days"]),
                    fallback["work_days"],
                ),
            ),
            "rest_days": max(
                0,
                _as_int(
                    incoming.get("rest_days", fallback["rest_days"]),
                    fallback["rest_days"],
                ),
            ),
        }

    return {
        "enabled": _as_bool(source.get("enabled", True), True),
        "spread_timeframe_tasks": _as_bool(
            source.get(
                "spread_timeframe_tasks",
                source.get("space_timeframe_tasks", True),
            ),
            True,
        ),
        "walking_speed_m_per_sec": max(
            0.1, _as_float(source.get("walking_speed_m_per_sec", 1.2), 1.2)
        ),
        "lift_wait_seconds": max(
            0.0, _as_float(source.get("lift_wait_seconds", 30.0), 30.0)
        ),
        "default_handling_minutes": max(
            0.0, _as_float(source.get("default_handling_minutes", 15.0), 15.0)
        ),
        "shift_patterns": clean_patterns,
    }


def _normalise_staff_weekly_hours(value) -> Dict[str, dict]:
    source = value if isinstance(value, dict) else {}
    clean: Dict[str, dict] = {}
    for day_key in DAY_KEYS:
        raw = source.get(day_key, {})
        raw = raw if isinstance(raw, dict) else {}
        enabled = _as_bool(raw.get("enabled", False), False)
        start_time = _clean_text(raw.get("start_time", "09:00")) or "09:00"
        end_time = _clean_text(raw.get("end_time", "17:00")) or "17:00"
        if _parse_hhmm_to_minutes(start_time, None) is None:
            start_time = "09:00"
        if _parse_hhmm_to_minutes(end_time, None) is None:
            end_time = "17:00"
        clean[day_key] = {
            "enabled": enabled,
            "start_time": start_time,
            "end_time": end_time,
        }
    return clean


def _day_key_for_datetime(value: datetime) -> str:
    return DAY_KEYS[value.weekday()]


def _datetime_for_day_and_hhmm(day_start: datetime, hhmm: str) -> datetime:
    hour, minute = [int(x) for x in hhmm.split(":")[:2]]
    return datetime.combine(day_start.date(), dt_time(hour=hour, minute=minute))


def _parse_hhmm_to_minutes(value, default: Optional[int] = None) -> Optional[int]:
    text = _clean_text(value)
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


def _minutes_to_time(value: int) -> dt_time:
    value = max(0, min(int(value), 24 * 60))
    if value >= 24 * 60:
        return dt_time(hour=23, minute=59, second=59)
    return dt_time(hour=value // 60, minute=value % 60)


def _department_operating_start_minutes(dept: dict) -> int:
    explicit = _parse_hhmm_to_minutes(dept.get("operating_start_time"), None)
    if explicit is not None:
        return explicit
    return 0


def _department_operating_end_minutes(dept: dict) -> int:
    explicit = _parse_hhmm_to_minutes(dept.get("operating_end_time"), None)
    if explicit is not None:
        return explicit
    start = _department_operating_start_minutes(dept)
    hours = _as_float(dept.get("hours_operated_per_day", 24.0), 24.0)
    if hours >= 24.0:
        return start + (24 * 60)
    return start + int(round(max(0.0, hours) * 60.0))


def _department_operating_hours_per_day(dept: dict) -> float:
    start = _department_operating_start_minutes(dept)
    end = _department_operating_end_minutes(dept)
    if end == start:
        return 24.0
    if end < start:
        end += 24 * 60
    return max(1.0, min((end - start) / 60.0, 24.0))


def _department_operating_periods_for_date(
    dept: dict, day: datetime
) -> List[Tuple[datetime, datetime]]:
    if not bool(dept.get("enabled", True)):
        return []

    active_days = dept.get("days_active", []) or []
    if active_days:
        allowed = {_clean_text(x).lower() for x in active_days if _clean_text(x)}
        if _day_key_for_datetime(day) not in allowed:
            return []

    start_min = _department_operating_start_minutes(dept)
    end_min = _department_operating_end_minutes(dept)
    if end_min <= start_min:
        end_min += 24 * 60

    duration_min = max(0, end_min - start_min)
    if duration_min <= 0:
        return []

    day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    start_dt = day_start + timedelta(minutes=start_min)
    end_dt = day_start + timedelta(minutes=end_min)
    return [(start_dt, end_dt)]


def _department_is_open_at_datetime(dept: Optional[dict], value: datetime) -> bool:
    if not dept:
        return True
    for offset in (-1, 0):
        base_day = (value + timedelta(days=offset)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        for start_dt, end_dt in _department_operating_periods_for_date(dept, base_day):
            if start_dt <= value < end_dt:
                return True
    return False


def _department_active_seconds_between(
    dept: Optional[dict], start_dt: datetime, end_dt: datetime
) -> float:
    if not dept:
        return max(0.0, (end_dt - start_dt).total_seconds())
    if end_dt <= start_dt:
        return 0.0

    total = 0.0
    cursor_day = (start_dt - timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    final_day = end_dt.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
        days=1
    )
    while cursor_day <= final_day:
        for period_start, period_end in _department_operating_periods_for_date(
            dept, cursor_day
        ):
            overlap_start = max(start_dt, period_start)
            overlap_end = min(end_dt, period_end)
            if overlap_end > overlap_start:
                total += (overlap_end - overlap_start).total_seconds()
        cursor_day += timedelta(days=1)
    return total


def _next_department_open_datetime(
    dept: Optional[dict], value: datetime
) -> Optional[datetime]:
    if not dept:
        return value
    if _department_is_open_at_datetime(dept, value):
        return value

    cursor_day = (value - timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    for _ in range(16):
        for start_dt, end_dt in _department_operating_periods_for_date(
            dept, cursor_day
        ):
            if value < start_dt:
                return start_dt
            if start_dt <= value < end_dt:
                return value
        cursor_day += timedelta(days=1)
    return None


def _payload_tracked_items(payload: Optional[PayloadType]) -> Dict[str, dict]:
    if payload is None or not getattr(payload, "track_items", False):
        return {}
    items = getattr(payload, "items", {}) or {}
    if not isinstance(items, dict):
        return {}

    result: Dict[str, dict] = {}
    for name, cfg in items.items():
        if not isinstance(cfg, dict):
            continue
        item_name = _clean_text(name)
        if not item_name:
            continue
        result[item_name] = {
            "target_quantity": _as_float(cfg.get("max", 100), 100),
            "trigger_quantity": _as_float(cfg.get("top_up_threshold", 15), 15),
            "usage_rate": _clean_text(cfg.get("usage_rate", "scheduled_sporadic"))
            or "scheduled_sporadic",
            "consumption_per_day": _as_float(cfg.get("consumption_per_day", 0.0), 0.0),
            "exchange_payload": _clean_text(cfg.get("exchange_payload", "")),
            "source_location": _clean_text(cfg.get("source_location", "")),
        }
    return result


def _apply_tracked_item_metadata(
    task: Task, cfg: dict, payloads: Dict[str, PayloadType]
) -> None:
    if not bool(cfg.get("tracked_item_exchange", False)):
        return

    base_payload_name = _clean_text(cfg.get("payload", task.payload))
    base_payload = payloads.get(base_payload_name)
    tracked_items = _payload_tracked_items(base_payload)
    if not tracked_items:
        return

    task.tracked_item_exchange = True
    task.exchange_mode = (
        _clean_text(cfg.get("exchange_mode", "top_up_only")) or "top_up_only"
    )
    task.tracked_item_source_payload = base_payload_name
    task.tracked_items = tracked_items

    # If every tracked item points to the same source or payload, use it as a more
    # specific task instruction. Mixed item sources remain as task metadata only.
    source_locations = {
        _clean_text(item.get("source_location", ""))
        for item in tracked_items.values()
        if _clean_text(item.get("source_location", ""))
    }
    exchange_payloads = {
        _clean_text(item.get("exchange_payload", ""))
        for item in tracked_items.values()
        if _clean_text(item.get("exchange_payload", ""))
    }

    if len(source_locations) == 1:
        task.pickup = next(iter(source_locations))
    if len(exchange_payloads) == 1:
        task.payload = next(iter(exchange_payloads))


class DynamicCategoryTaskGenerator(BaseTaskGenerator):
    """
    Generates tasks from task_generation.categories.

    This is the simulator-side counterpart of the editor's task generation dialog.
    It honours category defaults, department overrides and per-department location
    assignments created in the department dialog.
    """

    generator_type = "dynamic_category"

    def __init__(
        self,
        task_generation: dict,
        departments: Iterable[dict],
        locations: Dict[str, Location],
        payloads: Dict[str, PayloadType],
        clock: SimulationClock,
        waste_streams: Optional[Dict[str, dict]] = None,
    ):
        self.task_generation = task_generation or {}
        self.staff_config = _normalise_global_staff_config(
            self.task_generation.get(
                "staff_config", self.task_generation.get("staff", {})
            )
        )
        self.departments = list(departments or [])
        self.locations = locations or {}
        self.payloads = payloads or {}
        self.clock = clock
        self.waste_streams = waste_streams or {}
        self.task_counter = 0
        self.return_counter = 0
        self.scheduled_emitted = set()
        self.runtime: Dict[str, dict] = {}
        self.item_runtime: Dict[str, dict] = {}
        self.instances = self._build_instances()
        self._prepare_instance_runtime_fields()
        self._timeframe_group_members = self._build_timeframe_group_members()
        self._timeframe_allocation_cache: Dict[tuple, Tuple[int, int]] = {}

    def _next_task_id(self, category_key: str, department_id: str = "") -> str:
        self.task_counter += 1
        safe_cat = "".join(
            c if c.isalnum() else "_" for c in category_key.upper()
        ).strip("_")
        safe_dept = "".join(
            c if c.isalnum() else "_" for c in department_id.upper()
        ).strip("_")
        if safe_dept:
            return f"GEN_{safe_cat}_{safe_dept}_{self.task_counter:05d}"
        return f"GEN_{safe_cat}_{self.task_counter:05d}"

    def _next_return_task_id(self, outbound_id: str) -> str:
        self.return_counter += 1
        safe = "".join(c if c.isalnum() else "_" for c in outbound_id.upper()).strip(
            "_"
        )
        return f"RETURN_{safe}_{self.return_counter:05d}"

    def _category_is_active(self, cfg: dict, sim_time_sec: float) -> bool:
        if not _as_bool(cfg.get("enabled", False), False):
            return False
        active_days = cfg.get("days_active", []) or []
        if active_days:
            allowed = {_clean_text(x).lower() for x in active_days if _clean_text(x)}
            if self._day_key_for_sim_time(sim_time_sec) not in allowed:
                return False

        # Optional fortnightly recurrence. Week 0 starts at the simulation
        # start date, so checked tasks run in the first week, skip the next,
        # then repeat every other week. This applies to all dynamic category
        # generation modes because _category_is_active gates scheduled,
        # threshold, continuous, sporadic and tracked-item generation.
        if _as_bool(cfg.get("run_every_fortnight", False), False):
            current_day = self.clock.sim_seconds_to_datetime(sim_time_sec).date()
            start_day = self.clock.start_datetime.date()
            week_index = max(0, (current_day - start_day).days // 7)
            if week_index % 2 != 0:
                return False

        return True

    def _instance_department(self, instance: dict) -> Optional[dict]:
        dept = instance.get("department")
        return dept if isinstance(dept, dict) else None

    def _instance_is_active(self, instance: dict, sim_time_sec: float) -> bool:
        cfg = instance.get("cfg", {}) or {}
        if not self._category_is_active(cfg, sim_time_sec):
            return False
        dept = self._instance_department(instance)
        if dept is not None:
            return _department_is_open_at_datetime(
                dept, self.clock.sim_seconds_to_datetime(sim_time_sec)
            )
        return True

    def _instance_active_elapsed_seconds(
        self, instance: dict, start_sec: float, end_sec: float
    ) -> float:
        dept = self._instance_department(instance)
        return _department_active_seconds_between(
            dept,
            self.clock.sim_seconds_to_datetime(start_sec),
            self.clock.sim_seconds_to_datetime(end_sec),
        )

    def _adjust_release_to_department_open(
        self, instance: dict, release_time: float
    ) -> Optional[float]:
        dept = self._instance_department(instance)
        if dept is None:
            return release_time
        release_dt = self.clock.sim_seconds_to_datetime(release_time)
        adjusted = _next_department_open_datetime(dept, release_dt)
        if adjusted is None:
            return None
        return max(0.0, (adjusted - self.clock.start_datetime).total_seconds())

    def _day_key_for_sim_time(self, sim_time_sec: float) -> str:
        return _day_key_for_datetime(self.clock.sim_seconds_to_datetime(sim_time_sec))

    def _merge_category_with_override(
        self, category: dict, override: Optional[dict]
    ) -> dict:
        merged = dict(category or {})
        merged.pop("departments", None)
        if isinstance(override, dict):
            merged.update(override)
        return merged

    def _department_id(self, dept: dict) -> str:
        return _clean_text(dept.get("id")) or _clean_text(dept.get("name"))

    def _department_name(self, dept: dict) -> str:
        return _clean_text(dept.get("name")) or self._department_id(dept)

    def _department_category_locations(
        self, dept: dict, category_key: str
    ) -> List[str]:
        category_locations = dept.get("task_generation_locations", {}) or {}
        entry = category_locations.get(category_key, {}) or {}
        values = entry.get("pickup_dropoff_locations", [])
        return _unique_clean(values)

    def _dropoff_locations_from_cfg(self, cfg: dict) -> List[str]:
        values = []
        if isinstance(cfg.get("dropoff_locations"), list):
            values.extend(cfg.get("dropoff_locations", []))
        if cfg.get("dropoff_location"):
            values.insert(0, cfg.get("dropoff_location"))
        return _unique_clean(values)

    def _pickup_locations_from_cfg(self, cfg: dict) -> List[str]:
        values = []
        if isinstance(cfg.get("pickup_locations"), list):
            values.extend(cfg.get("pickup_locations", []))
        if cfg.get("pickup_location"):
            values.insert(0, cfg.get("pickup_location"))
        return _unique_clean(values)

    def _department_waste_stream_items(self, dept: dict) -> List[dict]:
        """Return configured waste stream entries for a department.

        Waste is the only logistics category that expands one configured
        department/category override into per-stream generator instances.  The
        department dialog stores waste streams as either names or dictionaries
        containing per-department generation settings.  This normalises both
        forms and ignores blank entries.
        """
        result: List[dict] = []
        seen = set()

        for raw in dept.get("waste_streams", []) or []:
            if isinstance(raw, dict):
                item = dict(raw)
                name = _clean_text(item.get("name"))
            else:
                name = _clean_text(raw)
                item = {"name": name}

            if not name or name in seen:
                continue

            item["name"] = name
            result.append(item)
            seen.add(name)

        return result

    def _grouped_department_overrides(self, category: dict) -> tuple[List[dict], set]:
        groups = (
            category.get("department_groups", [])
            if isinstance(category.get("department_groups", []), list)
            else []
        )
        grouped: List[dict] = []
        covered_departments = set()
        seen_ids = set()

        for index, group in enumerate(groups):
            if not isinstance(group, dict):
                continue

            payload = group.get("payload", {})
            departments = [
                _clean_text(x) for x in group.get("departments", []) if _clean_text(x)
            ]
            if not isinstance(payload, dict) or not departments:
                continue

            group_id = _clean_text(group.get("id", "")) or f"group_{index + 1}"
            if group_id in seen_ids:
                group_id = f"{group_id}_{index + 1}"
            seen_ids.add(group_id)
            covered_departments.update(departments)
            grouped.append(
                {
                    "id": group_id,
                    "departments": departments,
                    "payload": dict(payload),
                }
            )

        return grouped, covered_departments

    def _append_department_instance(
        self,
        instances: List[dict],
        category_key_text: str,
        category: dict,
        dept: dict,
        override: dict,
        instance_suffix: str = "",
    ) -> None:
        dept_id = self._department_id(dept)
        if not dept_id:
            return

        cfg = self._merge_category_with_override(category, override)
        if not bool(cfg.get("enabled", False)):
            return

        dept_locations = self._department_category_locations(dept, category_key_text)
        role = _clean_text(cfg.get("department_location_role", "dropoff")) or "dropoff"

        pickup_locations = self._pickup_locations_from_cfg(cfg)
        dropoff_locations = self._dropoff_locations_from_cfg(cfg)

        if role == "pickup" and dept_locations:
            pickup_locations = dept_locations
        elif role != "pickup" and dept_locations:
            dropoff_locations = dept_locations
        department_location_role = role if dept_locations else ""

        suffix = _clean_text(instance_suffix)
        key_base = f"{category_key_text}:{dept_id}"
        instance_key = f"{key_base}:{suffix}" if suffix else key_base

        if category_key_text.lower() == "waste":
            stream_items = self._department_waste_stream_items(dept)

            if stream_items:
                for stream_item in stream_items:
                    stream_cfg = self._waste_stream_cfg_for_department(
                        category, override, stream_item
                    )
                    if not stream_cfg:
                        continue
                    stream_name = _clean_text(stream_cfg.get("waste_stream"))
                    container_group = self._waste_container_group_for_instance(
                        dept_id, stream_name, stream_cfg, pickup_locations
                    )
                    stream_cfg["container_group"] = container_group
                    unique_key = f"waste:{dept_id}:{stream_name}"
                    if suffix:
                        unique_key = f"{unique_key}:{suffix}"
                    instances.append(
                        {
                            "key": unique_key,
                            "volume_key": container_group,
                            "schedule_key": unique_key,
                            "category_key": "waste",
                            "department_id": dept_id,
                            "department_name": self._department_name(dept),
                            "department": dict(dept),
                            "cfg": stream_cfg,
                            "pickup_locations": pickup_locations,
                            "dropoff_locations": dropoff_locations,
                            "waste_stream": stream_name,
                            "container_group": container_group,
                        }
                    )
                return

            # Backwards compatibility: if no department streams are set,
            # use the old department waste category override.

        instances.append(
            {
                "key": instance_key,
                "category_key": category_key_text,
                "department_id": dept_id,
                "department_name": self._department_name(dept),
                "department": dict(dept),
                "cfg": cfg,
                "pickup_locations": pickup_locations,
                "dropoff_locations": dropoff_locations,
                "department_location_role": department_location_role,
            }
        )

    def _threshold_from_waste_stream(self, stream_name: str, cfg: dict) -> float:
        threshold = _as_float(cfg.get("threshold_volume_m3", 0.0), 0.0)
        if threshold > 0:
            return threshold

        stream_cfg = self.waste_streams.get(stream_name, {}) or {}
        capacity = _as_float(stream_cfg.get("container_capacity_m3", 0.0), 0.0)
        fraction = _as_float(stream_cfg.get("full_threshold_fraction", 0.8), 0.8)
        fallback = capacity * fraction
        return fallback if fallback > 0 else 0.0

    def _waste_container_group_for_instance(
        self, dept_id: str, stream_name: str, cfg: dict, pickup_locations: List[str]
    ) -> str:
        explicit = _clean_text(
            cfg.get("shared_container_group", cfg.get("shared_container_id", ""))
        )
        if explicit:
            return f"shared:{explicit}"
        if bool(cfg.get("shared_container", False)):
            pickup_signature = ",".join(sorted(_unique_clean(pickup_locations)))
            return f"pickup:{stream_name}:{pickup_signature}"
        return f"department:{dept_id}:{stream_name}"

    def _waste_stream_cfg_for_department(
        self, category_cfg: dict, dept_override: dict, stream_item: dict
    ) -> Optional[dict]:
        stream_name = _clean_text(stream_item.get("name"))
        if not stream_name:
            return None

        global_stream = dict(self.waste_streams.get(stream_name, {}) or {})

        # Deleted/orphaned stream assignments should be removable in the UI but
        # should not create new tasks unless they still carry a valid payload in
        # the saved department stream item.
        if stream_name not in self.waste_streams and not _clean_text(
            stream_item.get("payload")
        ):
            return None

        cfg = self._merge_category_with_override(category_cfg, dept_override)

        # Global stream definition supplies the container/payload defaults.
        for key in ("payload", "container_capacity_m3", "full_threshold_fraction"):
            if key in global_stream and _clean_text(global_stream.get(key)):
                cfg[key] = global_stream.get(key)

        # The department's selected stream item supplies generation settings and
        # may intentionally override the global stream payload if present.
        for key, value in stream_item.items():
            if key == "name":
                continue
            cfg[key] = value

        cfg["waste_stream"] = stream_name
        cfg["task_source"] = "department_waste"
        cfg["initial_container_present"] = bool(
            cfg.get("initial_container_present", True)
        )
        cfg["shared_container"] = bool(cfg.get("shared_container", False))
        cfg["shared_container_group"] = _clean_text(
            cfg.get("shared_container_group", cfg.get("shared_container_id", ""))
        )

        payload_name = _clean_text(cfg.get("payload", ""))
        if not payload_name:
            payload_name = _clean_text(global_stream.get("payload", ""))
            cfg["payload"] = payload_name

        if payload_name not in self.payloads:
            return None

        threshold = self._threshold_from_waste_stream(stream_name, cfg)
        if threshold > 0:
            cfg["threshold_volume_m3"] = threshold

        if _as_float(cfg.get("volume_per_event_m3", 0.0), 0.0) <= 0.0:
            cfg["volume_per_event_m3"] = threshold

        return cfg

    def _build_instances(self) -> List[dict]:
        categories = self.task_generation.get("categories", {}) or {}
        instances: List[dict] = []

        for category_key, category in categories.items():
            if not isinstance(category, dict):
                continue

            category_key_text = str(category_key)
            overrides = (
                category.get("departments", {})
                if isinstance(category.get("departments", {}), dict)
                else {}
            )
            groups, grouped_department_ids = self._grouped_department_overrides(
                category
            )
            departments_by_id = {
                self._department_id(dept): dept
                for dept in self.departments
                if self._department_id(dept)
            }

            # Department overrides are independently enabled.  The category
            # default "enabled" flag must not disable configured departments.
            # Waste is the only category that expands into one instance per
            # configured department waste stream.
            for group in groups:
                for dept_id in group["departments"]:
                    dept = departments_by_id.get(dept_id)
                    if dept is None:
                        continue
                    self._append_department_instance(
                        instances,
                        category_key_text,
                        category,
                        dept,
                        group["payload"],
                        f"group:{group['id']}",
                    )

            for dept in self.departments:
                dept_id = self._department_id(dept)
                if not dept_id or dept_id not in overrides:
                    continue
                if dept_id in grouped_department_ids:
                    continue
                self._append_department_instance(
                    instances,
                    category_key_text,
                    category,
                    dept,
                    overrides.get(dept_id, {}),
                )

            # Category-level generation is only for a deliberately enabled
            # category default and only when there are no department overrides.
            # This prevents the "Category defaults" row from creating a site-wide
            # task in addition to department-level tasks.
            if bool(category.get("enabled", False)) and not overrides:
                instances.append(
                    {
                        "key": category_key_text,
                        "category_key": category_key_text,
                        "department_id": "",
                        "department_name": "",
                        "cfg": self._merge_category_with_override(category, None),
                        "pickup_locations": self._pickup_locations_from_cfg(category),
                        "dropoff_locations": self._dropoff_locations_from_cfg(category),
                    }
                )

        return instances

    def _prepare_instance_runtime_fields(self) -> None:
        """Cache config-derived values used repeatedly during generation."""
        for instance in self.instances:
            cfg = instance.get("cfg", {}) or {}
            weekly_hours = _normalise_staff_weekly_hours(
                cfg.get("staff_working_hours", {})
            )
            weekly_key = tuple(
                (
                    day_key,
                    bool(weekly_hours[day_key].get("enabled", False)),
                    _clean_text(weekly_hours[day_key].get("start_time", "")),
                    _clean_text(weekly_hours[day_key].get("end_time", "")),
                )
                for day_key in DAY_KEYS
            )
            generation_mode = (
                _clean_text(cfg.get("generation_mode", "scheduled")) or "scheduled"
            )
            payload_name = _clean_text(cfg.get("payload", ""))
            active_days = tuple(
                _clean_text(x).lower()[:3]
                for x in (cfg.get("days_active", []) or [])
                if _clean_text(x)
            )

            instance["_timeframe_group_key"] = (
                _clean_text(instance.get("category_key", "")).lower(),
                _clean_text(cfg.get("staff_resource_name", "")).lower(),
                _normalise_staff_shift_pattern(
                    cfg.get("staff_shift_pattern", "none")
                ),
                bool(
                    _as_bool(cfg.get("staff_use_custom_working_hours", False), False)
                ),
                weekly_key,
                _clean_text(cfg.get("timeframe_start", "")),
                _clean_text(cfg.get("timeframe_end", "")),
            )
            instance["_generation_mode"] = generation_mode
            instance["_requires_staff"] = _as_bool(
                cfg.get("requires_staff", False), False
            )
            instance["_payload_name"] = payload_name
            instance["_enabled"] = _as_bool(cfg.get("enabled", False), False)
            instance["_active_days"] = active_days
            instance["_run_every_fortnight"] = _as_bool(
                cfg.get("run_every_fortnight", False), False
            )
            instance["_timeframe_task_count"] = (
                self._calculate_timeframe_instance_task_count(instance, payload_name)
            )

    def _build_timeframe_group_members(self) -> Dict[tuple, List[dict]]:
        groups: Dict[tuple, List[dict]] = {}
        for instance in self.instances:
            if instance.get("_generation_mode") not in TIMEFRAME_MODES:
                continue
            if not bool(instance.get("_requires_staff", False)):
                continue
            group_key = self._timeframe_group_key(instance)
            groups.setdefault(group_key, []).append(instance)

        for members in groups.values():
            members.sort(key=lambda item: str(item.get("key", "")))
        return groups

    def _valid_locations_and_payload(
        self, pickup: str, dropoff: str, payload: str
    ) -> bool:
        return bool(
            pickup in self.locations
            and dropoff in self.locations
            and payload in self.payloads
        )

    def _timeframe_group_key(self, instance: dict) -> tuple:
        cached = instance.get("_timeframe_group_key")
        if cached is not None:
            return cached
        cfg = instance.get("cfg", {}) or {}
        weekly_hours = _normalise_staff_weekly_hours(
            cfg.get("staff_working_hours", {})
        )
        weekly_key = tuple(
            (
                day_key,
                bool(weekly_hours[day_key].get("enabled", False)),
                _clean_text(weekly_hours[day_key].get("start_time", "")),
                _clean_text(weekly_hours[day_key].get("end_time", "")),
            )
            for day_key in DAY_KEYS
        )
        return (
            _clean_text(instance.get("category_key", "")).lower(),
            _clean_text(cfg.get("staff_resource_name", "")).lower(),
            _normalise_staff_shift_pattern(cfg.get("staff_shift_pattern", "none")),
            bool(_as_bool(cfg.get("staff_use_custom_working_hours", False), False)),
            weekly_key,
            _clean_text(cfg.get("timeframe_start", "")),
            _clean_text(cfg.get("timeframe_end", "")),
        )

    def _calculate_timeframe_instance_task_count(
        self, instance: dict, payload_name: Optional[str] = None
    ) -> int:
        cfg = instance.get("cfg", {}) or {}
        multiple = max(
            1,
            _as_int(
                cfg.get("timeframe_payload_multiple", cfg.get("payload_multiple", 1)),
                1,
            ),
        )
        if payload_name is None:
            payload_name = _clean_text(cfg.get("payload", ""))
        return sum(
            1
            for index in range(multiple)
            for pickup, dropoff in self._pick_pairs(instance, index)
            if self._valid_locations_and_payload(pickup, dropoff, payload_name)
        )

    def _timeframe_instance_task_count(self, instance: dict) -> int:
        cached = instance.get("_timeframe_task_count")
        if cached is not None:
            return int(cached)
        return self._calculate_timeframe_instance_task_count(instance)

    def _instance_has_active_day(self, instance: dict, base_day: datetime) -> bool:
        cfg = instance.get("cfg", {}) or {}
        if not bool(instance.get("_enabled", _as_bool(cfg.get("enabled", False), False))):
            return False
        active_days = instance.get("_active_days")
        if active_days is None:
            active_days = tuple(
                _clean_text(x).lower()[:3]
                for x in (cfg.get("days_active", []) or [])
                if _clean_text(x)
            )
        if active_days:
            if _day_key_for_datetime(base_day) not in active_days:
                return False
        if bool(
            instance.get(
                "_run_every_fortnight",
                _as_bool(cfg.get("run_every_fortnight", False), False),
            )
        ):
            week_index = max(
                0, (base_day.date() - self.clock.start_datetime.date()).days // 7
            )
            if week_index % 2 != 0:
                return False
        dept = self._instance_department(instance)
        if dept is not None and not _department_operating_periods_for_date(
            dept, base_day
        ):
            return False
        return True

    def _timeframe_group_allocation(
        self, instance: dict, base_day: datetime
    ) -> Tuple[int, int]:
        group_key = self._timeframe_group_key(instance)
        date_key = base_day.date().isoformat()
        cache_key = (group_key, date_key, instance.get("key", ""))
        cached = self._timeframe_allocation_cache.get(cache_key)
        if cached is not None:
            return cached

        members = [
            item
            for item in self._timeframe_group_members.get(group_key, [])
            if self._instance_has_active_day(item, base_day)
        ]

        offset = 0
        total = sum(self._timeframe_instance_task_count(item) for item in members)
        total = max(1, total)
        for item in members:
            count = self._timeframe_instance_task_count(item)
            item_cache_key = (group_key, date_key, item.get("key", ""))
            self._timeframe_allocation_cache[item_cache_key] = (offset, total)
            offset += count

        return self._timeframe_allocation_cache.get(cache_key, (0, total))

    def _staff_shift_definition(self, cfg: dict) -> Tuple[str, dict]:
        pattern_key = _normalise_staff_shift_pattern(
            cfg.get("staff_shift_pattern", "none")
        )
        patterns = self.staff_config.get("shift_patterns", {}) or {}
        pattern = patterns.get(pattern_key, patterns.get("none", {})) or {}
        return pattern_key, pattern

    def _staff_shift_window_for_day(
        self, cfg: dict, base_day: datetime
    ) -> Optional[Tuple[datetime, datetime]]:
        if not _as_bool(cfg.get("requires_staff", False), False):
            return None
        use_custom_hours = _as_bool(
            cfg.get("staff_use_custom_working_hours", False), False
        )
        if (
            not use_custom_hours
            and not _as_bool(self.staff_config.get("enabled", True), True)
        ):
            return None

        if use_custom_hours:
            weekly_hours = _normalise_staff_weekly_hours(
                cfg.get("staff_working_hours", {})
            )
            day_cfg = weekly_hours.get(_day_key_for_datetime(base_day), {})
            if not _as_bool(day_cfg.get("enabled", False), False):
                return None
            start_minutes = _parse_hhmm_to_minutes(
                day_cfg.get("start_time"), None
            )
            end_minutes = _parse_hhmm_to_minutes(day_cfg.get("end_time"), None)
        else:
            _pattern_key, pattern = self._staff_shift_definition(cfg)
            active_days = {
                _clean_text(x).lower()[:3]
                for x in pattern.get("days_active", DAY_KEYS)
                if _clean_text(x)
            }
            if active_days and _day_key_for_datetime(base_day) not in active_days:
                return None
            start_minutes = _parse_hhmm_to_minutes(pattern.get("start_time"), None)
            end_minutes = _parse_hhmm_to_minutes(pattern.get("end_time"), None)
        if start_minutes is None or end_minutes is None:
            return None
        if end_minutes <= start_minutes:
            end_minutes += 24 * 60

        day_start = base_day.replace(hour=0, minute=0, second=0, microsecond=0)
        return (
            day_start + timedelta(minutes=start_minutes),
            day_start + timedelta(minutes=end_minutes),
        )

    def _intersect_with_department_hours(
        self, instance: dict, start_dt: datetime, end_dt: datetime
    ) -> Optional[Tuple[datetime, datetime]]:
        dept = self._instance_department(instance)
        if dept is None:
            return start_dt, end_dt

        candidate_periods: List[Tuple[datetime, datetime]] = []
        cursor = (start_dt - timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        final_day = end_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        while cursor <= final_day:
            candidate_periods.extend(
                _department_operating_periods_for_date(dept, cursor)
            )
            cursor += timedelta(days=1)

        overlaps = []
        for period_start, period_end in candidate_periods:
            overlap_start = max(start_dt, period_start)
            overlap_end = min(end_dt, period_end)
            if overlap_end > overlap_start:
                overlaps.append((overlap_start, overlap_end))
        if not overlaps:
            return None
        return max(overlaps, key=lambda pair: (pair[1] - pair[0]).total_seconds())

    def _timeframe_work_window(
        self,
        instance: dict,
        base_day: datetime,
        timeframe_start: datetime,
        timeframe_end: datetime,
    ) -> Tuple[datetime, datetime, bool, bool]:
        cfg = instance.get("cfg", {}) or {}
        staff_spread_enabled = bool(
            _as_bool(cfg.get("requires_staff", False), False)
            and _as_bool(self.staff_config.get("enabled", True), True)
            and _as_bool(self.staff_config.get("spread_timeframe_tasks", True), True)
        )
        if not staff_spread_enabled:
            return timeframe_start, timeframe_end, False, False

        shift_window = self._staff_shift_window_for_day(cfg, base_day)
        if shift_window is None:
            return timeframe_start, timeframe_end, False, True

        shift_start, shift_end = shift_window
        overlap_start = max(timeframe_start, shift_start)
        overlap_end = min(timeframe_end, shift_end)
        if overlap_end <= overlap_start:
            return timeframe_start, timeframe_end, False, True

        department_overlap = self._intersect_with_department_hours(
            instance, overlap_start, overlap_end
        )
        if department_overlap is None:
            return timeframe_start, timeframe_end, False, True
        return department_overlap[0], department_overlap[1], True, False

    def _base_task(
        self,
        instance: dict,
        pickup: str,
        dropoff: str,
        payload_name: str,
        release_time: float,
        label_suffix: str = "",
    ) -> Optional[Task]:
        cfg = instance["cfg"]
        if not self._valid_locations_and_payload(pickup, dropoff, payload_name):
            return None

        category_key = instance["category_key"]
        department_id = instance.get("department_id", "")
        labels = [category_key]
        if department_id:
            labels.append(department_id)
        if label_suffix:
            labels.append(label_suffix)

        task = Task(
            id=self._next_task_id(category_key, department_id),
            pickup=pickup,
            dropoff=dropoff,
            payload=payload_name,
            release_time=release_time,
            target_time=_as_float(cfg.get("target_time", 0.0), 0.0),
            quantity=1,
            priority=_as_int(cfg.get("priority", 100), 100),
            created_during_runtime=True,
            labels=labels,
            route_profile=_clean_text(cfg.get("route_profile")) or None,
            task_source=_clean_text(cfg.get("task_source", "task_generation"))
            or "task_generation",
            department_id=department_id,
            waste_stream=_clean_text(cfg.get("waste_stream", "")),
            waste_volume_m3=_as_float(cfg.get("waste_volume_m3", 0.0), 0.0),
            container_type=payload_name,
            requires_staff=_as_bool(cfg.get("requires_staff", False), False),
            staff_initial_count=max(1, _as_int(cfg.get("staff_initial_count", 1), 1)),
            staff_resource_name=_clean_text(cfg.get("staff_resource_name", "")),
            staff_category_key=category_key,
            staff_movement_policy=_clean_text(
                cfg.get("staff_movement_policy", "batch_same_location")
            )
            or "batch_same_location",
            staff_shift_pattern=_clean_text(cfg.get("staff_shift_pattern", "none"))
            or "none",
            staff_handling_minutes=max(
                0.0, _as_float(cfg.get("staff_handling_minutes", 0.0), 0.0)
            ),
            staff_use_custom_working_hours=_as_bool(
                cfg.get("staff_use_custom_working_hours", False), False
            ),
            staff_working_hours=_normalise_staff_weekly_hours(
                cfg.get("staff_working_hours", {})
            ),
        )
        if bool(getattr(task, "requires_staff", False)):
            pattern_key, pattern = self._staff_shift_definition(cfg)
            task.staff_shift_pattern = pattern_key
            task.staff_shift_start_time = _clean_text(pattern.get("start_time", ""))
            task.staff_shift_end_time = _clean_text(pattern.get("end_time", ""))
            task.staff_shift_days_active = list(pattern.get("days_active", []) or [])
            task.staff_shift_work_days = max(0, _as_int(pattern.get("work_days", 0), 0))
            task.staff_shift_rest_days = max(0, _as_int(pattern.get("rest_days", 0), 0))
            task.staff_timeframe_spacing_enabled = _as_bool(
                self.staff_config.get("spread_timeframe_tasks", True), True
            )
        if bool(cfg.get("return_enabled", False)):
            return_payload = _clean_text(cfg.get("return_payload", ""))
            if return_payload in self.payloads:
                task.return_enabled = True
                task.return_payload = return_payload
                task.return_delay_minutes = _as_float(
                    cfg.get("return_delay_minutes", 0.0), 0.0
                )
                task.return_route_profile = _clean_text(
                    cfg.get("return_route_profile", cfg.get("route_profile", ""))
                )
                task.return_priority = _as_int(
                    cfg.get("return_priority", cfg.get("priority", 100)), 100
                )

        # Waste-only runtime metadata.  These are deliberately attached as
        # dynamic attributes so amr_sim_models.Task remains backwards-compatible.
        # The simulator uses them to decide whether generated waste tasks must
        # collect an existing seeded container and whether several departments
        # share the same physical bin.
        if _clean_text(cfg.get("waste_stream", "")) or (
            _clean_text(cfg.get("task_source", "")) == "department_waste"
        ):
            task.initial_container_present = bool(
                cfg.get("initial_container_present", True)
            )
            task.shared_container = bool(cfg.get("shared_container", False))
            task.shared_container_group = _clean_text(
                cfg.get("shared_container_group", cfg.get("shared_container_id", ""))
            )
            task.container_group = _clean_text(cfg.get("container_group", ""))

        _apply_tracked_item_metadata(task, cfg, self.payloads)

        # Metadata may override pickup/payload using item-specific source/payload.
        if task.pickup not in self.locations or task.payload not in self.payloads:
            return None
        return task

    def _record_for_task(
        self, task: Task, instance: dict, event_type: str, details: str
    ) -> GeneratedTaskRecord:
        return GeneratedTaskRecord(
            task=task,
            event_type=event_type,
            details=details,
            pickup_location=task.pickup,
            dropoff_location=task.dropoff,
            payload_name=task.payload,
            task_source=getattr(task, "task_source", "task_generation"),
            department_id=getattr(
                task, "department_id", instance.get("department_id", "")
            ),
            waste_stream=getattr(
                task, "waste_stream", instance.get("waste_stream", "")
            ),
            waste_volume_m3=float(
                getattr(
                    task,
                    "waste_volume_m3",
                    getattr(task, "generated_volume_m3", 0.0),
                )
                or getattr(task, "generated_volume_m3", 0.0)
                or 0.0
            ),
            container_type=task.payload,
        )

    def _create_return_task(self, outbound: Task, instance: dict) -> Optional[Task]:
        cfg = instance["cfg"]
        if not bool(cfg.get("return_enabled", False)):
            return None

        return_payload = _clean_text(cfg.get("return_payload", ""))
        if not return_payload:
            # Deliberately do not invent a payload. Use a configured "none" payload if
            # you need empty returns.
            return None
        if return_payload not in self.payloads:
            return None

        delay_minutes = _as_float(cfg.get("return_delay_minutes", 0.0), 0.0)
        release_time = outbound.release_time + (delay_minutes * 60.0)

        task = Task(
            id=self._next_return_task_id(outbound.id),
            pickup=outbound.dropoff,
            dropoff=outbound.pickup,
            payload=return_payload,
            release_time=release_time,
            target_time=_as_float(
                cfg.get("return_target_time", cfg.get("target_time", 0.0)), 0.0
            ),
            quantity=1,
            priority=_as_int(cfg.get("return_priority", cfg.get("priority", 100)), 100),
            created_during_runtime=True,
            labels=list(outbound.labels) + ["return"],
            route_profile=_clean_text(
                cfg.get("return_route_profile", cfg.get("route_profile", ""))
            )
            or None,
            task_source="task_generation_return",
            department_id=getattr(outbound, "department_id", ""),
            container_type=return_payload,
            payload_instance_id=str(getattr(outbound, "payload_instance_id", "") or ""),
            is_return_task=True,
        )
        return task

    def _records_for_outbound(
        self, outbound: Optional[Task], instance: dict, reason: str
    ) -> List[GeneratedTaskRecord]:
        if outbound is None:
            return []
        return [
            self._record_for_task(
                outbound,
                instance,
                "task_generated",
                reason,
            )
        ]

    def _pick_pairs(
        self, instance: dict, occurrence_index: int = 0
    ) -> List[Tuple[str, str]]:
        pickups = [
            x for x in instance.get("pickup_locations", []) if x in self.locations
        ]
        dropoffs = [
            x for x in instance.get("dropoff_locations", []) if x in self.locations
        ]
        if not pickups or not dropoffs:
            return []
        role = _clean_text(instance.get("department_location_role", ""))
        occurrence_index = max(0, int(occurrence_index or 0))
        if role == "pickup" and len(pickups) > 1:
            pickups = [pickups[occurrence_index % len(pickups)]]
        elif role and role != "pickup" and len(dropoffs) > 1:
            dropoffs = [dropoffs[occurrence_index % len(dropoffs)]]

        pairs: List[Tuple[str, str]] = []
        for pickup in pickups:
            for dropoff in dropoffs:
                if pickup != dropoff:
                    pairs.append((pickup, dropoff))
        return pairs

    def _is_waste_instance(self, instance: dict) -> bool:
        cfg = instance.get("cfg", {}) or {}
        return bool(_clean_text(cfg.get("waste_stream", ""))) or (
            _clean_text(cfg.get("task_source", "")) == "department_waste"
        )

    def _runtime_for_volume_key(self, instance: dict) -> dict:
        return self.runtime.setdefault(
            instance.get("volume_key", instance["key"]),
            {
                "volume": 0.0,
                "contributors": {},
                "sporadic_accumulator": {},
                "volume_event_accumulator": {},
            },
        )

    def _threshold_collection_records(
        self, instance: dict, release_time: float, details: str
    ) -> List[GeneratedTaskRecord]:
        cfg = instance["cfg"]
        threshold_volume = _as_float(cfg.get("threshold_volume_m3", 0.0), 0.0)
        if threshold_volume <= 0.0:
            return []

        payload_name = _clean_text(cfg.get("payload", ""))
        if payload_name not in self.payloads:
            return []

        runtime = self._runtime_for_volume_key(instance)
        records: List[GeneratedTaskRecord] = []
        while _as_float(runtime.get("volume", 0.0), 0.0) >= threshold_volume:
            runtime["volume"] = (
                _as_float(runtime.get("volume", 0.0), 0.0) - threshold_volume
            )
            for pickup, dropoff in self._pick_pairs(instance):
                task = self._base_task(
                    instance, pickup, dropoff, payload_name, release_time, "threshold"
                )
                if task is not None:
                    # This is the amount being collected from the bin, not the
                    # individual fill-event volume.  Using volume_per_event_m3
                    # here made threshold collections look like nearly empty bin
                    # swaps in the CSV/visualiser.
                    task.generated_volume_m3 = threshold_volume
                    task.waste_volume_m3 = threshold_volume
                records.extend(self._records_for_outbound(task, instance, details))
        return records

    def _timeframe_records(
        self, instance: dict, now: float
    ) -> List[GeneratedTaskRecord]:
        cfg = instance["cfg"]
        mode = _clean_text(cfg.get("generation_mode", "scheduled")) or "scheduled"
        if mode not in TIMEFRAME_MODES:
            return []

        payload_name = _clean_text(cfg.get("payload", ""))
        if payload_name not in self.payloads:
            return []

        start_minutes, end_minutes = _timeframe_minutes_from_cfg(cfg)
        if start_minutes is None or end_minutes is None:
            return []
        if end_minutes <= start_minutes:
            end_minutes += 24 * 60

        multiple = max(
            1,
            _as_int(
                cfg.get("timeframe_payload_multiple", cfg.get("payload_multiple", 1)),
                1,
            ),
        )

        current_dt = self.clock.sim_seconds_to_datetime(now)
        day_start = current_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        records: List[GeneratedTaskRecord] = []

        # Also check the previous day because a timeframe or staff shift can cross
        # midnight. Each individual payload is emitted only when its own spaced
        # release time has been reached.
        for day_offset in (-1, 0):
            base_day = day_start + timedelta(days=day_offset)
            timeframe_start_dt = base_day + timedelta(minutes=start_minutes)
            timeframe_end_dt = base_day + timedelta(minutes=end_minutes)

            work_start_dt, work_end_dt, spread_applied, out_of_hours = (
                self._timeframe_work_window(
                    instance, base_day, timeframe_start_dt, timeframe_end_dt
                )
            )
            if work_end_dt <= work_start_dt:
                continue

            effective_deadline_dt = work_end_dt if spread_applied else timeframe_end_dt
            deadline_time = (
                effective_deadline_dt - self.clock.start_datetime
            ).total_seconds()
            if deadline_time < 0:
                continue

            schedule_key_base = (
                instance.get("schedule_key", instance["key"]),
                "timeframe",
                timeframe_start_dt.date().isoformat(),
                _clean_text(cfg.get("timeframe_start", "")),
                _clean_text(cfg.get("timeframe_end", "")),
                _clean_text(cfg.get("staff_shift_pattern", "none")),
                work_start_dt.isoformat(),
                work_end_dt.isoformat(),
            )

            window_seconds = max(0.0, (work_end_dt - work_start_dt).total_seconds())
            group_offset, group_total = self._timeframe_group_allocation(
                instance, base_day
            )
            local_ordinal = 0
            for index in range(multiple):
                pairs = self._pick_pairs(instance, index)
                for pickup, dropoff in pairs:
                    if spread_applied:
                        global_ordinal = group_offset + local_ordinal
                        slot_offset = window_seconds * (
                            float(global_ordinal) / float(max(1, group_total))
                        )
                        candidate_release_dt = work_start_dt + timedelta(
                            seconds=slot_offset
                        )
                    else:
                        candidate_release_dt = timeframe_start_dt
                    local_ordinal += 1

                    original_release_time = (
                        candidate_release_dt - self.clock.start_datetime
                    ).total_seconds()
                    release_time = self._adjust_release_to_department_open(
                        instance, original_release_time
                    )
                    if release_time is None or release_time < 0 or release_time > now:
                        continue
                    if release_time >= deadline_time:
                        continue
                    if not self._instance_is_active(instance, release_time):
                        continue

                    pair_key = (pickup, dropoff)
                    schedule_key = schedule_key_base + pair_key + (index,)
                    if schedule_key in self.scheduled_emitted:
                        continue

                    task = self._base_task(
                        instance=instance,
                        pickup=pickup,
                        dropoff=dropoff,
                        payload_name=payload_name,
                        release_time=release_time,
                        label_suffix="timeframe",
                    )
                    if task is None:
                        continue

                    self.scheduled_emitted.add(schedule_key)
                    task.target_time = max(0.0, deadline_time - release_time)
                    task.quantity = 1
                    task.timeframe_start = _clean_text(cfg.get("timeframe_start", ""))
                    task.timeframe_end = _clean_text(cfg.get("timeframe_end", ""))
                    task.timeframe_deadline_time = deadline_time
                    task.timeframe_payload_index = index + 1
                    task.timeframe_payload_multiple = multiple
                    task.staff_timeframe_spaced = bool(spread_applied)
                    task.staff_out_of_hours_required = bool(out_of_hours)
                    task.staff_work_window_start = (
                        work_start_dt - self.clock.start_datetime
                    ).total_seconds()
                    task.staff_work_window_end = deadline_time

                    spacing_note = (
                        f"spaced across staff hours {work_start_dt.strftime('%H:%M')}-"
                        f"{work_end_dt.strftime('%H:%M')}"
                        if spread_applied
                        else (
                            "outside configured staff hours"
                            if out_of_hours
                            else "released at timeframe start"
                        )
                    )
                    records.extend(
                        self._records_for_outbound(
                            task,
                            instance,
                            (
                                f"Generated timeframe {instance['category_key']} task "
                                f"{index + 1}/{multiple}; {spacing_note}; due by "
                                f"{effective_deadline_dt.strftime('%H:%M')}"
                            ),
                        )
                    )

        return records

    def _schedule_records(
        self, instance: dict, now: float
    ) -> List[GeneratedTaskRecord]:
        cfg = instance["cfg"]
        mode = _clean_text(cfg.get("generation_mode", "scheduled")) or "scheduled"
        if mode not in SCHEDULED_MODES:
            return []

        payload_name = _clean_text(cfg.get("payload", ""))
        if payload_name not in self.payloads:
            return []

        current_dt = self.clock.sim_seconds_to_datetime(now)
        day_start = current_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        records: List[GeneratedTaskRecord] = []

        for hhmm in _scheduled_times_from_cfg(cfg):
            release_dt = _datetime_for_day_and_hhmm(day_start, hhmm)
            original_release_time = (
                release_dt - self.clock.start_datetime
            ).total_seconds()
            release_time = self._adjust_release_to_department_open(
                instance, original_release_time
            )
            if release_time is None or release_time < 0 or release_time > now:
                continue
            if not self._instance_is_active(instance, release_time):
                continue

            schedule_key = (
                instance.get("schedule_key", instance["key"]),
                release_dt.date().isoformat(),
                hhmm,
            )
            if schedule_key in self.scheduled_emitted:
                continue
            self.scheduled_emitted.add(schedule_key)

            if self._is_waste_instance(instance) and mode == "scheduled_threshold":
                # For seeded/physical waste containers a scheduled-threshold entry
                # is a fill event, not an instruction to swap the bin immediately.
                # Only create a collection task when the accumulated fill reaches
                # the threshold.
                runtime = self._runtime_for_volume_key(instance)
                runtime["volume"] = _as_float(runtime.get("volume", 0.0), 0.0) + max(
                    0.0, _as_float(cfg.get("volume_per_event_m3", 0.0), 0.0)
                )
                records.extend(
                    self._threshold_collection_records(
                        instance,
                        release_time,
                        f"Generated scheduled-threshold {instance['category_key']} task at {hhmm}",
                    )
                )
                continue

            for pickup, dropoff in self._pick_pairs(instance):
                task = self._base_task(
                    instance=instance,
                    pickup=pickup,
                    dropoff=dropoff,
                    payload_name=payload_name,
                    release_time=release_time,
                    label_suffix="scheduled",
                )
                if task is not None and _clean_text(cfg.get("waste_stream", "")):
                    task.generated_volume_m3 = _as_float(
                        cfg.get("volume_per_event_m3", 0.0), 0.0
                    )
                    task.waste_volume_m3 = task.generated_volume_m3
                records.extend(
                    self._records_for_outbound(
                        task,
                        instance,
                        f"Generated scheduled {instance['category_key']} task at {hhmm}",
                    )
                )

        return records

    def _volume_records(self, instance: dict, now: float) -> List[GeneratedTaskRecord]:
        cfg = instance["cfg"]
        mode = _clean_text(cfg.get("generation_mode", "scheduled")) or "scheduled"
        if mode not in (THRESHOLD_MODES | CONTINUOUS_MODES | SPORADIC_MODES):
            return []

        payload_name = _clean_text(cfg.get("payload", ""))
        if payload_name not in self.payloads:
            return []
        category_active = self._category_is_active(cfg, now)
        department_open = self._instance_department(
            instance
        ) is None or _department_is_open_at_datetime(
            self._instance_department(instance), self.clock.sim_seconds_to_datetime(now)
        )
        if not category_active:
            runtime = self.runtime.setdefault(
                instance.get("volume_key", instance["key"]),
                {
                    "volume": 0.0,
                    "contributors": {},
                    "sporadic_accumulator": {},
                    "volume_event_accumulator": {},
                },
            )
            runtime.setdefault("contributors", {})[instance["key"]] = now
            return []

        volume_key = instance.get("volume_key", instance["key"])
        runtime = self.runtime.setdefault(
            volume_key,
            {
                "volume": 0.0,
                "contributors": {},
                "sporadic_accumulator": {},
                "volume_event_accumulator": {},
            },
        )
        contributor_key = instance["key"]
        contributors = runtime.setdefault("contributors", {})
        last_time = _as_float(contributors.get(contributor_key, 0.0), 0.0)
        if now <= last_time:
            return []

        elapsed_seconds = self._instance_active_elapsed_seconds(
            instance, last_time, now
        )
        elapsed_days = elapsed_seconds / 86400.0
        elapsed_hours = elapsed_seconds / 3600.0
        records: List[GeneratedTaskRecord] = []

        if elapsed_seconds <= 0.0 or not department_open:
            contributors[contributor_key] = now
            return records

        if mode in CONTINUOUS_MODES:
            runtime["volume"] += max(
                0.0, elapsed_days * _as_float(cfg.get("base_daily_volume_m3", 0.0), 0.0)
            )

        # For threshold-based waste streams, frequency_per_day and
        # volume_per_event_m3 represent bin-fill events.  The previous dynamic
        # category path only used base_daily_volume_m3, so department stream
        # settings with event volumes never accumulated to the threshold.
        if mode in THRESHOLD_MODES:
            event_frequency = max(
                0.0, _as_float(cfg.get("frequency_per_day", 0.0), 0.0)
            )
            event_volume = max(0.0, _as_float(cfg.get("volume_per_event_m3", 0.0), 0.0))
            if event_frequency > 0.0 and event_volume > 0.0:
                volume_event_accumulators = runtime.setdefault(
                    "volume_event_accumulator", {}
                )
                volume_event_accumulators[contributor_key] = _as_float(
                    volume_event_accumulators.get(contributor_key, 0.0), 0.0
                ) + (elapsed_days * event_frequency)
                whole_events = int(volume_event_accumulators[contributor_key])
                if whole_events > 0:
                    runtime["volume"] += whole_events * event_volume
                    volume_event_accumulators[contributor_key] -= whole_events

        if mode in SPORADIC_MODES:
            freq_per_day = max(0.0, _as_float(cfg.get("frequency_per_day", 0.0), 0.0))
            sporadic_accumulators = runtime.setdefault("sporadic_accumulator", {})
            sporadic_accumulators[contributor_key] = _as_float(
                sporadic_accumulators.get(contributor_key, 0.0), 0.0
            ) + (elapsed_days * freq_per_day)
            while sporadic_accumulators[contributor_key] >= 1.0:
                sporadic_accumulators[contributor_key] -= 1.0
                for pickup, dropoff in self._pick_pairs(instance):
                    task = self._base_task(
                        instance, pickup, dropoff, payload_name, now, "sporadic"
                    )
                    if task is not None:
                        generated_volume = _as_float(
                            cfg.get("volume_per_event_m3", 0.0), 0.0
                        )
                        task.generated_volume_m3 = generated_volume
                        task.waste_volume_m3 = generated_volume
                    records.extend(
                        self._records_for_outbound(
                            task,
                            instance,
                            f"Generated sporadic {instance['category_key']} task",
                        )
                    )

        if mode in THRESHOLD_MODES:
            records.extend(
                self._threshold_collection_records(
                    instance,
                    now,
                    f"Generated threshold {instance['category_key']} task",
                )
            )

        contributors[contributor_key] = now
        return records

    def _tracked_item_records(
        self, instance: dict, now: float
    ) -> List[GeneratedTaskRecord]:
        cfg = instance["cfg"]
        if not bool(cfg.get("tracked_item_exchange", False)):
            return []
        payload_name = _clean_text(cfg.get("payload", ""))
        payload = self.payloads.get(payload_name)
        items = _payload_tracked_items(payload)
        if not items:
            return []
        if not self._instance_is_active(instance, now):
            self.item_runtime.setdefault(
                instance["key"], {"last_update_time": now, "quantities": {}}
            )["last_update_time"] = now
            return []

        runtime = self.item_runtime.setdefault(
            instance["key"],
            {
                "last_update_time": 0.0,
                "quantities": {
                    name: item["target_quantity"] for name, item in items.items()
                },
            },
        )
        quantities = runtime.setdefault("quantities", {})
        for name, item in items.items():
            quantities.setdefault(name, item["target_quantity"])

        last_time = _as_float(runtime.get("last_update_time", 0.0), 0.0)
        if now <= last_time:
            return []

        elapsed_days = (
            self._instance_active_elapsed_seconds(instance, last_time, now) / 86400.0
        )
        triggered: Dict[str, dict] = {}
        for name, item in items.items():
            consumption = max(0.0, item.get("consumption_per_day", 0.0)) * elapsed_days
            quantities[name] = max(
                0.0,
                _as_float(
                    quantities.get(name, item["target_quantity"]),
                    item["target_quantity"],
                )
                - consumption,
            )
            if quantities[name] <= item["trigger_quantity"]:
                triggered[name] = {
                    **item,
                    "current_quantity": round(quantities[name], 3),
                    "target_quantity": item["target_quantity"],
                }
                # Assume an exchange/top-up request resets the local store to full.
                quantities[name] = item["target_quantity"]

        runtime["last_update_time"] = now
        if not triggered:
            return []

        records: List[GeneratedTaskRecord] = []
        exchange_mode = (
            _clean_text(cfg.get("exchange_mode", "top_up_only")) or "top_up_only"
        )

        for pickup, dropoff in self._pick_pairs(instance):
            task_payload = payload_name
            source_locations = {
                _clean_text(item.get("source_location"))
                for item in triggered.values()
                if _clean_text(item.get("source_location"))
            }
            exchange_payloads = {
                _clean_text(item.get("exchange_payload"))
                for item in triggered.values()
                if _clean_text(item.get("exchange_payload"))
            }
            task_pickup = (
                next(iter(source_locations)) if len(source_locations) == 1 else pickup
            )
            if len(exchange_payloads) == 1:
                task_payload = next(iter(exchange_payloads))

            task = self._base_task(
                instance,
                task_pickup,
                dropoff,
                task_payload,
                now,
                "tracked_item_exchange",
            )
            if task is None:
                continue
            task.tracked_item_exchange = True
            task.exchange_mode = exchange_mode
            task.tracked_item_source_payload = payload_name
            task.tracked_items = triggered
            records.extend(
                self._records_for_outbound(
                    task,
                    instance,
                    f"Generated tracked item exchange for {', '.join(sorted(triggered.keys()))}",
                )
            )

        return records

    def task_state_changed(self, task: Task, state: str) -> None:
        """Release a physical-container generation group after its cycle ends."""
        volume_key = _clean_text(getattr(task, "generator_volume_key", ""))
        if not volume_key:
            return
        runtime = self.runtime.get(volume_key)
        if not runtime:
            return

        collection_id = _clean_text(
            getattr(task, "generator_collection_task_id", "")
        ) or _clean_text(getattr(task, "id", ""))
        outstanding = runtime.setdefault("outstanding_collection_task_ids", set())
        state = _clean_text(state).lower()

        if state == "failed":
            # A failed return, or a failure after the physical container was
            # picked up, leaves the container cycle unresolved. Keep the group
            # blocked rather than generating a second task for the same bin.
            if bool(getattr(task, "is_return_task", False)) or bool(
                getattr(task, "payload_instance_picked_up", False)
            ):
                return

            threshold = _as_float(
                getattr(task, "generator_threshold_volume_m3", 0.0), 0.0
            )
            if collection_id in outstanding and threshold > 0.0:
                runtime["volume"] = (
                    _as_float(runtime.get("volume", 0.0), 0.0) + threshold
                )
            outstanding.discard(collection_id)
            return

        if state != "completed":
            return

        if bool(getattr(task, "is_return_task", False)):
            outstanding.discard(collection_id)
            return

        if not bool(getattr(task, "generator_waits_for_return", False)):
            outstanding.discard(collection_id)

    def update_until(self, now: float) -> List[GeneratedTaskRecord]:
        generated: List[GeneratedTaskRecord] = []
        if not self.instances:
            return generated

        for instance in self.instances:
            generated.extend(self._timeframe_records(instance, now))
            generated.extend(self._schedule_records(instance, now))
            generated.extend(self._volume_records(instance, now))
            generated.extend(self._tracked_item_records(instance, now))

        return generated


class DepartmentWasteTaskGenerator(BaseTaskGenerator):
    """
    Runtime department waste generator.

    Existing fields are intentionally supported:
    - departments[].enabled
    - departments[].days_active
    - departments[].operating_start_time / operating_end_time
    - departments[].hours_operated_per_day (derived compatibility field)
    - departments[].bed_count
    - departments[].patient_turnover
    - departments[].staff_count
    - departments[].waste.alpha/beta/gamma
    - departments[].waste.pickup_location/dropoff_location
    - departments[].waste_streams[]
    - waste_streams[].payload/container_capacity_m3/full_threshold_fraction
    """

    generator_type = "department_waste"

    def __init__(
        self,
        departments: Iterable[dict],
        waste_streams: Dict[str, dict],
        locations: Dict[str, Location],
        payloads: Dict[str, PayloadType],
        clock: SimulationClock,
        default_priority: int = 60,
    ):
        self.departments = list(departments or [])
        self.waste_streams = waste_streams or {}
        self.locations = locations or {}
        self.payloads = payloads or {}
        self.clock = clock
        self.default_priority = int(default_priority)
        self.runtime: Dict[str, Dict[str, dict]] = {}
        self.task_counter = 0
        self._init_runtime()

    def _init_runtime(self) -> None:
        self.runtime = {}

        for dept in self.departments:
            dept_id = str(dept.get("id", "")).strip()
            if not dept_id:
                continue

            stream_names = [
                str(x.get("name", x) if isinstance(x, dict) else x).strip()
                for x in dept.get("waste_streams", [])
                if str(x.get("name", x) if isinstance(x, dict) else x).strip()
                in self.waste_streams
            ]
            if not stream_names:
                continue

            self.runtime[dept_id] = {}
            for stream_name in stream_names:
                stream_cfg = dict(self.waste_streams.get(stream_name, {}))
                self.runtime[dept_id][stream_name] = {
                    "last_update_time": 0.0,
                    "fill_m3": 0.0,
                    "generated_m3_total": 0.0,
                    "tasks_created": 0,
                    "container_capacity_m3": float(
                        stream_cfg.get("container_capacity_m3", 0.0) or 0.0
                    ),
                    "full_threshold_fraction": float(
                        stream_cfg.get("full_threshold_fraction", 0.8) or 0.8
                    ),
                }

    def _day_key_for_sim_time(self, sim_time_sec: float) -> str:
        dt = self.clock.sim_seconds_to_datetime(sim_time_sec)
        return DAY_KEYS[dt.weekday()]

    def _department_is_active(self, dept: dict, sim_time_sec: float) -> bool:
        return _department_is_open_at_datetime(
            dept, self.clock.sim_seconds_to_datetime(sim_time_sec)
        )

    def _department_active_elapsed_seconds(
        self, dept: dict, start_sec: float, end_sec: float
    ) -> float:
        return _department_active_seconds_between(
            dept,
            self.clock.sim_seconds_to_datetime(start_sec),
            self.clock.sim_seconds_to_datetime(end_sec),
        )

    def _department_hourly_waste_rate_m3(self, dept: dict) -> float:
        waste_cfg = dict(dept.get("waste", {}) or {})

        alpha = float(waste_cfg.get("alpha", 0.0) or 0.0)
        beta = float(waste_cfg.get("beta", 0.0) or 0.0)
        gamma = float(waste_cfg.get("gamma", 0.0) or 0.0)

        bed_count = float(dept.get("bed_count", 0.0) or 0.0)
        patient_turnover = float(dept.get("patient_turnover", 0.0) or 0.0)
        staff_count = float(dept.get("staff_count", 0.0) or 0.0)
        hours_operated = _department_operating_hours_per_day(dept)

        turnover_per_hour = patient_turnover / hours_operated
        return (alpha * bed_count) + (beta * turnover_per_hour) + (gamma * staff_count)

    def _make_task_id(self, dept_id: str, stream_name: str) -> str:
        self.task_counter += 1
        safe_stream = "".join(
            c if c.isalnum() else "_" for c in str(stream_name).upper()
        ).strip("_")
        return f"WASTE_{dept_id}_{safe_stream}_{self.task_counter:05d}"

    def _create_task_record(
        self,
        dept: dict,
        stream_name: str,
        release_time: float,
        waste_volume_m3: float,
    ) -> Optional[GeneratedTaskRecord]:
        dept_id = str(dept.get("id", "")).strip()
        dept_name = str(dept.get("name", dept_id)).strip()
        stream_cfg = dict(self.waste_streams.get(stream_name, {}))
        waste_cfg = dict(dept.get("waste", {}) or {})

        pickup_location = str(waste_cfg.get("pickup_location", "")).strip()
        dropoff_location = str(waste_cfg.get("dropoff_location", "")).strip()
        payload_name = str(stream_cfg.get("payload", "")).strip()

        if not pickup_location or pickup_location not in self.locations:
            return None
        if not dropoff_location or dropoff_location not in self.locations:
            return None
        if not payload_name or payload_name not in self.payloads:
            return None

        task = Task(
            id=self._make_task_id(dept_id, stream_name),
            pickup=pickup_location,
            dropoff=dropoff_location,
            payload=payload_name,
            release_time=release_time,
            target_time=0.0,
            quantity=1,
            priority=int(
                waste_cfg.get("priority", self.default_priority)
                or self.default_priority
            ),
            created_during_runtime=True,
            labels=["waste", stream_name],
            route_profile=waste_cfg.get("route_profile") or None,
            task_source="department_waste",
            department_id=dept_id,
            waste_stream=stream_name,
            waste_volume_m3=float(waste_volume_m3),
            container_type=payload_name,
        )

        return GeneratedTaskRecord(
            task=task,
            event_type="waste_task_generated",
            details=f"Generated waste collection for {dept_name} / {stream_name}",
            pickup_location=pickup_location,
            dropoff_location=dropoff_location,
            payload_name=payload_name,
            task_source="department_waste",
            department_id=dept_id,
            waste_stream=stream_name,
            waste_volume_m3=float(waste_volume_m3),
            container_type=payload_name,
        )

    def update_until(self, now: float) -> List[GeneratedTaskRecord]:
        if now <= 0 or not self.departments:
            return []

        generated: List[GeneratedTaskRecord] = []

        for dept in self.departments:
            dept_id = str(dept.get("id", "")).strip()
            if not dept_id:
                continue

            runtime_by_stream = self.runtime.get(dept_id, {})
            if not runtime_by_stream:
                continue

            for stream_name, runtime in runtime_by_stream.items():
                last_time = float(runtime.get("last_update_time", 0.0) or 0.0)
                if now <= last_time:
                    continue

                if self._department_is_active(dept, now):
                    elapsed_hours = (
                        self._department_active_elapsed_seconds(dept, last_time, now)
                        / 3600.0
                    )
                    generated_m3 = max(
                        0.0, elapsed_hours * self._department_hourly_waste_rate_m3(dept)
                    )
                    runtime["fill_m3"] += generated_m3
                    runtime["generated_m3_total"] += generated_m3

                    trigger_volume_m3 = float(
                        runtime.get("container_capacity_m3", 0.0) or 0.0
                    ) * float(runtime.get("full_threshold_fraction", 0.8) or 0.8)

                    if trigger_volume_m3 > 0:
                        while runtime["fill_m3"] >= trigger_volume_m3:
                            record = self._create_task_record(
                                dept=dept,
                                stream_name=stream_name,
                                release_time=now,
                                waste_volume_m3=trigger_volume_m3,
                            )
                            if record is not None:
                                generated.append(record)
                            runtime["fill_m3"] -= trigger_volume_m3
                            runtime["tasks_created"] += 1

                runtime["last_update_time"] = now

        return generated


class TaskGenerationManager:
    """Registry and coordinator for all runtime task generators."""

    def __init__(
        self,
        config: dict,
        clock: SimulationClock,
        locations: Dict[str, Location],
        payloads: Dict[str, PayloadType],
    ):
        self.config = config or {}
        self.clock = clock
        self.locations = locations
        self.payloads = payloads
        self.generators: List[BaseTaskGenerator] = []
        self._build_generators()

    def _task_generation_cfg(self) -> dict:
        return self.config.get("task_generation", {}) or {}

    def _generation_enabled(self, name: str, default: bool = True) -> bool:
        cfg = self._task_generation_cfg()
        if cfg and not bool(cfg.get("enabled", True)):
            return False
        specific = cfg.get(name, {}) or {}
        return bool(specific.get("enabled", default))

    def _has_dynamic_categories(self) -> bool:
        categories = self._task_generation_cfg().get("categories", {}) or {}
        for category in categories.values():
            if not isinstance(category, dict):
                continue

            # Enabled category default, usually legacy/global category generation.
            if bool(category.get("enabled", False)):
                return True

            # Enabled department overrides must start the dynamic generator even
            # when the category default is intentionally disabled.
            overrides = category.get("departments", {})
            if isinstance(overrides, dict):
                for override in overrides.values():
                    if isinstance(override, dict) and bool(
                        override.get("enabled", False)
                    ):
                        return True
        return False

    def _build_generators(self) -> None:
        task_generation = self._task_generation_cfg()
        departments = list(self.config.get("departments", []))

        waste_streams = {
            str(item.get("name", "")).strip(): dict(item)
            for item in self.config.get("waste_streams", [])
            if str(item.get("name", "")).strip()
        }

        if self._has_dynamic_categories() and bool(
            task_generation.get("enabled", True)
        ):
            self.generators.append(
                DynamicCategoryTaskGenerator(
                    task_generation=task_generation,
                    departments=departments,
                    locations=self.locations,
                    payloads=self.payloads,
                    clock=self.clock,
                    waste_streams=waste_streams,
                )
            )

        # Keep legacy department waste support, but avoid duplicating new waste category
        # generation when the dynamic Waste category is enabled.
        categories = task_generation.get("categories", {}) or {}
        waste_category = (
            categories.get("waste", {})
            if isinstance(categories.get("waste", {}), dict)
            else {}
        )
        waste_overrides = (
            waste_category.get("departments", {})
            if isinstance(waste_category.get("departments", {}), dict)
            else {}
        )
        dynamic_waste_enabled = bool(
            bool(waste_category.get("enabled", False))
            or any(
                isinstance(override, dict) and bool(override.get("enabled", False))
                for override in waste_overrides.values()
            )
        )

        if (
            waste_streams
            and departments
            and not dynamic_waste_enabled
            and self._generation_enabled("department_waste", True)
        ):
            priority = int(
                (task_generation.get("department_waste", {}) or {}).get("priority", 60)
                or 60
            )
            self.generators.append(
                DepartmentWasteTaskGenerator(
                    departments=departments,
                    waste_streams=waste_streams,
                    locations=self.locations,
                    payloads=self.payloads,
                    clock=self.clock,
                    default_priority=priority,
                )
            )

    def update_until(self, now: float) -> List[GeneratedTaskRecord]:
        generated: List[GeneratedTaskRecord] = []
        for generator in self.generators:
            generated.extend(generator.update_until(now))
        return generated

    def task_state_changed(self, task: Task, state: str) -> None:
        """Forward task completion/failure state to every runtime generator."""
        for generator in self.generators:
            generator.task_state_changed(task, state)
