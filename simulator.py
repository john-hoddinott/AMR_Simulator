import argparse
import csv
import heapq
from bisect import bisect_left, insort_right
import json
import math
import os
import threading
import time
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from amr_sim_energy import (
    requires_recharge_before_route,
    total_lift_energy_kwh,
    total_route_energy_kwh,
)
from amr_sim_models import AMR, Event, Lift, Location, PayloadType, Task
from amr_sim_payload_instances import (
    EMPTY_PAYLOAD_NAME,
    PayloadInstanceStore,
    is_empty_payload_name,
    normalise_payload_name,
)
from amr_sim_task_generation import TaskGenerationManager
from amr_sim_time_utils import (
    SimulationClock,
    format_duration,
    parse_datetime,
    parse_release_time,
)


def _bool_from_config(value, default: bool = False) -> bool:
    """Return a predictable bool for JSON/config values.

    Handles real booleans plus common string/int forms produced by editor
    checkboxes and hand-edited JSON files.
    """
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


def _nested_get_bool(config: dict, keys, default: bool = False) -> bool:
    for key_path in keys:
        current = config
        found = True
        for key in key_path:
            if not isinstance(current, dict) or key not in current:
                found = False
                break
            current = current.get(key)
        if found:
            return _bool_from_config(current, default)
    return bool(default)


def _config_contains_enabled_bool_key(config, key_names) -> bool:
    """Recursively find any enabled boolean flag by key name.

    Editor/task CSV imports have used several flattened key names over time.
    This catches the flag whether it is stored globally in the JSON, under a
    nested section, or carried through from an imported CSV task row.
    """
    wanted = {str(k).strip().lower() for k in key_names}

    def walk(value) -> bool:
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).strip().lower() in wanted and _bool_from_config(
                    child, False
                ):
                    return True
                if isinstance(child, (dict, list)) and walk(child):
                    return True
        elif isinstance(value, list):
            for child in value:
                if isinstance(child, (dict, list)) and walk(child):
                    return True
        return False

    return walk(config)


class Simulation:
    def __init__(
        self,
        config: dict,
        verbose: bool = False,
        verbose_csv_path: Optional[str] = None,
    ):
        self.location_reservations = defaultdict(list)
        self.config = config
        sim_cfg = config.get("simulation", {})
        scenario_cfg = config.get("scenario_testing", {}) or {}
        self.scenario_mode = bool(scenario_cfg.get("enabled", False))
        self.scenario_name = str(scenario_cfg.get("active_scenario", "Normal operation") or "Normal operation")
        self.scenario_enhanced_logging = bool(scenario_cfg.get("enhanced_logging", False))
        self.scenario_description = ""
        self.scenario_events: List[dict] = []
        if self.scenario_mode and self.scenario_name != "Normal operation":
            for scenario in scenario_cfg.get("scenarios", []) or []:
                if str(scenario.get("name", "") or "").strip() == self.scenario_name:
                    self.scenario_description = str(scenario.get("description", "") or "")
                    self.scenario_events = [dict(x) for x in scenario.get("events", []) or [] if isinstance(x, dict)]
                    break
        else:
            self.scenario_mode = False
            self.scenario_name = "Normal operation"
        self.scenario_delay_sec = 0.0
        self.scenario_affected_segments = 0
        self.people_delay_sec = 0.0
        self.people_affected_segments = 0
        self.wash_cycles_completed = 0
        self.charge_intervals: List[dict] = []
        start_datetime = parse_datetime(
            sim_cfg.get("start_datetime", "2026-01-01T08:00:00")
        )
        tick_rate = float(sim_cfg.get("tick_rate", 120.0))
        self.wall_start_time = None
        self.last_progress_update = 0.0
        self.progress_update_interval = 0.2  # seconds
        self.estimated_total_sim_time = 1.0

        self.clock = SimulationClock(start_datetime=start_datetime, tick_rate=tick_rate)
        self.current_time = 0.0
        self.verbose = verbose
        self.verbose_csv_path = verbose_csv_path
        self.verbose_rows: List[dict] = []
        # Keep verbose logging bounded. Long simulations can produce hundreds
        # of thousands of rows; retaining every row until shutdown caused both
        # memory growth and progressively slower list/GC behaviour. Rows are
        # now appended to the CSV in chunks while preserving the same schema.
        self.verbose_row_buffer_size = max(
            100,
            int(sim_cfg.get("verbose_row_buffer_size", 5000) or 5000),
        )
        self._verbose_csv_started = False
        self._verbose_rows_written = 0
        self._verbose_write_lock = threading.RLock()
        self._format_sim_time_cache: Dict[float, str] = {}
        self.event_counter = 0
        self.events: List[Event] = []
        self.pending_tasks: List[Tuple[int, float, int, Task]] = []
        self.pending_task_counter = 0
        self._removed_pending_task_ids = set()
        # Inventory-space checks used to rebuild the active task-id set by
        # scanning both heaps every time. Cache it until either heap changes.
        self._task_activity_version = 0
        self._active_task_ids_cache_version = -1
        self._active_task_ids_cache = set()
        self.lock = threading.RLock()
        self.route_cache_lock = threading.RLock()
        self.stop_requested = False
        self.completed_task_records: List[dict] = []
        self.failed_tasks: List[dict] = []
        self.failed_task_ids = set()
        self.location_reservations: Dict[str, List[Tuple[float, float]]] = defaultdict(
            list
        )
        self.location_reservation_max_duration: Dict[str, float] = defaultdict(float)

        self.payload_instance_store = PayloadInstanceStore()
        self.location_storage_peak: Dict[str, dict] = {}
        self.payload_population_peak: Dict[str, int] = {}
        self._location_recommendation_rows_written = False
        self._payload_population_rows_written = False
        self.staff_resource_pools: Dict[str, dict] = {}
        self.staff_assignments: List[dict] = []
        task_generation_cfg = config.get("task_generation", {}) or {}
        staff_cfg = task_generation_cfg.get(
            "staff_config", task_generation_cfg.get("staff", {})
        ) or {}
        try:
            self.staff_walk_speed_m_per_sec = max(
                0.1, float(staff_cfg.get("walking_speed_m_per_sec", 1.2) or 1.2)
            )
        except Exception:
            self.staff_walk_speed_m_per_sec = 1.2
        try:
            self.staff_lift_wait_seconds = max(
                0.0, float(staff_cfg.get("lift_wait_seconds", 30.0) or 0.0)
            )
        except Exception:
            self.staff_lift_wait_seconds = 30.0
        try:
            self.staff_default_handling_minutes = max(
                0.0, float(staff_cfg.get("default_handling_minutes", 15.0) or 0.0)
            )
        except Exception:
            self.staff_default_handling_minutes = 15.0
        self._staff_travel_cache: Dict[Tuple[str, str], Tuple[float, float, str]] = {}

        # Congestion setup
        building_cfg = config.get("building", {})

        self.edge_reservations: Dict[
            Tuple[str, str], List[Tuple[float, float, str]]
        ] = defaultdict(list)
        # Direction-aware reservations preserve FIFO movement along a corridor
        # edge. Physical reservations still control capacity/congestion; these
        # prevent a later AMR in the same direction from visually overtaking.
        self.directed_edge_reservations: Dict[
            Tuple[str, str], List[Tuple[float, float, str]]
        ] = defaultdict(list)
        # Maximum interval duration per reservation resource allows overlap
        # searches to jump past irrelevant history with bisect while preserving
        # exact interval semantics.
        self.edge_reservation_max_duration: Dict[Tuple[str, str], float] = defaultdict(
            float
        )
        self.directed_edge_reservation_max_duration: Dict[Tuple[str, str], float] = (
            defaultdict(float)
        )

        self.node_reservations: Dict[str, List[Tuple[float, float, str]]] = defaultdict(
            list
        )
        self.node_reservation_max_duration: Dict[str, float] = defaultdict(float)
        self.lift_reservations: Dict[
            str, List[Tuple[float, float, int, int, str]]
        ] = defaultdict(list)
        self.node_clearance_time_sec = float(
            building_cfg.get("node_clearance_time_sec", 0.5)
        )

        self.route_cache: Dict[Tuple, Optional[dict]] = {}
        self.local_obstacle_bbox_cache: Dict[
            Tuple[str, str], Tuple[Tuple[float, float, float, float], ...]
        ] = {}
        self.local_manoeuvre_waypoint_cache: Dict[
            Tuple, Tuple[Tuple[float, float], ...]
        ] = {}
        self.local_manoeuvre_waypoint_cache_max_entries = max(
            0,
            int(
                sim_cfg.get("local_manoeuvre_waypoint_cache_max_entries", 10000)
                or 0
            ),
        )
        self.generated_release_stagger_sec = max(
            0.0,
            float(
                sim_cfg.get(
                    "generated_task_release_stagger_sec",
                    sim_cfg.get("task_release_stagger_sec", 0.25),
                )
                or 0.0
            ),
        )
        self._generated_release_stagger_counts: Dict[float, int] = defaultdict(int)
        self.seed_waste_stream_containers_at_start = bool(
            sim_cfg.get(
                "seed_waste_stream_containers_at_start",
                sim_cfg.get("waste_stream_containers_present_at_start", False),
            )
        )
        # PayloadInstanceStore owns exact task reservations.  Keep no parallel
        # anonymous reservation set, because it cannot release a failed task's
        # reservation without risking another task's claim.
        self.initial_waste_container_instances: Dict[Tuple[str, str, str], str] = {}
        self.route_precompute_enabled = bool(
            sim_cfg.get("precompute_static_routes", True)
        )
        self.route_precompute_max_pairs = max(
            0, int(sim_cfg.get("route_precompute_max_pairs", 100000) or 0)
        )
        self.max_single_candidate_tasks = max(
            1, int(sim_cfg.get("max_single_candidate_tasks", 8) or 8)
        )
        self.max_multi_stop_candidate_tasks = max(
            2, int(sim_cfg.get("max_multi_stop_candidate_tasks", 8) or 8)
        )
        self.max_assignments_per_tick = max(
            0, int(sim_cfg.get("max_assignments_per_tick", 50) or 0)
        )
        self.assignment_continue_delay_sec = max(
            0.001, float(sim_cfg.get("assignment_continue_delay_sec", 0.001) or 0.001)
        )
        self._assignment_continue_scheduled = False
        self.idle_return_check_interval_sec = max(
            1.0, float(sim_cfg.get("idle_return_check_interval_sec", 60.0) or 60.0)
        )
        self._next_idle_return_check_time = 0.0
        default_route_workers = max(1, min(8, os.cpu_count() or 1))
        self.routing_worker_threads = max(
            1,
            int(
                sim_cfg.get(
                    "routing_worker_threads",
                    sim_cfg.get("route_worker_threads", default_route_workers),
                )
            ),
        )
        # Routing/assignment estimates are numerous and often repeated while the
        # same AMR/task state is being considered.  Cache non-reserving estimates
        # and only use the routing thread pool when there is enough work to offset
        # Future/as_completed overhead.
        self.route_estimate_cache: Dict[tuple, Optional[dict]] = {}
        self.route_estimate_cache_version = 0
        self.route_estimate_time_bucket_sec = max(
            1.0, float(sim_cfg.get("route_estimate_time_bucket_sec", 30.0) or 30.0)
        )
        self.route_estimate_cache_max_entries = max(
            0, int(sim_cfg.get("route_estimate_cache_max_entries", 25000) or 0)
        )
        self.parallel_routing_min_jobs = max(
            2, int(sim_cfg.get("parallel_routing_min_jobs", 64) or 64)
        )
        self.parallel_routing_enabled = (
            bool(sim_cfg.get("parallel_routing", True))
            and self.routing_worker_threads > 1
        )
        self.routing_executor = (
            ThreadPoolExecutor(
                max_workers=self.routing_worker_threads,
                thread_name_prefix="amr-route",
            )
            if self.parallel_routing_enabled
            else None
        )

        self.amr_spacing_m = float(building_cfg.get("amr_spacing_m", 1.5))
        self.edge_max_concurrency = int(building_cfg.get("edge_max_concurrency", 1))
        self.edge_congestion_window_sec = float(
            building_cfg.get("edge_congestion_window_sec", 30.0)
        )
        self.edge_slowdown_per_amr = float(
            building_cfg.get("edge_slowdown_per_amr", 0.15)
        )
        self.min_congestion_speed_factor = float(
            building_cfg.get("min_congestion_speed_factor", 0.45)
        )
        self.default_corridor_width_m = max(
            0.1, float(building_cfg.get("default_corridor_width_m", 2.4) or 2.4)
        )
        self.default_door_clear_width_m = max(
            0.1, float(building_cfg.get("default_door_clear_width_m", 0.9) or 0.9)
        )
        self.people_slowdown_per_person = max(
            0.0, float(building_cfg.get("people_slowdown_per_person", 0.08) or 0.08)
        )
        self.minimum_people_speed_factor = max(
            0.05, min(1.0, float(building_cfg.get("minimum_people_speed_factor", 0.35) or 0.35))
        )
        self.people_edge_reservations: Dict[Tuple[str, str], List[dict]] = defaultdict(list)
        self.reservation_prune_interval_sec = max(
            1.0, float(sim_cfg.get("reservation_prune_interval_sec", 300.0) or 300.0)
        )
        self.reservation_history_retention_sec = max(
            self.edge_congestion_window_sec * 2.0,
            float(
                sim_cfg.get(
                    "reservation_history_retention_sec",
                    max(300.0, self.edge_congestion_window_sec * 2.0),
                )
                or 0.0
            ),
        )
        self._next_reservation_prune_time = 0.0

        self.load_unload_time_sec = float(
            config["building"].get("load_unload_time_sec", 20.0)
        )
        self.floor_height_m = float(config["building"].get("floor_height_m", 4.0))
        configured_charge_locations = config["building"].get("charge_locations")
        if isinstance(configured_charge_locations, list):
            self.charge_location_names = [
                str(x).strip() for x in configured_charge_locations if str(x).strip()
            ]
        else:
            legacy_charge_location = str(
                config["building"].get("charge_location", "")
            ).strip()
            self.charge_location_names = (
                [legacy_charge_location] if legacy_charge_location else []
            )
        if not self.charge_location_names:
            self.charge_location_names = [config["locations"][0]["name"]]
        # Backwards-compatible current charger label used by existing logging paths.
        self.charge_location_name = self.charge_location_names[0]

        # Parse locations from config

        self.locations: Dict[str, Location] = {
            loc["name"]: Location(
                name=loc["name"],
                floor=int(loc["floor"]),
                x=float(loc.get("x", 0.0)),
                y=float(loc.get("y", 0.0)),
                wash_cycle_required=bool(loc.get("wash_cycle_required", False)),
                wash_cycle_duration_sec=max(0.0, float(loc.get("wash_cycle_duration_sec", 300.0) or 0.0)),
                wash_location=str(loc.get("wash_location", "") or "").strip(),
                people_area_type=str(loc.get("people_area_type", "none") or "none").strip().lower(),
            )
            for loc in config["locations"]
        }

        # Parse maximum concurrency from config

        self.location_max_concurrency: Dict[str, int] = {
            loc["name"]: int(loc.get("max_concurrency", 999999))
            for loc in config["locations"]
        }

        # Global inventory-space bypass.  This is used for capacity studies where
        # payloads should still be tracked physically, but finite drawn inventory
        # slots must not block, reserve, occupy, or fail tasks.  Several legacy
        # key names are accepted because older editor exports used different labels.
        inventory_bypass_keys = [
            "ignore_inventory_spaces",
            "ignore_inventory_space",
            "disable_inventory_spaces",
            "disable_inventory_space",
            "inventory_spaces_disabled",
            "inventory_space_disabled",
            "ignore_location_inventory_spaces",
            "ignore_location_inventory_space",
            "disable_location_inventory_spaces",
            "disable_location_inventory_space",
            "disable_inventory_space_checks",
            "ignore_inventory_space_checks",
        ]
        self.disable_inventory_spaces = _nested_get_bool(
            config,
            [("simulation", key) for key in inventory_bypass_keys]
            + [("building", key) for key in inventory_bypass_keys]
            + [("task_generation", key) for key in inventory_bypass_keys]
            + [(key,) for key in inventory_bypass_keys],
            default=False,
        ) or _config_contains_enabled_bool_key(config, inventory_bypass_keys)

        # Inventory spaces are finite storage slots inside locations.
        # A drop-off only needs a free compatible slot when the location explicitly
        # defines at least one valid inventory space. Locations with no
        # inventory_spaces, an empty inventory_spaces list, or no valid spaces keep
        # the previous unlimited-storage behaviour.
        self.inventory_spaces_by_location: Dict[str, List[dict]] = {}
        self._init_inventory_spaces(config.get("locations", []))

        # Parse payloads from configuration

        self.payloads: Dict[str, PayloadType] = {}
        for p in config["payloads"]:
            legacy_size = float(p.get("size_units", 1.0))
            raw_items = p.get("items", {}) or {}
            if isinstance(raw_items, list):
                converted_items = {}
                for item in raw_items:
                    if not isinstance(item, dict):
                        continue
                    item_name = str(item.get("name", "")).strip()
                    if item_name:
                        converted_items[item_name] = dict(item)
                raw_items = converted_items
            if not isinstance(raw_items, dict):
                raw_items = {}
            clean_items = {}
            for item_name, item_cfg in raw_items.items():
                item_name = str(item_name).strip()
                if not item_name:
                    continue
                item_cfg = item_cfg if isinstance(item_cfg, dict) else {}
                clean_items[item_name] = {
                    "max": float(item_cfg.get("max", 100)),
                    "top_up_threshold": float(item_cfg.get("top_up_threshold", 15)),
                    "usage_rate": str(item_cfg.get("usage_rate", "scheduled_sporadic")),
                    "consumption_per_day": float(
                        item_cfg.get("consumption_per_day", 0.0)
                    ),
                    "exchange_payload": str(
                        item_cfg.get("exchange_payload", "")
                    ).strip(),
                    "source_location": str(item_cfg.get("source_location", "")).strip(),
                }
            payload_type = PayloadType(
                name=p["name"],
                weight_kg=float(p["weight_kg"]),
                length_m=float(p.get("length_m", legacy_size)),
                width_m=float(p.get("width_m", legacy_size)),
                height_m=float(p.get("height_m", legacy_size)),
                size_units=legacy_size,
                track_items=bool(p.get("track_items", False)),
                items=clean_items,
                allowed_carry_orientations=[
                    str(x).strip().lower()
                    for x in (p.get("allowed_carry_orientations", ["lengthways", "sideways"]) or ["lengthways"])
                    if str(x).strip().lower() in {"lengthways", "sideways"}
                ] or ["lengthways"],
            )
            # Optional payload-level preference from the visualiser payload dialog.
            # Kept as a runtime attribute so older amr_sim_models.PayloadType
            # dataclasses do not need to change just to carry this scheduling hint.
            payload_type.prefer_multi_stop_amr = bool(
                p.get("prefer_multi_stop_amr", False)
            )
            self.payloads[p["name"]] = payload_type

        # Internal zero-load payload used for empty idle return, recharge and no-load moves.
        # It is deliberately not written as a real payload in verbose output.
        if EMPTY_PAYLOAD_NAME not in self.payloads:
            empty_payload = PayloadType(
                name=EMPTY_PAYLOAD_NAME,
                weight_kg=0.0,
                length_m=0.0,
                width_m=0.0,
                height_m=0.0,
                size_units=0.0,
            )
            empty_payload.prefer_multi_stop_amr = False
            self.payloads[EMPTY_PAYLOAD_NAME] = empty_payload

        # Parse waste streams and departments
        self.waste_streams: Dict[str, dict] = {
            str(item.get("name", "")).strip(): dict(item)
            for item in config.get("waste_streams", [])
            if str(item.get("name", "")).strip()
        }

        self.departments: List[dict] = list(config.get("departments", []))
        self.department_runtime: Dict[str, Dict[str, dict]] = {}
        self.department_task_counter = 0
        self._seed_initial_waste_stream_containers()

        self.route_profiles = config.get("route_profiles", {})
        self.task_generation_manager = TaskGenerationManager(
            config=config,
            clock=self.clock,
            locations=self.locations,
            payloads=self.payloads,
        )
        self.task_generation_interval_sec = float(
            (config.get("task_generation", {}) or {}).get("update_interval_sec", 900.0)
        )
        self.task_generation_horizon_sec = self._task_generation_horizon_seconds(config)

        # Parse lifts from configuration

        self.lifts: List[Lift] = []
        self.lift_initial_floors: Dict[str, int] = {}
        for item in config["lifts"]:
            floor_locations = {
                int(floor): (float(coords["x"]), float(coords["y"]))
                for floor, coords in item.get("floor_locations", {}).items()
            }

            lift = Lift(
                id=item["id"],
                served_floors=list(item["served_floors"]),
                speed_floors_per_sec=float(item["speed_floors_per_sec"]),
                door_time_sec=float(item.get("door_time_sec", 4.0)),
                boarding_time_sec=float(item.get("boarding_time_sec", 5.0)),
                floor_locations=floor_locations,
                capacity_length_m=float(item.get("capacity_length_m", 2.8)),
                capacity_width_m=float(item.get("capacity_width_m", 1.8)),
                capacity_height_m=float(item.get("capacity_height_m", 2.1)),
                capacity_size_units=float(item.get("capacity_size_units", 1.0)),
                current_floor=int(item.get("start_floor", 0)),
                car_mass_kg=float(item.get("car_mass_kg", 1200.0)),
                counterweight_ratio=float(item.get("counterweight_ratio", 0.5)),
                travel_efficiency=float(item.get("travel_efficiency", 0.75)),
                door_power_w=float(item.get("door_power_w", 800.0)),
                standby_power_w=float(item.get("standby_power_w", 120.0)),
                regen_efficiency=float(item.get("regen_efficiency", 0.2)),
                health_percent=float(item.get("health_percent", 100.0)),
                health_loss_per_journey_percent=float(
                    item.get("health_loss_per_journey_percent", 0.05)
                ),
                mean_time_between_failures_hours=float(
                    item.get("mean_time_between_failures_hours", 720.0)
                ),
                mean_time_to_repair_hours=float(
                    item.get("mean_time_to_repair_hours", 4.0)
                ),
                minimum_operational_health_percent=float(
                    item.get("minimum_operational_health_percent", 20.0)
                ),
                health_speed_penalty_at_zero=float(
                    item.get("health_speed_penalty_at_zero", 0.5)
                ),
            )

            for floor in lift.served_floors:
                if floor not in lift.floor_locations:
                    raise ValueError(
                        f"Lift {lift.id} is missing floor_locations for floor {floor}"
                    )

            self.lifts.append(lift)
            self.lift_initial_floors[lift.id] = int(lift.current_floor)

        self.graph_nodes: Dict[str, Location] = {}
        self.floor_graphs: Dict[int, Dict[str, List[dict]]] = defaultdict(
            lambda: defaultdict(list)
        )
        self.floor_reverse_graphs: Dict[int, Dict[str, List[dict]]] = defaultdict(
            lambda: defaultdict(list)
        )
        self._build_floor_graphs(config.get("corridors", {}))
        self._precompute_static_routes()
        self._init_people_movements(config.get("people_movements", []))

        # Parse AMRS from configuration

        self.amrs: List[AMR] = []
        for amr_type in config["amrs"]:
            quantity = int(amr_type.get("quantity", 1))
            for i in range(quantity):
                payload_slots = self._normalise_configured_payload_slots(amr_type)
                primary_slot = payload_slots[0]
                amr = AMR(
                    id=f"{amr_type['id']}-{i + 1}",
                    payload_capacity_kg=float(primary_slot["payload_capacity_kg"]),
                    payload_length_capacity_m=float(
                        primary_slot["payload_length_capacity_m"]
                    ),
                    payload_width_capacity_m=float(
                        primary_slot["payload_width_capacity_m"]
                    ),
                    payload_height_capacity_m=float(
                        primary_slot["payload_height_capacity_m"]
                    ),
                    length_m=float(amr_type.get("length_m", 0.8)),
                    width_m=float(amr_type.get("width_m", 0.6)),
                    height_m=float(amr_type.get("height_m", 1.2)),
                    payload_size_capacity=float(
                        amr_type.get("payload_size_capacity", 1.0)
                    ),
                    speed_m_per_sec=float(amr_type["speed_m_per_sec"]),
                    motor_power_w=float(amr_type.get("motor_power_w", 750.0)),
                    battery_capacity_kwh=float(
                        amr_type.get("battery_capacity_kwh", 5.0)
                    ),
                    battery_charge_rate_kw=float(
                        amr_type.get("battery_charge_rate_kw", 1.5)
                    ),
                    recharge_threshold_percent=float(
                        amr_type.get("recharge_threshold_percent", 20.0)
                    ),
                    battery_soc_percent=float(
                        amr_type.get("battery_soc_percent", 100.0)
                    ),
                    # AMRs no longer start from per-type start_location.
                    # They are allocated to compatible spaces at configured charging locations
                    # by _assign_initial_amrs_to_charge_inventory_spaces() below.
                    location_name=(
                        self.charge_location_names[0]
                        if self.charge_location_names
                        else config["locations"][0]["name"]
                    ),
                    is_charging=False,
                )
                amr.payload_slots = payload_slots
                amr.multi_stop_enabled = bool(
                    amr_type.get("multi_stop_enabled", len(payload_slots) > 1)
                    and len(payload_slots) > 1
                )
                amr.manual_task_compatible = len(payload_slots) == 1
                self.amrs.append(amr)

        self.amrs_by_id: Dict[str, AMR] = {amr.id: amr for amr in self.amrs}
        self._assign_initial_amrs_to_charge_inventory_spaces()
        self.amrs_by_id = {amr.id: amr for amr in self.amrs}

        # Parse tasks from configuration

        initial_tasks = []
        for task_dict in config.get("tasks", []):
            task_data = dict(task_dict)
            task_data["release_time"] = parse_release_time(
                task_data, self.clock.start_datetime
            )
            task_data.pop("release_datetime", None)
            task = Task(**task_data)
            self._prepare_task_payload_instance(task)
            initial_tasks.append(task)

        for task in initial_tasks:
            self.schedule_task_release(task)

        # Runtime task generation is now handled by amr_sim_task_generation.py.
        # Keep the old department runtime methods below for compatibility only.

        self.estimated_total_sim_time = self._estimate_total_sim_time()

        self.amr_centre_name = config["building"].get("amr_centre", "AMR_CENTRE")
        self.idle_return_window_sec = float(
            config["building"].get("idle_return_window_sec", 300.0)
        )
        self.enable_idle_return = bool(
            config["building"].get("enable_idle_return", True)
        )
        self.synthetic_task_counter = 0

        # Third-party/bin-store mass collections remove all used/full payloads
        # from a store location and replace them with matching empty containers.
        # This models a waste contractor rotating the exact number of bins used
        # at a bin store, either on scheduled visits or when finite store spaces
        # reach a configured capacity trigger.
        self.mass_collection_configs = self._normalise_mass_collection_configs(
            config.get("mass_collections", [])
        )
        self._mass_collection_last_capacity_visit = {}
        self._seed_mass_collection_empty_inventory()
        self._schedule_mass_collection_events()

        if getattr(self.task_generation_manager, "generators", []):
            self.push_event(0.0, "generator_tick", {})

    def _normalise_mass_collection_configs(self, raw_configs) -> List[dict]:
        """Normalise third-party mass collection/empty-bin rotation settings.

        Supported JSON shape:
            "mass_collections": [
              {
                "id": "D47 bin rotation",
                "enabled": true,
                "location": "D47-WASTE",
                "payloads": ["Clinical Waste Bin"],   # optional; blank = all payloads
                "days_active": ["mon", "tue", "wed", "thu", "fri"],
                "scheduled_times": ["06:00", "18:00"],
                "capacity_trigger_fraction": 0.8,
                "capacity_trigger_count": 0,
                "capacity_check_interval_minutes": 15,
                "replace_with_empty_equivalents": true
              }
            ]
        """
        if isinstance(raw_configs, dict):
            raw_items = raw_configs.get("items", [])
        else:
            raw_items = raw_configs
        if not isinstance(raw_items, list):
            return []

        configs: List[dict] = []
        for index, raw in enumerate(raw_items, start=1):
            if not isinstance(raw, dict):
                continue
            location = str(
                raw.get("location", raw.get("store_location", "")) or ""
            ).strip()
            if not location:
                continue
            if location not in self.locations:
                continue

            payloads = raw.get("payloads", raw.get("payload_names", []))
            if isinstance(payloads, str):
                payloads = [x.strip() for x in payloads.split(",")]
            payloads = [str(x).strip() for x in (payloads or []) if str(x).strip()]
            payloads = [
                x
                for x in payloads
                if x in self.payloads and not is_empty_payload_name(x)
            ]

            days = raw.get("days_active", raw.get("active_days", []))
            if isinstance(days, str):
                days = [x.strip() for x in days.split(",")]
            days = [str(x).strip().lower()[:3] for x in (days or []) if str(x).strip()]
            if not days:
                days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

            times = raw.get("scheduled_times", raw.get("schedule_times", []))
            if isinstance(times, str):
                times = [x.strip() for x in times.split(",")]
            clean_times = []
            for value in times or []:
                text = str(value or "").strip()
                try:
                    parts = [int(x) for x in text.split(":")[:2]]
                    if len(parts) == 2 and 0 <= parts[0] <= 23 and 0 <= parts[1] <= 59:
                        clean_times.append(f"{parts[0]:02d}:{parts[1]:02d}")
                except Exception:
                    continue

            try:
                capacity_fraction = float(
                    raw.get(
                        "capacity_trigger_fraction", raw.get("trigger_fraction", 0.0)
                    )
                    or 0.0
                )
            except Exception:
                capacity_fraction = 0.0
            capacity_fraction = max(0.0, min(1.0, capacity_fraction))

            try:
                capacity_count = int(
                    float(
                        raw.get("capacity_trigger_count", raw.get("trigger_count", 0))
                        or 0
                    )
                )
            except Exception:
                capacity_count = 0

            try:
                interval_min = float(
                    raw.get(
                        "capacity_check_interval_minutes",
                        raw.get("check_interval_minutes", 15.0),
                    )
                    or 15.0
                )
            except Exception:
                interval_min = 15.0
            interval_min = max(1.0, interval_min)

            config_id = (
                str(raw.get("id", raw.get("name", "")) or "").strip()
                or f"MASS-COLLECTION-{index}"
            )
            configs.append(
                {
                    "id": config_id,
                    "enabled": bool(raw.get("enabled", True)),
                    "location": location,
                    "payloads": payloads,
                    "days_active": sorted(set(days)),
                    "scheduled_times": sorted(set(clean_times)),
                    "capacity_trigger_fraction": capacity_fraction,
                    "capacity_trigger_count": max(0, capacity_count),
                    "capacity_check_interval_minutes": interval_min,
                    "replace_with_empty_equivalents": bool(
                        raw.get("replace_with_empty_equivalents", True)
                    ),
                    "notes": str(raw.get("notes", "") or ""),
                }
            )
        return configs

    def _day_key_for_time(self, sim_time: float) -> str:
        return ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][
            self.clock.sim_seconds_to_datetime(sim_time).weekday()
        ]

    def _schedule_mass_collection_events(self) -> None:
        if not self.mass_collection_configs:
            return

        day_count = (
            int(math.ceil(max(self.task_generation_horizon_sec, 0.0) / 86400.0)) + 1
        )
        start_day = self.clock.start_datetime.replace(
            hour=0, minute=0, second=0, microsecond=0
        )

        for cfg in self.mass_collection_configs:
            if not cfg.get("enabled", True):
                continue

            for day_index in range(day_count + 1):
                day_start = start_day + __import__("datetime").timedelta(days=day_index)
                day_key = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][
                    day_start.weekday()
                ]
                if day_key not in set(cfg.get("days_active", [])):
                    continue
                for hhmm in cfg.get("scheduled_times", []):
                    try:
                        hour, minute = [int(x) for x in str(hhmm).split(":")[:2]]
                        visit_dt = day_start.replace(hour=hour, minute=minute)
                        sim_time = (
                            visit_dt - self.clock.start_datetime
                        ).total_seconds()
                    except Exception:
                        continue
                    if 0.0 <= sim_time <= self.task_generation_horizon_sec:
                        self.push_event(
                            sim_time,
                            "mass_collection_visit",
                            {"config_id": cfg["id"], "trigger": "scheduled"},
                        )

            if (
                float(cfg.get("capacity_trigger_fraction", 0.0) or 0.0) > 0.0
                or int(cfg.get("capacity_trigger_count", 0) or 0) > 0
            ):
                self.push_event(
                    0.0, "mass_collection_capacity_tick", {"config_id": cfg["id"]}
                )

    def _mass_collection_config_by_id(self, config_id: str) -> Optional[dict]:
        config_id = str(config_id or "").strip()
        for cfg in self.mass_collection_configs:
            if str(cfg.get("id", "")).strip() == config_id:
                return cfg
        return None

    def _mass_collection_payload_allowed(self, cfg: dict, payload_name: str) -> bool:
        payloads = cfg.get("payloads", []) or []
        return not payloads or payload_name in set(payloads)

    def _mass_collection_candidate_records(self, cfg: dict) -> List[object]:
        location = str(cfg.get("location", "") or "").strip()
        records = []
        for record in self.payload_instance_store.records_at(location):
            payload_name = normalise_payload_name(getattr(record, "payload", ""))
            if not payload_name or not self._mass_collection_payload_allowed(
                cfg, payload_name
            ):
                continue
            if self.payload_instance_store.is_reserved(
                str(getattr(record, "instance_id", "") or "")
            ):
                continue
            metadata = getattr(record, "metadata", {}) or {}
            state = str(metadata.get("container_state", "") or "").strip().lower()
            # The rotation collects used/full bins.  Empty stock at the store is
            # deliberately left available for AMR return trips.
            if state in {"full", "used", "dirty", "awaiting_collection"}:
                records.append(record)
        return records

    def _mass_collection_capacity_limit(self, cfg: dict) -> int:
        if bool(getattr(self, "disable_inventory_spaces", False)):
            return 0
        explicit = int(cfg.get("capacity_trigger_count", 0) or 0)
        if explicit > 0:
            return explicit
        fraction = float(cfg.get("capacity_trigger_fraction", 0.0) or 0.0)
        if fraction <= 0.0:
            return 0
        spaces = self.inventory_spaces_by_location.get(
            str(cfg.get("location", "") or "").strip(), []
        )
        if not spaces:
            return 0
        return max(1, int(math.ceil(len(spaces) * fraction)))

    def _mass_collection_store_has_inventory(self, cfg: dict) -> bool:
        location = str(cfg.get("location", "") or "").strip()
        return bool(location and self._location_has_inventory_spaces(location))

    def _location_has_inventory_mass_collection_rotation(
        self, location_name: str, payload_name: str = ""
    ) -> bool:
        location_name = str(location_name or "").strip()
        payload_name = normalise_payload_name(payload_name)
        for cfg in self.mass_collection_configs:
            if not cfg.get("enabled", True):
                continue
            if str(cfg.get("location", "") or "").strip() != location_name:
                continue
            if not self._mass_collection_store_has_inventory(cfg):
                continue
            if payload_name and not self._mass_collection_payload_allowed(
                cfg, payload_name
            ):
                continue
            return True
        return False

    def _available_empty_container_record(self, location_name: str, payload_name: str):
        payload_name = normalise_payload_name(payload_name)
        for record in self.payload_instance_store.records_at(location_name):
            if getattr(record, "payload", "") != payload_name:
                continue
            if self.payload_instance_store.is_reserved(record.instance_id):
                continue
            if self._record_is_available_empty_container(record):
                return record
        return None

    def _seed_mass_collection_empty_inventory(self) -> None:
        """Populate finite bin-store inventory spaces with empty bins at simulation start.

        A mass-collection location with defined inventory spaces represents a finite
        store of empty exchange bins.  Each compatible free payload slot is stocked
        with an empty payload instance so AMR bin-return tasks can exchange full
        bins for real empty equivalents.  Locations without inventory spaces keep
        unlimited empty-bin issue behaviour and are not stocked here.
        """
        for cfg in self.mass_collection_configs:
            if not cfg.get("enabled", True):
                continue
            location = str(cfg.get("location", "") or "").strip()
            if not location or not self._mass_collection_store_has_inventory(cfg):
                continue

            allowed_payloads = list(cfg.get("payloads", []) or [])
            if not allowed_payloads:
                inferred = []
                for space in self.inventory_spaces_by_location.get(location, []):
                    payload_name = str(space.get("payload", "") or "").strip()
                    if (
                        payload_name
                        and payload_name in self.payloads
                        and not is_empty_payload_name(payload_name)
                    ):
                        inferred.append(payload_name)
                allowed_payloads = sorted(set(inferred))

            for space in self.inventory_spaces_by_location.get(location, []):
                if bool(space.get("occupied", False)):
                    continue
                compatible_payloads = []
                for payload_name in allowed_payloads:
                    payload = self.payloads.get(payload_name)
                    if payload is not None and self._inventory_space_can_fit_payload(
                        space, payload
                    ):
                        compatible_payloads.append(payload_name)
                if not compatible_payloads:
                    continue
                payload_name = compatible_payloads[0]
                instance_id = self.payload_instance_store.make_instance_id(
                    payload_name,
                    f"{cfg.get('id', 'mass-collection')}-initial-empty",
                )
                self.payload_instance_store.store(
                    location,
                    payload_name,
                    instance_id,
                    source_task_id=str(cfg.get("id", "")),
                    metadata={
                        "task_source": "mass_collection_initial_empty_stock",
                        "mass_collection_id": str(cfg.get("id", "")),
                        "container_type": payload_name,
                        "container_state": "empty",
                    },
                )
                space["occupied"] = True
                space["payload"] = payload_name
                space["payload_instance_id"] = instance_id
                space["task_id"] = str(cfg.get("id", ""))
                space["reserved_by_task"] = ""
                self._log_payload_location_event(
                    "location_payload_enter", location, payload_name, instance_id
                )
                self._record_payload_population_snapshot()

    def _task_is_full_bin_dropoff_to_inventory_rotation(
        self, task: Task, payload: Optional[PayloadType] = None
    ) -> bool:
        if payload is None:
            payload = self._payload_for_task(task)
        if payload is None:
            return False
        if bool(getattr(task, "is_return_task", False)):
            return False
        if not self._location_has_inventory_mass_collection_rotation(
            task.dropoff, payload.name
        ):
            return False
        state = self._payload_instance_container_state_for_task(task)
        return state == "full"

    def _task_can_exchange_with_store_empty(
        self, task: Task, payload: PayloadType
    ) -> bool:
        if not self._task_is_full_bin_dropoff_to_inventory_rotation(task, payload):
            return False
        return (
            self._available_empty_container_record(task.dropoff, payload.name)
            is not None
        )

    def _consume_store_empty_for_exchange(
        self, task: Task, payload: PayloadType
    ) -> None:
        """Stage an empty bin for the return leg and free its store slot for the full bin."""
        if bool(getattr(task, "return_same_payload_instance", False)):
            return
        if not self._task_is_full_bin_dropoff_to_inventory_rotation(task, payload):
            return
        if str(getattr(task, "exchange_empty_payload_instance_id", "") or "").strip():
            return
        record = self._available_empty_container_record(task.dropoff, payload.name)
        if record is None:
            raise RuntimeError(
                f"No '{payload.name}'s available at {task.dropoff} for exchange"
            )
        removed = self.payload_instance_store.pickup(
            task.dropoff,
            payload_name=payload.name,
            instance_id=str(getattr(record, "instance_id", "") or ""),
        )
        if removed is None:
            raise RuntimeError(
                f"No '{payload.name}'s available at {task.dropoff} for exchange"
            )
        self._remove_payload_record_from_inventory(task.dropoff, removed.instance_id)
        task.exchange_empty_payload_instance_id = removed.instance_id
        task.exchange_empty_payload_metadata = dict(
            getattr(removed, "metadata", {}) or {}
        )

    def _remove_payload_record_from_inventory(
        self, location_name: str, instance_id: str
    ) -> None:
        for space in self.inventory_spaces_by_location.get(location_name, []):
            if (
                str(space.get("payload_instance_id", "") or "").strip()
                != str(instance_id or "").strip()
            ):
                continue
            space["occupied"] = False
            space["payload"] = ""
            space["payload_instance_id"] = ""
            space["task_id"] = ""
            space["reserved_by_task"] = ""

    def _store_mass_collection_empty_equivalent(self, cfg: dict, full_record) -> str:
        location = str(cfg.get("location", "") or "").strip()
        payload_name = normalise_payload_name(getattr(full_record, "payload", ""))
        if not location or not payload_name:
            return ""
        new_instance_id = self.payload_instance_store.make_instance_id(
            payload_name,
            f"{cfg.get('id', 'mass-collection')}-empty-equivalent",
        )
        old_metadata = getattr(full_record, "metadata", {}) or {}
        metadata = dict(old_metadata)
        metadata.update(
            {
                "task_source": "third_party_mass_collection",
                "mass_collection_id": str(cfg.get("id", "")),
                "source_full_payload_instance_id": str(
                    getattr(full_record, "instance_id", "") or ""
                ),
                "container_state": "empty",
            }
        )
        self.payload_instance_store.store(
            location,
            payload_name,
            new_instance_id,
            source_task_id=str(cfg.get("id", "")),
            metadata=metadata,
        )

        payload = self.payloads.get(payload_name)
        if payload is not None and self._location_has_inventory_spaces(location):
            space = self._find_free_inventory_space(location, payload)
            if space is not None:
                space["occupied"] = True
                space["payload"] = payload_name
                space["payload_instance_id"] = new_instance_id
                space["task_id"] = str(cfg.get("id", ""))
                space["reserved_by_task"] = ""
        self._log_payload_location_event(
            "location_payload_enter", location, payload_name, new_instance_id
        )
        self._record_payload_population_snapshot()
        return new_instance_id

    def _execute_mass_collection_visit(
        self, cfg: dict, now: float, trigger: str
    ) -> None:
        if not cfg or not cfg.get("enabled", True):
            return
        candidates = self._mass_collection_candidate_records(cfg)
        if not candidates:
            self.log_step(
                event_time=now,
                event_type="mass_collection_visit",
                details=f"Mass collection {cfg.get('id', '')} found no used/full payloads at {cfg.get('location', '')}",
                from_location=str(cfg.get("location", "")),
                to_location=str(cfg.get("location", "")),
                status="completed",
                task_source="third_party_mass_collection",
            )
            return

        collected_ids = []
        replacement_ids = []
        location = str(cfg.get("location", "") or "").strip()
        for record in list(candidates):
            instance_id = str(getattr(record, "instance_id", "") or "")
            payload_name = normalise_payload_name(getattr(record, "payload", ""))
            removed = self.payload_instance_store.pickup(
                location, payload_name=payload_name, instance_id=instance_id
            )
            if removed is None:
                continue
            self._remove_payload_record_from_inventory(location, instance_id)
            self._log_payload_location_event(
                "location_payload_exit", location, payload_name, instance_id
            )
            self._record_payload_population_snapshot()
            collected_ids.append(instance_id)
            if cfg.get(
                "replace_with_empty_equivalents", True
            ) and self._mass_collection_store_has_inventory(cfg):
                replacement_id = self._store_mass_collection_empty_equivalent(
                    cfg, removed
                )
                if replacement_id:
                    replacement_ids.append(replacement_id)

        self._mass_collection_last_capacity_visit[str(cfg.get("id", ""))] = now
        self.log_step(
            event_time=now,
            event_type="mass_collection_visit",
            details=(
                f"Mass collection {cfg.get('id', '')} collected {len(collected_ids)} used/full payload(s) "
                f"from {location} and delivered {len(replacement_ids)} empty equivalent(s); trigger={trigger}; "
                f"collected={collected_ids}; replacements={replacement_ids}"
            ),
            from_location=location,
            to_location=location,
            payload_name=";".join(
                sorted(
                    {
                        normalise_payload_name(getattr(r, "payload", ""))
                        for r in candidates
                    }
                )
            ),
            payload_instance_id=";".join(replacement_ids),
            status="completed",
            task_source="third_party_mass_collection",
        )
        # Newly delivered empty equivalents may unblock pending return tasks.
        self._try_assign_tasks(now)

    def _handle_mass_collection_capacity_tick(self, cfg: dict, now: float) -> None:
        if not cfg or not cfg.get("enabled", True):
            return
        interval = max(
            60.0, float(cfg.get("capacity_check_interval_minutes", 15.0) or 15.0) * 60.0
        )
        next_tick = now + interval
        if next_tick <= self.task_generation_horizon_sec:
            self.push_event(
                next_tick,
                "mass_collection_capacity_tick",
                {"config_id": cfg.get("id", "")},
            )

        if self._day_key_for_time(now) not in set(cfg.get("days_active", [])):
            return
        limit = self._mass_collection_capacity_limit(cfg)
        if limit <= 0:
            return
        candidates = self._mass_collection_candidate_records(cfg)
        if len(candidates) < limit:
            return
        last = float(
            self._mass_collection_last_capacity_visit.get(str(cfg.get("id", "")), -1e18)
        )
        if now - last < interval - 1e-9:
            return
        self._execute_mass_collection_visit(cfg, now, trigger="capacity")

    def _location_has_mass_collection_rotation(
        self, location_name: str, payload_name: str = ""
    ) -> bool:
        location_name = str(location_name or "").strip()
        payload_name = normalise_payload_name(payload_name)
        for cfg in self.mass_collection_configs:
            if not cfg.get("enabled", True):
                continue
            if str(cfg.get("location", "") or "").strip() != location_name:
                continue
            if payload_name and not self._mass_collection_payload_allowed(
                cfg, payload_name
            ):
                continue
            return True
        return False

    def _task_generation_horizon_seconds(self, config: dict) -> float:
        sim_cfg = config.get("simulation", {}) or {}
        if sim_cfg.get("end_datetime"):
            try:
                return max(
                    0.0,
                    (
                        parse_datetime(sim_cfg["end_datetime"])
                        - self.clock.start_datetime
                    ).total_seconds(),
                )
            except Exception:
                pass
        if sim_cfg.get("duration_hours") is not None:
            return max(0.0, float(sim_cfg.get("duration_hours", 0.0)) * 3600.0)
        if sim_cfg.get("duration_days") is not None:
            return max(0.0, float(sim_cfg.get("duration_days", 0.0)) * 86400.0)

        latest = 0.0
        for task in config.get("tasks", []):
            try:
                latest = max(
                    latest, parse_release_time(dict(task), self.clock.start_datetime)
                )
            except Exception:
                continue
        return max(latest + 86400.0, 86400.0)

    def _normalise_configured_payload_slots(self, amr_type: dict) -> List[dict]:
        raw_slots = (
            amr_type.get("payload_slots", []) if isinstance(amr_type, dict) else []
        )
        slots = []
        if isinstance(raw_slots, list):
            for index, slot in enumerate(raw_slots, start=1):
                if not isinstance(slot, dict):
                    continue
                slots.append(
                    {
                        "name": str(slot.get("name", "")).strip() or f"Slot {index}",
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
                            for x in (slot.get("allowed_payload_orientations", ["lengthways", "sideways"]) or ["lengthways"])
                            if str(x).strip().lower() in {"lengthways", "sideways"}
                        ] or ["lengthways"],
                    }
                )
        if not slots:
            slots.append(
                {
                    "name": "Slot 1",
                    "payload_capacity_kg": float(
                        amr_type.get("payload_capacity_kg", 100.0) or 100.0
                    ),
                    "payload_length_capacity_m": float(
                        amr_type.get(
                            "payload_length_capacity_m",
                            amr_type.get("payload_size_capacity", 1.0),
                        )
                        or 1.0
                    ),
                    "payload_width_capacity_m": float(
                        amr_type.get(
                            "payload_width_capacity_m",
                            amr_type.get("payload_size_capacity", 1.0),
                        )
                        or 1.0
                    ),
                    "payload_height_capacity_m": float(
                        amr_type.get(
                            "payload_height_capacity_m",
                            amr_type.get("payload_size_capacity", 1.0),
                        )
                        or 1.0
                    ),
                    "allowed_payload_orientations": ["lengthways", "sideways"],
                }
            )
        return slots

    def _space_points_dimensions(self, points: list) -> Tuple[float, float]:
        if not points:
            return 0.0, 0.0

        xs = []
        ys = []
        for p in points:
            try:
                if "dx" in p and "dy" in p:
                    xs.append(float(p.get("dx", 0.0)))
                    ys.append(float(p.get("dy", 0.0)))
                else:
                    xs.append(float(p.get("x", 0.0)))
                    ys.append(float(p.get("y", 0.0)))
            except Exception:
                continue

        if not xs or not ys:
            return 0.0, 0.0

        return abs(max(xs) - min(xs)), abs(max(ys) - min(ys))

    def _payload_log_name(self, payload_name: str) -> str:
        return (
            ""
            if is_empty_payload_name(payload_name)
            else str(payload_name or "").strip()
        )

    def _payload_for_task(self, task: Task) -> Optional[PayloadType]:
        payload_name = str(getattr(task, "payload", "") or "").strip()
        if is_empty_payload_name(payload_name):
            return self.payloads.get(EMPTY_PAYLOAD_NAME)
        return self.payloads.get(payload_name)

    def _payload_prefers_multi_stop_amr(self, payload: Optional[PayloadType]) -> bool:
        if payload is None or is_empty_payload_name(payload.name):
            return False
        return bool(getattr(payload, "prefer_multi_stop_amr", False))

    def _task_prefers_multi_stop_amr(self, task: Task) -> bool:
        return self._payload_prefers_multi_stop_amr(self._payload_for_task(task))

    def _runtime_amr_payload_slots(self, amr: AMR) -> List[dict]:
        slots = getattr(amr, "payload_slots", None)
        clean = []
        if isinstance(slots, list):
            for index, slot in enumerate(slots, start=1):
                if not isinstance(slot, dict):
                    continue
                clean.append(
                    {
                        "name": str(slot.get("name", "")).strip() or f"Slot {index}",
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
                            for x in (slot.get("allowed_payload_orientations", ["lengthways", "sideways"]) or ["lengthways"])
                            if str(x).strip().lower() in {"lengthways", "sideways"}
                        ] or ["lengthways"],
                    }
                )
        if not clean:
            clean.append(
                {
                    "name": "Slot 1",
                    "payload_capacity_kg": float(
                        getattr(amr, "payload_capacity_kg", 0.0) or 0.0
                    ),
                    "payload_length_capacity_m": float(
                        getattr(amr, "payload_length_capacity_m", 0.0) or 0.0
                    ),
                    "payload_width_capacity_m": float(
                        getattr(amr, "payload_width_capacity_m", 0.0) or 0.0
                    ),
                    "payload_height_capacity_m": float(
                        getattr(amr, "payload_height_capacity_m", 0.0) or 0.0
                    ),
                    "allowed_payload_orientations": ["lengthways", "sideways"],
                }
            )
        return clean

    def _is_multi_stop_amr(self, amr: AMR) -> bool:
        slots = self._runtime_amr_payload_slots(amr)
        return bool(getattr(amr, "multi_stop_enabled", False)) and len(slots) > 1

    def _payload_orientation_dimensions(
        self, payload: PayloadType, orientation: str
    ) -> Tuple[float, float, float]:
        orientation = str(orientation or "lengthways").strip().lower()
        length = float(payload.length_m)
        width = float(payload.width_m)
        if orientation == "sideways":
            length, width = width, length
        return length, width, float(payload.height_m)

    def _compatible_payload_orientations(
        self, payload: PayloadType, slot: dict
    ) -> List[str]:
        payload_allowed = [
            str(x).strip().lower()
            for x in (getattr(payload, "allowed_carry_orientations", None) or ["lengthways", "sideways"])
            if str(x).strip().lower() in {"lengthways", "sideways"}
        ]
        slot_allowed = [
            str(x).strip().lower()
            for x in (slot.get("allowed_payload_orientations", ["lengthways", "sideways"]) or ["lengthways"])
            if str(x).strip().lower() in {"lengthways", "sideways"}
        ]
        compatible = []
        for orientation in ("lengthways", "sideways"):
            if orientation not in payload_allowed or orientation not in slot_allowed:
                continue
            length, width, height = self._payload_orientation_dimensions(payload, orientation)
            if (
                float(payload.weight_kg) <= float(slot.get("payload_capacity_kg", 0.0) or 0.0)
                and length <= float(slot.get("payload_length_capacity_m", 0.0) or 0.0)
                and width <= float(slot.get("payload_width_capacity_m", 0.0) or 0.0)
                and height <= float(slot.get("payload_height_capacity_m", 0.0) or 0.0)
            ):
                compatible.append(orientation)
        return compatible

    def _payload_fits_slot(
        self, payload: PayloadType, slot: dict, orientation: Optional[str] = None
    ) -> bool:
        orientations = self._compatible_payload_orientations(payload, slot)
        if orientation is None:
            return bool(orientations)
        return str(orientation or "").strip().lower() in orientations

    def _choose_payload_orientation(
        self, amr: AMR, payload: PayloadType, preferred_slot: str = ""
    ) -> Tuple[str, str]:
        for slot in self._runtime_amr_payload_slots(amr):
            slot_name = str(slot.get("name", "") or "")
            if preferred_slot and slot_name != preferred_slot:
                continue
            orientations = self._compatible_payload_orientations(payload, slot)
            if orientations:
                # Prefer lengthways because it usually minimises lateral swept width.
                orientation = "lengthways" if "lengthways" in orientations else orientations[0]
                return slot_name, orientation
        return "", ""

    def _amr_can_carry_payload(self, amr: AMR, payload: PayloadType) -> bool:
        if is_empty_payload_name(payload.name):
            return True
        return bool(self._choose_payload_orientation(amr, payload)[0])

    def _assign_tasks_to_amr_slots(
        self, amr: AMR, tasks: List[Task]
    ) -> Optional[Dict[str, str]]:
        slots = self._runtime_amr_payload_slots(amr)
        used_slots = set()
        assignments: Dict[str, str] = {}
        sortable = []
        for task in tasks:
            payload = self._payload_for_task(task)
            if payload is None or is_empty_payload_name(payload.name):
                return None
            volume = float(payload.length_m) * float(payload.width_m) * float(payload.height_m)
            sortable.append((float(payload.weight_kg), volume, task.id, task, payload))
        for _weight, _volume, _task_id, task, payload in sorted(sortable, reverse=True):
            assigned = None
            assigned_orientation = ""
            for slot in slots:
                slot_name = str(slot.get("name", ""))
                if slot_name in used_slots:
                    continue
                orientations = self._compatible_payload_orientations(payload, slot)
                if orientations:
                    assigned = slot_name
                    assigned_orientation = "lengthways" if "lengthways" in orientations else orientations[0]
                    break
            if assigned is None:
                return None
            assignments[task.id] = assigned
            task.payload_orientation = assigned_orientation
            used_slots.add(assigned)
        return assignments

    def _make_aggregate_payload(self, tasks: List[Task]) -> PayloadType:
        oriented_payloads = []
        for task in tasks:
            payload = self._payload_for_task(task)
            if payload is None:
                continue
            orientation = str(getattr(task, "payload_orientation", "lengthways") or "lengthways")
            length, width, height = self._payload_orientation_dimensions(payload, orientation)
            oriented_payloads.append((payload, length, width, height))
        if not oriented_payloads:
            return self.payloads[EMPTY_PAYLOAD_NAME]
        # Multi-stop payloads occupy separate AMR slots.  For corridor and lift
        # clearance use the largest oriented envelope actually carried, rather
        # than silently rotating every payload back to its catalogue orientation.
        return PayloadType(
            name="multi_payload",
            weight_kg=sum(float(payload.weight_kg) for payload, _l, _w, _h in oriented_payloads),
            length_m=max(length for _payload, length, _width, _height in oriented_payloads),
            width_m=max(width for _payload, _length, width, _height in oriented_payloads),
            height_m=max(height for _payload, _length, _width, height in oriented_payloads),
            size_units=sum(
                float(getattr(payload, "size_units", 0.0) or 0.0)
                for payload, _length, _width, _height in oriented_payloads
            ),
            allowed_carry_orientations=["lengthways"],
        )

    def _multi_stop_task_is_eligible(self, task: Task) -> bool:
        if getattr(task, "is_idle_return", False) or getattr(
            task, "is_return_task", False
        ):
            return False
        pickup_loc = self.locations.get(str(getattr(task, "pickup", "") or ""))
        dropoff_loc = self.locations.get(str(getattr(task, "dropoff", "") or ""))
        if bool(getattr(pickup_loc, "wash_cycle_required", False)) or bool(
            getattr(dropoff_loc, "wash_cycle_required", False)
        ):
            return False
        if bool(getattr(task, "manual_task", False)) or bool(
            getattr(task, "manual_task_only", False)
        ):
            return False
        # AMR-locked continuation tasks, such as waste-bin returns, must remain
        # single-task routes so they can be assigned back to the same AMR.
        if str(getattr(task, "locked_amr_id", "") or "").strip():
            return False
        if task.release_time > self.current_time:
            return False
        if task.pickup not in self.locations or task.dropoff not in self.locations:
            return False
        payload = self._payload_for_task(task)
        if payload is None or is_empty_payload_name(payload.name):
            return False
        if not self._pickup_instance_available(task):
            self._set_task_pending_reason(
                task, self._pickup_instance_pending_reason(task)
            )
            return False
        if self._location_has_payload_inventory_spaces(task.dropoff):
            if self._find_free_inventory_space(
                task.dropoff, payload
            ) is None and not self._task_can_exchange_with_store_empty(task, payload):
                self._set_task_pending_reason(
                    task, self._inventory_pending_reason(task.dropoff, payload)
                )
                return False
        return True

    def _multi_stop_batch_for_amr(self, amr: AMR) -> Optional[List[Task]]:
        if not self._is_multi_stop_amr(amr):
            return None
        slot_count = len(self._runtime_amr_payload_slots(amr))
        released = [
            item[3]
            for item in sorted(self.pending_tasks)
            if self._multi_stop_task_is_eligible(item[3])
        ]
        # Payloads marked as preferring multi-stop AMRs should get first access
        # to multi-stop slots, while the existing priority/release order is
        # retained inside each preference group.
        released.sort(
            key=lambda task: (
                0 if self._task_prefers_multi_stop_amr(task) else 1,
                int(getattr(task, "priority", 100) or 100),
                float(getattr(task, "release_time", 0.0) or 0.0),
                str(getattr(task, "id", "")),
            )
        )
        if len(released) < 2:
            return None
        selected: List[Task] = []
        route_signature = None
        candidate_limit = max(
            slot_count, min(len(released), self.max_multi_stop_candidate_tasks)
        )
        for task in released[:candidate_limit]:
            # Keep a batch on the same route profile / dirty-label state so route restrictions stay predictable.
            signature = (
                str(getattr(task, "route_profile", "") or ""),
                bool("dirty" in list(getattr(task, "labels", []) or [])),
            )
            if route_signature is None:
                route_signature = signature
            if signature != route_signature:
                continue
            trial = selected + [task]
            if len(trial) > slot_count:
                continue
            if self._assign_tasks_to_amr_slots(amr, trial) is None:
                continue
            selected.append(task)
            if len(selected) >= slot_count:
                break
        return selected if len(selected) >= 2 else None

    def _estimate_route_seconds(
        self,
        amr: AMR,
        from_loc: Location,
        to_loc: Location,
        payload: PayloadType,
        rules: Optional[dict] = None,
        start_time_value: Optional[float] = None,
    ) -> float:
        if from_loc.floor == to_loc.floor:
            route = self._same_floor_segments(
                amr, from_loc, to_loc, rules=rules, start_time_value=start_time_value
            )
            return math.inf if route is None else float(route[1])
        plan = self._nearest_compatible_lift_plan(
            start_time_value if start_time_value is not None else self.current_time,
            amr,
            from_loc,
            to_loc,
            payload,
            rules=rules,
        )
        if plan is None:
            return math.inf
        return float(
            plan["final_finish"]
            - (start_time_value if start_time_value is not None else self.current_time)
        )

    def _ordered_multi_stop_legs(
        self,
        amr: AMR,
        tasks: List[Task],
        start_loc: Location,
        aggregate_payload: PayloadType,
        loaded_rules: Optional[dict],
        start_time_value: float,
    ) -> List[Tuple[str, Task]]:
        ordered: List[Tuple[str, Task]] = []
        current = start_loc
        t = start_time_value
        remaining = list(tasks)
        empty_payload = self.payloads.get(EMPTY_PAYLOAD_NAME, aggregate_payload)
        carrying = False
        while remaining:
            payload_for_leg = aggregate_payload if carrying else empty_payload
            rules = loaded_rules if carrying else None
            next_task = min(
                remaining,
                key=lambda task: self._estimate_route_seconds(
                    amr,
                    current,
                    self.locations[task.pickup],
                    payload_for_leg,
                    rules=rules,
                    start_time_value=t,
                ),
            )
            ordered.append(("pickup", next_task))
            t += self._estimate_route_seconds(
                amr,
                current,
                self.locations[next_task.pickup],
                payload_for_leg,
                rules=rules,
                start_time_value=t,
            )
            t += self.load_unload_time_sec
            current = self.locations[next_task.pickup]
            carrying = True
            remaining.remove(next_task)
        remaining = list(tasks)
        while remaining:
            next_task = min(
                remaining,
                key=lambda task: self._estimate_route_seconds(
                    amr,
                    current,
                    self.locations[task.dropoff],
                    aggregate_payload,
                    rules=loaded_rules,
                    start_time_value=t,
                ),
            )
            ordered.append(("dropoff", next_task))
            t += self._estimate_route_seconds(
                amr,
                current,
                self.locations[next_task.dropoff],
                aggregate_payload,
                rules=loaded_rules,
                start_time_value=t,
            )
            t += self.load_unload_time_sec
            current = self.locations[next_task.dropoff]
            remaining.remove(next_task)
        return ordered

    def _estimate_multi_stop_for_amr(
        self, amr: AMR, tasks: List[Task], reserve: bool = False
    ) -> Optional[dict]:
        try:
            if not tasks or len(tasks) < 2:
                return None
            slot_assignments = self._assign_tasks_to_amr_slots(amr, tasks)
            if slot_assignments is None:
                return None
            for task in tasks:
                payload = self._payload_for_task(task)
                if payload is None or not self._amr_can_carry_payload(amr, payload):
                    self._set_task_pending_reason(
                        task, "No AMR slot has sufficient payload weight/dimensions"
                    )
                    return None
                if not self._pickup_instance_available(task):
                    self._set_task_pending_reason(
                        task, self._pickup_instance_pending_reason(task)
                    )
                    return None
                if self._location_has_payload_inventory_spaces(task.dropoff):
                    if self._find_free_inventory_space(
                        task.dropoff, payload
                    ) is None and not self._task_can_exchange_with_store_empty(
                        task, payload
                    ):
                        self._set_task_pending_reason(
                            task, self._inventory_pending_reason(task.dropoff, payload)
                        )
                        return None

            aggregate_payload = self._make_aggregate_payload(tasks)
            empty_payload = self.payloads.get(EMPTY_PAYLOAD_NAME, aggregate_payload)
            amr_loc = self.locations[amr.location_name]
            loaded_rules = self._resolve_task_route_rules(tasks[0])
            t = max(
                self.current_time,
                amr.available_time,
                max(task.release_time for task in tasks),
            )
            task_start_time = t
            ordered_legs = self._ordered_multi_stop_legs(
                amr, tasks, amr_loc, aggregate_payload, loaded_rules, t
            )

            segments: List[dict] = []
            total = 0.0
            lift_energy_kwh_total = 0.0
            lift_empty_sec_total = 0.0
            lift_loaded_sec_total = 0.0
            travel_to_first_pickup_sec = 0.0
            loaded_travel_sec_total = 0.0
            current_location = amr_loc
            carrying_count = 0

            departure_space_name = str(
                getattr(amr, "inventory_space_name", "") or ""
            ).strip()
            if departure_space_name:
                departure_segments, departure_duration, _departure_distance = (
                    self._local_manoeuvre_segments_from_inventory_space(
                        amr, amr_loc.name, departure_space_name, t, purpose="amr_unstow"
                    )
                )
                if departure_segments:
                    segments.extend(departure_segments)
                    t += departure_duration
                    total += departure_duration

            def move_between(
                location_a, location_b, current_time_value, payload_for_leg, rules=None
            ):
                nonlocal total, lift_energy_kwh_total, lift_empty_sec_total, lift_loaded_sec_total
                if location_a.floor == location_b.floor:
                    route = self._same_floor_segments(
                        amr,
                        location_a,
                        location_b,
                        rules=rules,
                        start_time_value=current_time_value,
                        payload=payload_for_leg,
                        orientation="lengthways",
                    )
                    if route is None:
                        return math.inf, None, 0.0, 0.0
                    same_segments, route_duration, route_distance = route
                    if reserve:
                        self._reserve_corridor_segments(
                            amr, same_segments, current_time_value
                        )
                    total += route_duration
                    return (
                        current_time_value + route_duration,
                        same_segments,
                        route_duration,
                        route_distance,
                    )

                plan = self._nearest_compatible_lift_plan(
                    current_time_value,
                    amr,
                    location_a,
                    location_b,
                    payload_for_leg,
                    rules=rules,
                    orientation="lengthways",
                )
                if plan is None:
                    return math.inf, None, 0.0, 0.0
                lift_energy_kwh_total += total_lift_energy_kwh(
                    lift=plan["lift"],
                    payload=payload_for_leg,
                    floor_height_m=self.floor_height_m,
                    reposition_floor_delta=(
                        plan["reposition_to_floor"] - plan["reposition_from_floor"]
                    ),
                    loaded_floor_delta=(location_b.floor - location_a.floor),
                    wait_time_sec=plan["wait_time"],
                    door_time_sec=plan["lift"].door_time_sec,
                )
                lift_empty_sec_total += float(plan.get("reposition_sec", 0.0))
                lift_loaded_sec_total += float(plan.get("loaded_travel_sec", 0.0))
                if reserve:
                    self._reserve_corridor_segments(
                        amr, plan["to_lift_segments"], current_time_value
                    )
                    self._reserve_corridor_segments(
                        amr, plan["from_lift_segments"], plan["lift_finish"]
                    )
                    self._reserve_lift_journey(plan, amr.id)
                    self._apply_lift_journey_wear(
                        plan["lift"],
                        journey_operating_sec=float(plan.get("reposition_sec", 0.0))
                        + float(plan.get("loaded_travel_sec", 0.0)),
                        journey_finish_time=plan["lift_finish"],
                    )
                transfer_segments = list(plan["to_lift_segments"])
                if plan["wait_time"] > 0:
                    transfer_segments.append(
                        {
                            "type": "wait_for_lift",
                            "lift_id": plan["lift"].id,
                            "from": plan["origin_lift"].name,
                            "to": plan["origin_lift"].name,
                            "duration": plan["wait_time"],
                            "distance_m": 0.0,
                        }
                    )
                if plan.get("reposition_sec", 0.0) > 0:
                    transfer_segments.append(
                        {
                            "type": "lift_reposition",
                            "lift_id": plan["lift"].id,
                            "from": f"{plan['lift'].id}-F{plan['reposition_from_floor']}",
                            "to": f"{plan['lift'].id}-F{plan['reposition_to_floor']}",
                            "amr_wait_node": plan["origin_lift"].name,
                            "from_floor": plan["reposition_from_floor"],
                            "to_floor": plan["reposition_to_floor"],
                            "wait_time": 0.0,
                            "duration": plan["reposition_sec"],
                            "distance_m": abs(
                                plan["reposition_to_floor"]
                                - plan["reposition_from_floor"]
                            )
                            * self.floor_height_m,
                            "vertical_distance_m": abs(
                                plan["reposition_to_floor"]
                                - plan["reposition_from_floor"]
                            )
                            * self.floor_height_m,
                        }
                    )
                transfer_segments.append(
                    {
                        "type": "lift_transfer",
                        "lift_id": plan["lift"].id,
                        "from": plan["origin_lift"].name,
                        "to": plan["destination_lift"].name,
                        "from_floor": location_a.floor,
                        "to_floor": location_b.floor,
                        "wait_time": 0.0,
                        "duration": max(
                            0.0,
                            plan["lift_finish"]
                            - plan.get(
                                "reposition_finish",
                                plan["lift_start"] + plan.get("reposition_sec", 0.0),
                            ),
                        ),
                        "distance_m": plan["vertical_distance_m"],
                        "vertical_distance_m": plan["vertical_distance_m"],
                    }
                )
                transfer_segments.extend(plan["from_lift_segments"])
                segment_duration = plan["final_finish"] - current_time_value
                total += segment_duration
                return (
                    plan["final_finish"],
                    transfer_segments,
                    segment_duration,
                    (
                        plan["to_lift_distance_m"]
                        + plan["vertical_distance_m"]
                        + plan["from_lift_distance_m"]
                    ),
                )

            grouped_legs: List[Tuple[str, List[Task]]] = []
            idx = 0
            while idx < len(ordered_legs):
                action, task = ordered_legs[idx]
                if action == "pickup":
                    pickup_location_name = task.pickup
                    group = [task]
                    idx += 1
                    while idx < len(ordered_legs):
                        next_action, next_task = ordered_legs[idx]
                        if (
                            next_action != "pickup"
                            or next_task.pickup != pickup_location_name
                        ):
                            break
                        group.append(next_task)
                        idx += 1
                    grouped_legs.append(("pickup", group))
                    continue

                grouped_legs.append((action, [task]))
                idx += 1

            for action, leg_tasks in grouped_legs:
                task = leg_tasks[0]
                target_location = self.locations[
                    task.pickup if action == "pickup" else task.dropoff
                ]
                payload_for_leg = (
                    aggregate_payload if carrying_count > 0 else empty_payload
                )
                rules = loaded_rules if carrying_count > 0 else None
                t, new_segments, seg_time, _distance = move_between(
                    current_location, target_location, t, payload_for_leg, rules=rules
                )
                if new_segments is None or math.isinf(t):
                    return None
                if carrying_count == 0 and action == "pickup":
                    travel_to_first_pickup_sec += seg_time
                else:
                    loaded_travel_sec_total += seg_time
                for seg in new_segments:
                    seg.setdefault("multi_stop_task_ids", [x.id for x in tasks])
                    seg.setdefault("payload_slot_count", len(slot_assignments))
                segments.extend(new_segments)

                location_start = self._find_next_available_time(
                    target_location.name, t, self.load_unload_time_sec
                )
                location_wait = location_start - t
                if location_wait > 0:
                    segments.append(
                        {
                            "type": "wait_for_location",
                            "from": target_location.name,
                            "to": target_location.name,
                            "duration": location_wait,
                            "distance_m": 0.0,
                            "location": target_location.name,
                            "task_ids": [x.id for x in leg_tasks],
                            "multi_stop_task_ids": [x.id for x in tasks],
                        }
                    )
                    total += location_wait
                    t = location_start
                if reserve:
                    self._reserve_location(
                        target_location.name, t, t + self.load_unload_time_sec
                    )

                inventory_space_name = ""
                if action == "dropoff":
                    payload = self._payload_for_task(task)
                    if reserve and payload is not None:
                        reserved_space = self._reserve_inventory_space_for_task(
                            task, payload
                        )
                        if (
                            self._location_has_payload_inventory_spaces(
                                target_location.name
                            )
                            and reserved_space is None
                            and not self._task_can_exchange_with_store_empty(
                                task, payload
                            )
                        ):
                            self._set_task_pending_reason(
                                task,
                                self._inventory_pending_reason(
                                    target_location.name, payload
                                ),
                            )
                            return None
                        if reserved_space is not None:
                            inventory_space_name = str(reserved_space.get("name", ""))
                            local_segments, local_duration, _local_distance = (
                                self._local_manoeuvre_segments_to_inventory_space(
                                    amr,
                                    target_location.name,
                                    reserved_space,
                                    t,
                                    purpose="payload_dropoff",
                                )
                            )
                            if local_segments:
                                for local_segment in local_segments:
                                    local_segment.setdefault(
                                        "task_id", ",".join(x.id for x in leg_tasks)
                                    )
                                    local_segment.setdefault(
                                        "task_ids", [x.id for x in leg_tasks]
                                    )
                                    local_segment.setdefault(
                                        "payload",
                                        ",".join(
                                            str(getattr(x, "payload", "") or "")
                                            for x in leg_tasks
                                        ),
                                    )
                                    local_segment.setdefault(
                                        "payload_instance_id",
                                        ",".join(
                                            str(
                                                getattr(x, "payload_instance_id", "")
                                                or ""
                                            )
                                            for x in leg_tasks
                                        ),
                                    )
                                    local_segment.setdefault(
                                        "multi_stop_task_ids", [x.id for x in tasks]
                                    )
                                segments.extend(local_segments)
                                t += local_duration
                                total += local_duration
                                loaded_travel_sec_total += local_duration

                segment_task_ids = [x.id for x in leg_tasks]
                segments.append(
                    {
                        "type": action,
                        "location": target_location.name,
                        "duration": self.load_unload_time_sec,
                        "task_id": ",".join(segment_task_ids),
                        "task_ids": segment_task_ids,
                        "payload": ",".join(
                            str(getattr(x, "payload", "") or "") for x in leg_tasks
                        ),
                        "payload_instance_id": ",".join(
                            str(getattr(x, "payload_instance_id", "") or "")
                            for x in leg_tasks
                        ),
                        "slot_name": ",".join(
                            slot_assignments.get(x.id, "")
                            for x in leg_tasks
                            if slot_assignments.get(x.id, "")
                        ),
                        "inventory_space": inventory_space_name
                        or getattr(task, "assigned_inventory_space", ""),
                        "multi_stop_task_ids": [x.id for x in tasks],
                    }
                )
                t += self.load_unload_time_sec
                total += self.load_unload_time_sec
                current_location = target_location
                if action == "pickup":
                    carrying_count += len(leg_tasks)
                elif action == "dropoff":
                    carrying_count = max(0, carrying_count - len(leg_tasks))

            corridor_energy_kwh = total_route_energy_kwh(
                amr,
                aggregate_payload,
                travel_to_first_pickup_sec,
                loaded_travel_sec_total,
            )
            actual_energy_kwh = corridor_energy_kwh + lift_energy_kwh_total
            projected_battery_soc_after = (
                100.0
                * max(0.0, amr.battery_energy_kwh() - actual_energy_kwh)
                / max(amr.battery_capacity_kwh, 1e-9)
            )
            if requires_recharge_before_route(amr, actual_energy_kwh):
                return None
            if reserve:
                amr.consume_energy(actual_energy_kwh)
                battery_soc_after = amr.battery_soc_percent
            else:
                battery_soc_after = projected_battery_soc_after
            if reserve:
                self._record_committed_segment_impacts(segments)
            return {
                "multi_stop": True,
                "tasks": tasks,
                "task_start_time": task_start_time,
                "finish_time": t,
                "duration": total,
                "segments": segments,
                "end_location": current_location.name,
                "energy_kwh": actual_energy_kwh,
                "battery_soc_after": battery_soc_after,
                "corridor_energy_kwh": corridor_energy_kwh,
                "lift_energy_kwh": lift_energy_kwh_total,
                "lift_empty_sec_total": lift_empty_sec_total,
                "lift_loaded_sec_total": lift_loaded_sec_total,
                "slot_assignments": slot_assignments,
            }
        except Exception as exc:
            print(f"_estimate_multi_stop_for_amr failed for {amr.id}: {exc}")
            return None

    def _prepare_task_payload_instance(self, task: Task) -> None:
        payload_name = normalise_payload_name(getattr(task, "payload", ""))
        if not payload_name:
            task.payload = EMPTY_PAYLOAD_NAME
            task.payload_instance_id = ""
            return
        if payload_name not in self.payloads:
            return
        if self._task_requires_existing_payload_instance(task):
            # Existing-container tasks must collect a physical payload already in
            # the store. Do not create a new synthetic instance at scheduling time.
            return
        self.payload_instance_store.ensure_task_instance_id(task)

    def _task_requires_existing_payload_instance(self, task: Task) -> bool:
        # Outbound tasks can create a new physical object at their source.
        # Most return tasks collect an existing object, but waste-bin exchange
        # returns are different: the full bin remains at the waste store and a
        # newly issued empty bin is returned to the department/shared bin space.
        if bool(getattr(task, "creates_new_payload_instance", False)):
            return False
        return bool(getattr(task, "is_return_task", False)) or bool(
            getattr(task, "requires_existing_payload_instance", False)
        )

    def _pickup_instance_available(self, task: Task) -> bool:
        payload_name = normalise_payload_name(getattr(task, "payload", ""))
        instance_id = str(getattr(task, "payload_instance_id", "") or "").strip()
        if not payload_name:
            return True
        if not self._task_requires_existing_payload_instance(task):
            return True
        if instance_id:
            if self.payload_instance_store.is_reserved(
                instance_id, excluding_owner=str(getattr(task, "id", "") or "")
            ):
                return False
            if not self.payload_instance_store.has_instance_at(
                task.pickup, instance_id, payload_name
            ):
                return False
            if (
                bool(getattr(task, "is_return_task", False))
                and str(getattr(task, "task_source", "") or "")
                == "task_generation_return"
                and not bool(getattr(task, "returns_same_payload_instance", False))
            ):
                record = getattr(self.payload_instance_store, "_records", {}).get(
                    instance_id
                )
                return record is None or self._record_is_available_empty_container(
                    record
                )
            return True
        records = [
            record
            for record in self.payload_instance_store.records_at(task.pickup)
            if record.payload == payload_name
            and not self.payload_instance_store.is_reserved(
                record.instance_id,
                excluding_owner=str(getattr(task, "id", "") or ""),
            )
        ]
        if (
            bool(getattr(task, "is_return_task", False))
            and str(getattr(task, "task_source", "") or "") == "task_generation_return"
            and not bool(getattr(task, "returns_same_payload_instance", False))
        ):
            records = [
                record
                for record in records
                if self._record_is_available_empty_container(record)
            ]
        return bool(records)

    def _pickup_instance_pending_reason(self, task: Task) -> str:
        instance_id = str(getattr(task, "payload_instance_id", "") or "").strip()
        payload_name = normalise_payload_name(getattr(task, "payload", ""))
        if instance_id:
            return (
                f"Payload instance {instance_id} ({payload_name}) is not available "
                f"at pickup location {task.pickup}"
            )
        return f"Existing payload {payload_name} is not available at pickup location {task.pickup}"

    def _pickup_payload_instance_for_task(self, task: Task) -> None:
        payload_name = normalise_payload_name(getattr(task, "payload", ""))
        if not payload_name:
            return

        instance_id = str(getattr(task, "payload_instance_id", "") or "").strip()
        record = None

        if instance_id:
            record = self.payload_instance_store.pickup(
                task.pickup,
                payload_name=payload_name,
                instance_id=instance_id,
                reservation_owner=str(getattr(task, "id", "") or ""),
            )
            if record is None and self._task_requires_existing_payload_instance(task):
                raise RuntimeError(self._pickup_instance_pending_reason(task))

        if (
            record is None
            and not instance_id
            and self._task_requires_existing_payload_instance(task)
        ):
            if (
                bool(getattr(task, "is_return_task", False))
                and str(getattr(task, "task_source", "") or "")
                == "task_generation_return"
                and not bool(getattr(task, "returns_same_payload_instance", False))
            ):
                candidate = next(
                    (
                        item
                        for item in self.payload_instance_store.records_at(task.pickup)
                        if item.payload == payload_name
                        and self._record_is_available_empty_container(item)
                    ),
                    None,
                )
                if candidate is not None:
                    record = self.payload_instance_store.pickup(
                        task.pickup,
                        payload_name=payload_name,
                        instance_id=candidate.instance_id,
                        reservation_owner=str(getattr(task, "id", "") or ""),
                    )
            else:
                record = self.payload_instance_store.pickup(
                    task.pickup,
                    payload_name=payload_name,
                    reservation_owner=str(getattr(task, "id", "") or ""),
                )
            if record is None:
                raise RuntimeError(self._pickup_instance_pending_reason(task))
            instance_id = record.instance_id

        if record is None and not instance_id:
            # Normal outbound tasks can create a new physical object at the source.
            instance_id = self.payload_instance_store.ensure_task_instance_id(task)

        if instance_id:
            task.payload_instance_id = instance_id
            task.payload_instance_picked_up = True
            self.payload_instance_store.release_reservation(
                instance_id, str(getattr(task, "id", "") or "")
            )

        # A pickup physically removes stock from the pickup location.  Keep the
        # current occupancy state in sync for subsequent peak/recommendation
        # calculations and for any immediately-following stowage checks.
        self._log_payload_location_event(
            "location_payload_exit",
            getattr(task, "pickup", ""),
            payload_name,
            instance_id,
            task=task,
        )
        self._record_location_storage_peak(getattr(task, "pickup", ""))
        self._record_payload_population_snapshot()

    def _payload_instance_container_state_for_task(self, task: Task) -> str:
        """Return the physical state to store for a payload instance.

        For waste-bin exchange tasks, the collected bin that arrives at the
        waste store remains there as a full bin.  The return journey is a new
        empty bin issued by the waste store, so later shared-bin collection tasks
        must only bind to empty/available records rather than reusing a full bin
        already left at the store.
        """
        is_waste_task = bool(str(getattr(task, "waste_stream", "") or "").strip()) or (
            str(getattr(task, "task_source", "") or "").strip()
            in {"department_waste", "task_generation_return"}
        )
        if not is_waste_task:
            return ""
        if bool(getattr(task, "is_return_task", False)):
            return "empty"
        return "full"

    def _record_is_available_empty_container(self, record) -> bool:
        metadata = getattr(record, "metadata", {}) or {}
        state = str(metadata.get("container_state", "") or "").strip().lower()
        # Older seeded records did not carry a state; treat them as empty/available.
        return state in {"", "empty", "available"}

    def _store_payload_instance_for_task(self, task: Task) -> None:
        payload_name = normalise_payload_name(getattr(task, "payload", ""))
        instance_id = str(getattr(task, "payload_instance_id", "") or "").strip()
        if not payload_name:
            return
        if not instance_id:
            instance_id = self.payload_instance_store.ensure_task_instance_id(task)
        # Preserve physical-container identity metadata every time the payload is
        # stored.  Shared waste bins rely on container_group to let later tasks
        # re-bind to the actual returned bin location.  Without this, the initial
        # seed carries the group, but the record loses it after the first
        # outbound/return cycle and later shared tasks remain pending.
        metadata = {
            "task_source": getattr(task, "task_source", ""),
            "department_id": getattr(task, "department_id", ""),
            "waste_stream": getattr(task, "waste_stream", ""),
            "container_group": getattr(task, "container_group", ""),
            "shared_container_group": getattr(task, "shared_container_group", ""),
            "container_type": getattr(task, "container_type", ""),
            "container_state": self._payload_instance_container_state_for_task(task),
        }

        existing_record = getattr(self.payload_instance_store, "_records", {}).get(
            instance_id
        )
        if existing_record is not None:
            previous_metadata = getattr(existing_record, "metadata", {}) or {}
            for key in (
                "container_group",
                "shared_container_group",
                "waste_stream",
                "container_state",
            ):
                if not str(metadata.get(key, "") or "").strip():
                    metadata[key] = previous_metadata.get(key, "")

        self.payload_instance_store.store(
            task.dropoff,
            payload_name,
            instance_id,
            source_task_id=task.id,
            metadata=metadata,
        )
        self._log_payload_location_event(
            "location_payload_enter",
            task.dropoff,
            payload_name,
            instance_id,
            task=task,
            inventory_space=str(getattr(task, "assigned_inventory_space", "") or ""),
        )
        self._record_payload_population_snapshot()

    def _record_payload_population_snapshot(self) -> None:
        """Track peak simultaneous physical payload population by payload type.

        This is an asset-population metric. It is not the number of tasks and
        not the number of movements. It counts the current live payload instance
        records in the runtime store and stores the highest count observed for
        each payload type.
        """
        counts = self.payload_instance_store.counts_by_payload()

        for payload_name, count in counts.items():
            payload_name = normalise_payload_name(payload_name)
            if not payload_name or is_empty_payload_name(payload_name):
                continue
            self.payload_population_peak[payload_name] = max(
                int(self.payload_population_peak.get(payload_name, 0) or 0),
                int(count),
            )

        # The store maintains payload-type membership incrementally, so this
        # remains O(number of payload types) rather than O(all instances ever
        # created) on every pickup and drop-off.
        for payload_name in self.payload_instance_store.known_payload_names():
            payload_name = normalise_payload_name(payload_name)
            if payload_name and not is_empty_payload_name(payload_name):
                self.payload_population_peak.setdefault(payload_name, 0)

    def _log_payload_location_event(
        self,
        event_type: str,
        location_name: str,
        payload_name: str,
        instance_id: str,
        task: Optional[Task] = None,
        event_time: Optional[float] = None,
        inventory_space: str = "",
    ) -> None:
        if not self.verbose:
            return
        payload_name = normalise_payload_name(payload_name)
        instance_id = str(instance_id or "").strip()
        location_name = str(location_name or "").strip()
        if not payload_name or not instance_id or not location_name:
            return
        event_time = self.current_time if event_time is None else float(event_time)
        explicit_inventory_space = str(inventory_space or "").strip()
        inventory_space = explicit_inventory_space
        if not inventory_space:
            for space in self.inventory_spaces_by_location.get(location_name, []) or []:
                if (
                    str(space.get("payload_instance_id", "") or "").strip()
                    == instance_id
                ):
                    inventory_space = str(space.get("name", "") or "").strip()
                    break
        if (
            not inventory_space
            and task is not None
            and str(getattr(task, "dropoff", "") or "").strip() == location_name
        ):
            inventory_space = str(
                getattr(task, "assigned_inventory_space", "") or ""
            ).strip()
        loc = self.locations.get(location_name)
        self.log_step(
            event_time=event_time,
            event_type=event_type,
            task_id=str(getattr(task, "id", "") or ""),
            details=f"Payload {instance_id} ({payload_name}) {event_type.replace('location_payload_', '')} {location_name}",
            from_location=location_name,
            to_location=location_name,
            payload_name=payload_name,
            payload_instance_id=instance_id,
            start_time=event_time,
            end_time=event_time,
            start_node=location_name,
            end_node=location_name,
            start_x=getattr(loc, "x", None),
            start_y=getattr(loc, "y", None),
            start_floor=getattr(loc, "floor", None),
            end_x=getattr(loc, "x", None),
            end_y=getattr(loc, "y", None),
            end_floor=getattr(loc, "floor", None),
            status="payload_location",
            task_source=str(getattr(task, "task_source", "") or ""),
            department_id=str(getattr(task, "department_id", "") or ""),
            waste_stream=str(getattr(task, "waste_stream", "") or ""),
            waste_volume_m3=float(getattr(task, "waste_volume_m3", 0.0) or 0.0),
            container_type=str(
                getattr(task, "container_type", payload_name) or payload_name
            ),
            inventory_space=inventory_space,
        )

    def _record_location_storage_peak(self, location_name: str) -> None:
        """Store the highest simultaneous physical payload occupancy per location.

        This reads from PayloadInstanceStore, so the peak is based on the current
        physical payloads at the location rather than the number of generated or
        completed tasks. It should be called immediately after payloads are
        stored, picked up, removed, or replaced.
        """
        location_name = str(location_name or "").strip()
        if not location_name:
            return

        payload_count = 0
        area_m2 = 0.0
        volume_m3 = 0.0

        # Aggregate counts are updated by PayloadInstanceStore on store/pickup.
        # This avoids rebuilding and walking every record at a busy location.
        for payload_name, count in self.payload_instance_store.counts_at(
            location_name
        ).items():
            payload_name = normalise_payload_name(payload_name)
            payload = self.payloads.get(payload_name)
            if payload is None or is_empty_payload_name(getattr(payload, "name", "")):
                continue
            count = max(0, int(count or 0))
            payload_count += count
            footprint = max(0.0, float(getattr(payload, "length_m", 0.0) or 0.0)) * max(
                0.0, float(getattr(payload, "width_m", 0.0) or 0.0)
            )
            area_m2 += footprint * count
            volume_m3 += (
                footprint
                * max(0.0, float(getattr(payload, "height_m", 0.0) or 0.0))
                * count
            )

        item = self.location_storage_peak.setdefault(
            location_name,
            {
                "peak_payload_count": 0,
                "peak_area_m2": 0.0,
                "peak_volume_m3": 0.0,
                "current_payload_count": 0,
                "current_area_m2": 0.0,
                "current_volume_m3": 0.0,
            },
        )
        item["current_payload_count"] = payload_count
        item["current_area_m2"] = area_m2
        item["current_volume_m3"] = volume_m3
        if payload_count > int(item.get("peak_payload_count", 0) or 0):
            item["peak_payload_count"] = payload_count
        if area_m2 > float(item.get("peak_area_m2", 0.0) or 0.0):
            item["peak_area_m2"] = area_m2
        if volume_m3 > float(item.get("peak_volume_m3", 0.0) or 0.0):
            item["peak_volume_m3"] = volume_m3

    def _configured_inventory_area_for_location(self, location_name: str) -> float:
        total = 0.0
        for space in (
            self.inventory_spaces_by_location.get(str(location_name or "").strip(), [])
            or []
        ):
            points = space.get("points", []) or []
            if len(points) >= 3:
                try:
                    coords = [
                        (
                            float(p.get("dx", p.get("x", 0.0)) or 0.0),
                            float(p.get("dy", p.get("y", 0.0)) or 0.0),
                        )
                        for p in points
                        if isinstance(p, dict)
                    ]
                    if len(coords) >= 3:
                        shoelace = 0.0
                        for i, (x1, y1) in enumerate(coords):
                            x2, y2 = coords[(i + 1) % len(coords)]
                            shoelace += (x1 * y2) - (x2 * y1)
                        total += abs(shoelace) / 2.0
                        continue
                except Exception:
                    pass
            try:
                total += max(0.0, float(space.get("length_m", 0.0) or 0.0)) * max(
                    0.0, float(space.get("width_m", 0.0) or 0.0)
                )
            except Exception:
                pass
        return total

    def _append_payload_population_summary_rows(self) -> None:
        """Write one runtime payload-population row per payload type.

        total_runtime_payloads is the peak simultaneous number of physical
        instances of that payload that existed in the runtime store. It is the
        number to use for asset/population sizing, not task count.
        """
        if self._payload_population_rows_written or not self.verbose:
            return
        self._payload_population_rows_written = True
        self._record_payload_population_snapshot()

        known_by_payload: Dict[str, set] = defaultdict(set)
        for record in getattr(
            self.payload_instance_store, "_known_instances", {}
        ).values():
            payload_name = normalise_payload_name(getattr(record, "payload", ""))
            instance_id = str(getattr(record, "instance_id", "") or "").strip()
            if payload_name and instance_id and not is_empty_payload_name(payload_name):
                known_by_payload[payload_name].add(instance_id)

        event_time = float(getattr(self, "current_time", 0.0) or 0.0)
        for payload_name in sorted(
            set(known_by_payload) | set(self.payload_population_peak)
        ):
            peak_count = int(self.payload_population_peak.get(payload_name, 0) or 0)
            known_count = len(known_by_payload.get(payload_name, set()))
            payload = self.payloads.get(payload_name)
            weight = (
                float(getattr(payload, "weight_kg", 0.0) or 0.0) if payload else 0.0
            )
            self._append_verbose_row(
                {
                    "sim_time_sec": round(event_time, 3),
                    "sim_datetime": self.clock.format_sim_time(event_time),
                    "event_type": "payload_population_summary",
                    "task_id": "",
                    "amr_id": "",
                    "payload": payload_name,
                    "payload_instance_id": "",
                    "from_location": "",
                    "to_location": "",
                    "status": "summary",
                    "details": (
                        f"Runtime payload population for {payload_name}: peak={peak_count}; "
                        f"known_instances={known_count}"
                    ),
                    "payload_runtime_population": peak_count,
                    "payload_known_instances": known_count,
                    "payload_weight_kg": weight,
                }
            )

    def _append_location_space_recommendation_rows(self) -> None:
        """Write one final peak-occupancy row per location into the verbose CSV.

        These rows are intended for the report. They are not printed to console.
        """
        if self._location_recommendation_rows_written or not self.verbose:
            return
        self._location_recommendation_rows_written = True

        for location_name in sorted(
            set(self.locations.keys()) | set(self.location_storage_peak.keys())
        ):
            self._record_location_storage_peak(location_name)
            item = self.location_storage_peak.get(location_name, {}) or {}
            peak_count = int(item.get("peak_payload_count", 0) or 0)
            peak_area = float(item.get("peak_area_m2", 0.0) or 0.0)
            peak_volume = float(item.get("peak_volume_m3", 0.0) or 0.0)
            configured_area = self._configured_inventory_area_for_location(
                location_name
            )
            if peak_count <= 0 and peak_area <= 0.0 and configured_area <= 0.0:
                continue

            event_time = float(getattr(self, "current_time", 0.0) or 0.0)
            self._append_verbose_row(
                {
                    "sim_time_sec": round(event_time, 3),
                    "sim_datetime": self.clock.format_sim_time(event_time),
                    "event_type": "location_space_recommendation",
                    "task_id": "",
                    "amr_id": "",
                    "payload": "",
                    "payload_instance_id": "",
                    "from_location": location_name,
                    "to_location": location_name,
                    "status": "summary",
                    "details": (
                        f"Peak stored payloads={peak_count}; peak area={peak_area:.3f} m2; "
                        f"peak volume={peak_volume:.3f} m3"
                    ),
                    "location_inventory_spaces_disabled": bool(
                        getattr(self, "disable_inventory_spaces", False)
                    ),
                    "location_configured_inventory_area_m2": configured_area,
                    "location_peak_payload_count": peak_count,
                    "location_peak_footprint_area_m2": peak_area,
                    "location_peak_volume_m3": peak_volume,
                    "location_payload_footprint_area_m2": float(
                        item.get("current_area_m2", 0.0) or 0.0
                    ),
                    "location_payload_volume_m3": float(
                        item.get("current_volume_m3", 0.0) or 0.0
                    ),
                    "location_recommended_area_m2": peak_area * 1.30,
                    "location_recommended_volume_m3": peak_volume * 1.30,
                }
            )

    def _init_inventory_spaces(self, location_dicts: List[dict]) -> None:
        self.inventory_spaces_by_location = {}

        for loc in location_dicts:
            location_name = str(loc.get("name", "")).strip()
            if not location_name:
                continue

            raw_spaces = loc.get("inventory_spaces", []) or []
            clean_spaces = []

            for index, raw_space in enumerate(raw_spaces, start=1):
                if not isinstance(raw_space, dict):
                    continue

                points = list(raw_space.get("points", []) or [])
                point_length, point_width = self._space_points_dimensions(points)

                length_m = float(
                    raw_space.get(
                        "length_m",
                        raw_space.get("length", point_length),
                    )
                    or point_length
                    or 0.0
                )
                width_m = float(
                    raw_space.get(
                        "width_m",
                        raw_space.get("width", point_width),
                    )
                    or point_width
                    or 0.0
                )
                height_m = float(
                    raw_space.get(
                        "height_m",
                        raw_space.get("height", 999999.0),
                    )
                    or 999999.0
                )

                name = str(raw_space.get("name", "")).strip() or f"Space {index}"
                occupied = bool(raw_space.get("occupied", False))

                slot_type = str(raw_space.get("space_type", "") or "").strip().lower()
                stores_amr = (
                    bool(raw_space.get("stores_amr", False)) or slot_type == "amr"
                )
                amr_type = str(raw_space.get("amr_type", "") or "").strip()
                for slot in raw_space.get("payload_slots", []) or []:
                    if not isinstance(slot, dict):
                        continue
                    if (
                        str(slot.get("slot_type", "") or "").strip().lower() == "amr"
                        or str(slot.get("amr_type", "") or "").strip()
                    ):
                        stores_amr = True
                        amr_type = (
                            amr_type or str(slot.get("amr_type", "") or "").strip()
                        )

                clean_spaces.append(
                    {
                        "name": name,
                        "points": points,
                        "payload_slots": list(raw_space.get("payload_slots", []) or []),
                        "length_m": length_m,
                        "width_m": width_m,
                        "height_m": height_m,
                        "occupied": occupied,
                        "payload": str(raw_space.get("payload", "")).strip(),
                        "payload_instance_id": str(
                            raw_space.get("payload_instance_id", "")
                        ).strip(),
                        "reserved_by_task": str(
                            raw_space.get("reserved_by_task", "")
                        ).strip(),
                        "task_id": str(raw_space.get("task_id", "")).strip(),
                        "space_type": (
                            "amr"
                            if stores_amr
                            else str(raw_space.get("space_type", "") or "").strip()
                        ),
                        "stores_amr": stores_amr,
                        "amr_type": amr_type,
                        "amr_id": str(raw_space.get("amr_id", "") or "").strip(),
                        "reserved_by_amr": str(
                            raw_space.get("reserved_by_amr", "") or ""
                        ).strip(),
                        "has_charger": bool(raw_space.get("has_charger", False)),
                    }
                )

            if clean_spaces:
                self.inventory_spaces_by_location[location_name] = clean_spaces

    def _inventory_space_centre_location(
        self, parent_location_name: str, space: dict
    ) -> Optional[Location]:
        parent = self.locations.get(str(parent_location_name or "").strip())
        if parent is None:
            return None
        points = list(space.get("points", []) or [])
        xs = []
        ys = []
        for point in points:
            try:
                if "dx" in point and "dy" in point:
                    xs.append(float(parent.x) + float(point.get("dx", 0.0)))
                    ys.append(float(parent.y) + float(point.get("dy", 0.0)))
                else:
                    xs.append(float(point.get("x", parent.x)))
                    ys.append(float(point.get("y", parent.y)))
            except Exception:
                continue
        if xs and ys:
            x = (min(xs) + max(xs)) / 2.0
            y = (min(ys) + max(ys)) / 2.0
        else:
            x = float(parent.x)
            y = float(parent.y)
        return Location(
            name=str(parent_location_name or ""),
            floor=int(parent.floor),
            x=round(float(x), 3),
            y=round(float(y), 3),
        )

    def _inventory_space_rotation_deg(self, space: dict) -> float:
        """Return the AMR bay/slot rotation in degrees for parked AMR display."""
        if not isinstance(space, dict):
            return 0.0
        for slot in space.get("payload_slots", []) or []:
            if not isinstance(slot, dict):
                continue
            if (
                str(slot.get("slot_type", "") or "").strip().lower() == "amr"
                or str(slot.get("amr_type", "") or "").strip()
            ):
                try:
                    return float(slot.get("rotation_deg", 0.0) or 0.0)
                except Exception:
                    return 0.0
        try:
            return float(space.get("rotation_deg", 0.0) or 0.0)
        except Exception:
            return 0.0

    def _inventory_space_world_polygon(
        self, parent_location_name: str, space: dict
    ) -> List[Tuple[float, float]]:
        """Return inventory/AMR space polygon in world coordinates."""
        parent = self.locations.get(str(parent_location_name or "").strip())
        if parent is None:
            return []
        points = space.get("points", []) or []
        polygon: List[Tuple[float, float]] = []
        for point in points:
            try:
                polygon.append(
                    (
                        float(parent.x) + float(point.get("dx", 0.0) or 0.0),
                        float(parent.y) + float(point.get("dy", 0.0) or 0.0),
                    )
                )
            except Exception:
                continue
        if len(polygon) >= 3:
            return polygon
        slot = next(iter(space.get("payload_slots", []) or []), {})
        try:
            cx = float(parent.x) + float(slot.get("dx", 0.0) or 0.0)
            cy = float(parent.y) + float(slot.get("dy", 0.0) or 0.0)
        except Exception:
            return []
        half = 0.3
        return [
            (cx - half, cy - half),
            (cx + half, cy - half),
            (cx + half, cy + half),
            (cx - half, cy + half),
        ]

    def _expanded_bbox_for_polygon(
        self, polygon: List[Tuple[float, float]], clearance: float
    ) -> Optional[Tuple[float, float, float, float]]:
        if not polygon:
            return None
        xs = [p[0] for p in polygon]
        ys = [p[1] for p in polygon]
        return (
            min(xs) - clearance,
            min(ys) - clearance,
            max(xs) + clearance,
            max(ys) + clearance,
        )

    def _point_inside_bbox(
        self, point: Tuple[float, float], bbox: Tuple[float, float, float, float]
    ) -> bool:
        x, y = point
        min_x, min_y, max_x, max_y = bbox
        return min_x <= x <= max_x and min_y <= y <= max_y

    def _segments_intersect_2d(
        self,
        a: Tuple[float, float],
        b: Tuple[float, float],
        c: Tuple[float, float],
        d: Tuple[float, float],
    ) -> bool:
        def orient(p, q, r):
            return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

        def on_segment(p, q, r):
            return (
                min(p[0], r[0]) - 1e-9 <= q[0] <= max(p[0], r[0]) + 1e-9
                and min(p[1], r[1]) - 1e-9 <= q[1] <= max(p[1], r[1]) + 1e-9
            )

        o1 = orient(a, b, c)
        o2 = orient(a, b, d)
        o3 = orient(c, d, a)
        o4 = orient(c, d, b)
        if (o1 > 0) != (o2 > 0) and (o3 > 0) != (o4 > 0):
            return True
        if abs(o1) <= 1e-9 and on_segment(a, c, b):
            return True
        if abs(o2) <= 1e-9 and on_segment(a, d, b):
            return True
        if abs(o3) <= 1e-9 and on_segment(c, a, d):
            return True
        if abs(o4) <= 1e-9 and on_segment(c, b, d):
            return True
        return False

    def _segment_intersects_bbox(
        self,
        a: Tuple[float, float],
        b: Tuple[float, float],
        bbox: Tuple[float, float, float, float],
    ) -> bool:
        if self._point_inside_bbox(a, bbox) or self._point_inside_bbox(b, bbox):
            return True
        min_x, min_y, max_x, max_y = bbox
        corners = [(min_x, min_y), (max_x, min_y), (max_x, max_y), (min_x, max_y)]
        for idx in range(4):
            if self._segments_intersect_2d(a, b, corners[idx], corners[(idx + 1) % 4]):
                return True
        return False

    def _local_obstacle_bboxes(
        self, location_name: str, exclude_space_name: str = ""
    ) -> List[Tuple[float, float, float, float]]:
        """Return expanded bboxes for payload/AMR spaces to avoid locally."""
        location_name = str(location_name or "").strip()
        exclude_space_name = str(exclude_space_name or "").strip()
        cache_key = (location_name, exclude_space_name)
        cached = self.local_obstacle_bbox_cache.get(cache_key)
        if cached is not None:
            return list(cached)

        bboxes: List[Tuple[float, float, float, float]] = []
        for space in self.inventory_spaces_by_location.get(
            location_name, []
        ):
            if (
                exclude_space_name
                and str(space.get("name", "") or "").strip() == exclude_space_name
            ):
                continue
            bbox = self._expanded_bbox_for_polygon(
                self._inventory_space_world_polygon(location_name, space), 0.15
            )
            if bbox is not None:
                bboxes.append(bbox)
        self.local_obstacle_bbox_cache[cache_key] = tuple(bboxes)
        return bboxes

    def _local_manoeuvre_waypoint_cache_get(self, cache_key: Tuple):
        cached = self.local_manoeuvre_waypoint_cache.get(cache_key)
        if cached is None:
            return None
        return [tuple(point) for point in cached]

    def _local_manoeuvre_waypoint_cache_set(
        self, cache_key: Tuple, waypoints: List[Tuple[float, float]]
    ) -> None:
        max_entries = int(
            getattr(self, "local_manoeuvre_waypoint_cache_max_entries", 0) or 0
        )
        if max_entries <= 0:
            return
        if len(self.local_manoeuvre_waypoint_cache) >= max_entries:
            self.local_manoeuvre_waypoint_cache.clear()
        self.local_manoeuvre_waypoint_cache[cache_key] = tuple(
            (float(x), float(y)) for x, y in waypoints
        )

    def _clear_local_segment(
        self,
        a: Tuple[float, float],
        b: Tuple[float, float],
        obstacles: List[Tuple[float, float, float, float]],
    ) -> bool:
        return not any(
            self._segment_intersects_bbox(a, b, obstacle) for obstacle in obstacles
        )

    def _local_manoeuvre_waypoints(
        self,
        start: Tuple[float, float],
        end: Tuple[float, float],
        obstacles: List[Tuple[float, float, float, float]],
    ) -> List[Tuple[float, float]]:
        """Find a short off-graph route around local inventory spaces.

        Candidate points are limited to obstacles near the requested movement
        corridor. The former all-obstacle visibility graph grew approximately
        cubically with the number of spaces and became a dominant run-time cost.
        Every candidate edge is still checked against the complete obstacle set.
        """
        if self._clear_local_segment(start, end, obstacles):
            return [start, end]

        clearance = 0.35
        corridor_padding = 1.25
        path_min_x = min(start[0], end[0]) - corridor_padding
        path_max_x = max(start[0], end[0]) + corridor_padding
        path_min_y = min(start[1], end[1]) - corridor_padding
        path_max_y = max(start[1], end[1]) + corridor_padding

        def overlaps_path_corridor(bbox) -> bool:
            min_x, min_y, max_x, max_y = bbox
            return not (
                max_x < path_min_x
                or min_x > path_max_x
                or max_y < path_min_y
                or min_y > path_max_y
            )

        nearby = [bbox for bbox in obstacles if overlaps_path_corridor(bbox)]
        if not nearby:
            nearby = list(obstacles)

        # Bound candidate growth on very dense storage layouts. Obstacles closest
        # to the direct movement segment are the ones most useful for a detour.
        if len(nearby) > 24:
            sx, sy = start
            ex, ey = end
            dx = ex - sx
            dy = ey - sy
            length_sq = (dx * dx) + (dy * dy)

            def obstacle_score(bbox) -> float:
                min_x, min_y, max_x, max_y = bbox
                cx = (min_x + max_x) / 2.0
                cy = (min_y + max_y) / 2.0
                if length_sq <= 1e-12:
                    return math.hypot(cx - sx, cy - sy)
                frac = max(0.0, min(1.0, ((cx - sx) * dx + (cy - sy) * dy) / length_sq))
                px = sx + (frac * dx)
                py = sy + (frac * dy)
                return math.hypot(cx - px, cy - py)

            nearby = sorted(nearby, key=obstacle_score)[:24]

        candidates: List[Tuple[float, float]] = [start, end]
        for min_x, min_y, max_x, max_y in nearby:
            candidates.extend(
                [
                    (min_x - clearance, min_y - clearance),
                    (min_x - clearance, max_y + clearance),
                    (max_x + clearance, min_y - clearance),
                    (max_x + clearance, max_y + clearance),
                ]
            )

        filtered: List[Tuple[float, float]] = []
        seen = set()
        for point in candidates:
            key = (round(point[0], 4), round(point[1], 4))
            if key in seen:
                continue
            seen.add(key)
            if any(self._point_inside_bbox(point, obstacle) for obstacle in obstacles):
                continue
            filtered.append(point)
        candidates = filtered
        if len(candidates) < 2:
            return [start, end]

        graph: Dict[int, List[Tuple[int, float]]] = {
            i: [] for i in range(len(candidates))
        }
        for i in range(len(candidates)):
            for j in range(i + 1, len(candidates)):
                if self._clear_local_segment(candidates[i], candidates[j], nearby):
                    dist = math.hypot(
                        candidates[i][0] - candidates[j][0],
                        candidates[i][1] - candidates[j][1],
                    )
                    graph[i].append((j, dist))
                    graph[j].append((i, dist))

        queue: List[Tuple[float, int]] = [(0.0, 0)]
        distances = {0: 0.0}
        previous: Dict[int, int] = {}
        while queue:
            dist, node = heapq.heappop(queue)
            if node == 1:
                break
            if dist > distances.get(node, math.inf):
                continue
            for nxt, edge_dist in graph.get(node, []):
                nd = dist + edge_dist
                if nd < distances.get(nxt, math.inf):
                    distances[nxt] = nd
                    previous[nxt] = node
                    heapq.heappush(queue, (nd, nxt))

        if 1 not in distances:
            # Cheap orthogonal alternatives are preferable to rebuilding a huge
            # all-space visibility graph. Keep the legacy direct fallback only
            # when neither dogleg is collision free.
            for middle in ((start[0], end[1]), (end[0], start[1])):
                if self._clear_local_segment(
                    start, middle, obstacles
                ) and self._clear_local_segment(middle, end, obstacles):
                    return [start, middle, end]
            return [start, end]

        order = [1]
        while order[-1] != 0:
            order.append(previous[order[-1]])
        order.reverse()
        path = [candidates[i] for i in order]
        if all(
            self._clear_local_segment(path[index], path[index + 1], obstacles)
            for index in range(len(path) - 1)
        ):
            return path
        for middle in ((start[0], end[1]), (end[0], start[1])):
            if self._clear_local_segment(
                start, middle, obstacles
            ) and self._clear_local_segment(middle, end, obstacles):
                return [start, middle, end]
        return [start, end]

    def _normalise_angle_deg(self, value: float) -> float:
        return (float(value) + 180.0) % 360.0 - 180.0

    def _angle_lerp_deg(self, start_deg: float, end_deg: float, frac: float) -> float:
        frac = max(0.0, min(1.0, float(frac)))
        delta = self._normalise_angle_deg(float(end_deg) - float(start_deg))
        return float(start_deg) + (delta * frac)

    def _amr_vehicle_turning_radius_m(self, amr: AMR) -> float:
        """Approximate a reversible AMR steering radius from its footprint."""
        length = float(getattr(amr, "length_m", 1.0) or 1.0)
        width = float(getattr(amr, "width_m", 0.6) or 0.6)
        # Minimum usable turning circle grows with diagonal size.  AMRs can steer
        # from either end, so this is deliberately conservative but not car-like.
        return max(0.35, math.hypot(length, width) * 0.55)

    def _heading_deg_between(
        self, a: Tuple[float, float], b: Tuple[float, float]
    ) -> float:
        return math.degrees(
            math.atan2(float(b[1]) - float(a[1]), float(b[0]) - float(a[0]))
        )

    def _choose_reversible_body_heading_deg(
        self, movement_heading_deg: float, preferred_heading_deg: float
    ) -> float:
        """Choose forwards or backwards body heading closest to the preferred heading."""
        forward = float(movement_heading_deg)
        reverse = float(movement_heading_deg) + 180.0
        df = abs(self._normalise_angle_deg(forward - preferred_heading_deg))
        dr = abs(self._normalise_angle_deg(reverse - preferred_heading_deg))
        return reverse if dr < df else forward

    def _insert_vehicle_alignment_waypoint(
        self,
        waypoints: List[Tuple[float, float]],
        target_heading_deg: float,
        radius: float,
        obstacles: List[Tuple[float, float, float, float]],
    ) -> List[Tuple[float, float]]:
        """Add a straight final approach along the target slot axis when possible."""
        if len(waypoints) < 2:
            return waypoints
        end = waypoints[-1]
        previous = waypoints[-2]
        heading = math.radians(float(target_heading_deg))
        axis = (math.cos(heading), math.sin(heading))
        approach_dist = max(0.45, min(radius * 1.25, 2.5))
        candidates = [
            (end[0] - axis[0] * approach_dist, end[1] - axis[1] * approach_dist),
            (end[0] + axis[0] * approach_dist, end[1] + axis[1] * approach_dist),
        ]
        best = None
        best_score = math.inf
        for candidate in candidates:
            if any(
                self._point_inside_bbox(candidate, obstacle) for obstacle in obstacles
            ):
                continue
            if not self._clear_local_segment(candidate, end, obstacles):
                continue
            if not self._clear_local_segment(previous, candidate, obstacles):
                continue
            # Prefer the approach point that causes the least detour from the
            # existing route while still aligning with the bay/payload slot.
            score = math.hypot(previous[0] - candidate[0], previous[1] - candidate[1])
            if score < best_score:
                best = candidate
                best_score = score
        if best is None:
            return waypoints
        if math.hypot(best[0] - previous[0], best[1] - previous[1]) <= 1e-6:
            return waypoints
        return waypoints[:-1] + [best, end]

    def _smooth_vehicle_waypoints(
        self,
        waypoints: List[Tuple[float, float]],
        start_heading_deg: float,
        target_heading_deg: float,
        radius: float,
    ) -> List[Tuple[float, float]]:
        """Densify corners so the visualiser shows steering rather than teleport turns."""
        if len(waypoints) <= 2:
            return waypoints
        result: List[Tuple[float, float]] = [waypoints[0]]
        corner_cut = max(0.15, min(radius * 0.45, 0.8))
        for idx in range(1, len(waypoints) - 1):
            prev_pt = waypoints[idx - 1]
            cur_pt = waypoints[idx]
            next_pt = waypoints[idx + 1]
            d1 = math.hypot(cur_pt[0] - prev_pt[0], cur_pt[1] - prev_pt[1])
            d2 = math.hypot(next_pt[0] - cur_pt[0], next_pt[1] - cur_pt[1])
            if d1 <= 1e-6 or d2 <= 1e-6:
                continue
            cut = min(corner_cut, d1 * 0.45, d2 * 0.45)
            in_pt = (
                cur_pt[0] - ((cur_pt[0] - prev_pt[0]) / d1) * cut,
                cur_pt[1] - ((cur_pt[1] - prev_pt[1]) / d1) * cut,
            )
            out_pt = (
                cur_pt[0] + ((next_pt[0] - cur_pt[0]) / d2) * cut,
                cur_pt[1] + ((next_pt[1] - cur_pt[1]) / d2) * cut,
            )
            result.append(in_pt)
            # One midpoint gives a visible curved steer without creating a huge log.
            result.append(((in_pt[0] + out_pt[0]) / 2.0, (in_pt[1] + out_pt[1]) / 2.0))
            result.append(out_pt)
        result.append(waypoints[-1])
        filtered: List[Tuple[float, float]] = []
        for pt in result:
            if (
                not filtered
                or math.hypot(pt[0] - filtered[-1][0], pt[1] - filtered[-1][1]) > 1e-4
            ):
                filtered.append(pt)
        return filtered

    def _local_manoeuvre_segments_to_inventory_space(
        self,
        amr: AMR,
        location_name: str,
        target_space: dict,
        start_time_value: float,
        purpose: str = "inventory",
    ) -> Tuple[List[dict], float, float]:
        """Build visual/logged off-graph reversible vehicle manoeuvre segments to a target space."""
        parent = self.locations.get(str(location_name or "").strip())
        target = self._inventory_space_centre_location(location_name, target_space)
        if parent is None or target is None:
            return [], 0.0, 0.0

        start = (float(parent.x), float(parent.y))
        end = (float(target.x), float(target.y))
        target_heading_deg = self._inventory_space_rotation_deg(target_space)
        radius = self._amr_vehicle_turning_radius_m(amr)
        space_name = str(target_space.get("name", "") or "")
        initial_heading_deg = float(
            getattr(amr, "rotation_deg", target_heading_deg) or target_heading_deg
        )
        cache_key = (
            str(location_name or "").strip(),
            space_name.strip(),
            start[0],
            start[1],
            end[0],
            end[1],
            float(initial_heading_deg),
            float(target_heading_deg),
            float(radius),
        )
        waypoints = self._local_manoeuvre_waypoint_cache_get(cache_key)
        if waypoints is None:
            obstacles = self._local_obstacle_bboxes(location_name, space_name)
            waypoints = self._local_manoeuvre_waypoints(start, end, obstacles)
            waypoints = self._insert_vehicle_alignment_waypoint(
                waypoints, target_heading_deg, radius, obstacles
            )
            waypoints = self._smooth_vehicle_waypoints(
                waypoints, initial_heading_deg, target_heading_deg, radius
            )
            self._local_manoeuvre_waypoint_cache_set(cache_key, waypoints)

        segments: List[dict] = []
        total_duration = 0.0
        total_distance = 0.0
        previous_body_heading = initial_heading_deg
        speed = max(float(getattr(amr, "speed_m_per_sec", 1.0) or 1.0), 1e-9)
        for idx in range(len(waypoints) - 1):
            a = waypoints[idx]
            b = waypoints[idx + 1]
            dist = math.hypot(a[0] - b[0], a[1] - b[1])
            if dist <= 1e-9:
                continue

            movement_heading = self._heading_deg_between(a, b)
            is_final_segment = idx == len(waypoints) - 2
            desired_end_heading = (
                target_heading_deg
                if is_final_segment
                else self._choose_reversible_body_heading_deg(
                    movement_heading, target_heading_deg
                )
            )
            # Limit abrupt visual heading changes based on footprint-derived radius.
            # Travel time includes a small steering/alignment allowance so tight
            # local manoeuvres are visibly slower than corridor travel.
            turn_delta = abs(
                self._normalise_angle_deg(desired_end_heading - previous_body_heading)
            )
            turn_allowance = (turn_delta / 90.0) * (radius / max(speed, 1e-9))
            duration = (dist / speed) + max(0.0, turn_allowance)

            segments.append(
                {
                    "type": "local_manoeuvre",
                    "purpose": purpose,
                    "from": f"{location_name}::local::{idx}",
                    "to": f"{location_name}::local::{idx + 1}",
                    "from_x": round(a[0], 4),
                    "from_y": round(a[1], 4),
                    "from_floor": parent.floor,
                    "to_x": round(b[0], 4),
                    "to_y": round(b[1], 4),
                    "to_floor": parent.floor,
                    "duration": duration,
                    "distance_m": dist,
                    "inventory_space": str(target_space.get("name", "") or ""),
                    "amr_rotation_start_deg": round(float(previous_body_heading), 3),
                    "amr_rotation_end_deg": round(float(desired_end_heading), 3),
                    "amr_rotation_deg": round(float(desired_end_heading), 3),
                    "amr_turning_radius_m": round(float(radius), 3),
                    "local_path_index": idx,
                }
            )
            previous_body_heading = desired_end_heading
            total_duration += duration
            total_distance += dist

        return segments, total_duration, total_distance

    def _local_manoeuvre_segments_from_inventory_space(
        self,
        amr: AMR,
        location_name: str,
        space_name: str,
        start_time_value: float,
        purpose: str = "amr_unstow",
    ) -> Tuple[List[dict], float, float]:
        """Build visual/logged local manoeuvre segments from an AMR bay to its parent location node."""
        location_name = str(location_name or "").strip()
        space_name = str(space_name or "").strip()
        if not location_name or not space_name:
            return [], 0.0, 0.0

        target_space = None
        for space in self.inventory_spaces_by_location.get(location_name, []):
            if str(space.get("name", "") or "").strip() == space_name:
                target_space = space
                break
        if target_space is None:
            return [], 0.0, 0.0

        to_space_segments, total_duration, total_distance = (
            self._local_manoeuvre_segments_to_inventory_space(
                amr,
                location_name,
                target_space,
                start_time_value,
                purpose=purpose,
            )
        )
        if not to_space_segments:
            return [], 0.0, 0.0

        reversed_segments: List[dict] = []
        for index, segment in enumerate(reversed(to_space_segments)):
            item = dict(segment)
            item["purpose"] = purpose
            item["from"], item["to"] = segment.get("to", ""), segment.get("from", "")
            item["from_x"], item["to_x"] = segment.get("to_x"), segment.get("from_x")
            item["from_y"], item["to_y"] = segment.get("to_y"), segment.get("from_y")
            item["from_floor"], item["to_floor"] = segment.get("to_floor"), segment.get(
                "from_floor"
            )
            item["amr_rotation_start_deg"], item["amr_rotation_end_deg"] = (
                segment.get("amr_rotation_end_deg"),
                segment.get("amr_rotation_start_deg"),
            )
            item["amr_rotation_deg"] = item.get("amr_rotation_end_deg")
            item["local_path_index"] = index
            reversed_segments.append(item)

        return reversed_segments, total_duration, total_distance

    def _inventory_space_accepts_amr(self, space: dict, amr: AMR) -> bool:
        """Return True when an inventory space can store this AMR.

        AMR bays may be marked either on the inventory-space parent
        (stores_amr/space_type/amr_type) or on a payload_slots[] entry created by
        the editor.  The simulator must honour both shapes so charging-location
        allocation matches editor validation.
        """
        if not isinstance(space, dict):
            return False

        amr_type_values = []
        parent_marks_amr = (
            bool(space.get("stores_amr", False))
            or str(space.get("space_type", "") or "").strip().lower() == "amr"
            or bool(str(space.get("amr_type", "") or "").strip())
        )
        if str(space.get("amr_type", "") or "").strip():
            amr_type_values.append(str(space.get("amr_type", "") or "").strip())

        slot_marks_amr = False
        for slot in space.get("payload_slots", []) or []:
            if not isinstance(slot, dict):
                continue
            slot_type = str(slot.get("slot_type", "") or "").strip().lower()
            slot_amr_type = str(slot.get("amr_type", "") or "").strip()
            if slot_type == "amr" or slot_amr_type:
                slot_marks_amr = True
                if slot_amr_type:
                    amr_type_values.append(slot_amr_type)

        if not (parent_marks_amr or slot_marks_amr):
            return False

        base_id = str(getattr(amr, "id", "") or "").rsplit("-", 1)[0]
        amr_id = str(getattr(amr, "id", "") or "").strip()
        clean_amr_types = {x for x in amr_type_values if x}
        if (
            clean_amr_types
            and base_id not in clean_amr_types
            and amr_id not in clean_amr_types
        ):
            return False

        length_m = float(space.get("length_m", 0.0) or 0.0)
        width_m = float(space.get("width_m", 0.0) or 0.0)
        height_m = float(space.get("height_m", 999999.0) or 999999.0)
        amr_length = float(getattr(amr, "length_m", 0.0) or 0.0)
        amr_width = float(getattr(amr, "width_m", 0.0) or 0.0)
        amr_height = float(getattr(amr, "height_m", 0.0) or 0.0)
        tolerance_m = 1e-3

        def _fits(required: float, available: float) -> bool:
            return float(required) <= (float(available) + tolerance_m)

        fits_normal = _fits(amr_length, length_m) and _fits(amr_width, width_m)
        fits_rotated = _fits(amr_length, width_m) and _fits(amr_width, length_m)
        return (fits_normal or fits_rotated) and _fits(amr_height, height_m)

    def _location_has_any_amr_inventory_spaces(self, location_name: str) -> bool:
        """Return True if the location contains any AMR bay, regardless of type."""
        for space in self.inventory_spaces_by_location.get(
            str(location_name or "").strip(), []
        ):
            if not isinstance(space, dict):
                continue
            if bool(space.get("stores_amr", False)):
                return True
            if str(space.get("space_type", "") or "").strip().lower() == "amr":
                return True
            if str(space.get("amr_type", "") or "").strip():
                return True
            for slot in space.get("payload_slots", []) or []:
                if not isinstance(slot, dict):
                    continue
                if str(slot.get("slot_type", "") or "").strip().lower() == "amr":
                    return True
                if str(slot.get("amr_type", "") or "").strip():
                    return True
        return False

    def _space_name(self, space: dict) -> str:
        return str((space or {}).get("name", "") or "").strip()

    def _amr_by_id(self, amr_id: str) -> Optional[AMR]:
        amr_id = str(amr_id or "").strip()
        if not amr_id:
            return None
        amr = getattr(self, "amrs_by_id", {}).get(amr_id)
        if amr is not None:
            return amr
        for candidate in getattr(self, "amrs", []) or []:
            if str(getattr(candidate, "id", "") or "").strip() == amr_id:
                return candidate
        return None

    def _space_is_available_for_amr(
        self, space: dict, amr: AMR, location_name: str = ""
    ) -> bool:
        """Return True if an AMR bay can be claimed by this AMR.

        Space occupancy can become stale when an AMR leaves a charging bay, is
        reallocated to another bay, or an older CSV/config is replayed.  Do not
        allow stale ``amr_id`` / ``reserved_by_amr`` markers to make all bays at a
        charging location appear unavailable.  A bay is blocked only when the
        other AMR still records that exact bay as its current or target bay at
        the same location.
        """
        amr_id = str(getattr(amr, "id", "") or "").strip()
        space_name = self._space_name(space)
        loc_name = str(location_name or "").strip()

        occupied_by = str(space.get("amr_id", "") or "").strip()
        if occupied_by and occupied_by != amr_id:
            other = self._amr_by_id(occupied_by)
            other_loc = (
                str(getattr(other, "location_name", "") or "").strip() if other else ""
            )
            other_space = (
                str(getattr(other, "inventory_space_name", "") or "").strip()
                if other
                else ""
            )
            if (
                other is not None
                and other_space == space_name
                and (not loc_name or other_loc == loc_name)
            ):
                return False
            # Stale occupancy marker.  Clear it so subsequent AMRs can use the bay.
            space["amr_id"] = ""
            if not str(space.get("payload", "") or "").strip():
                space["occupied"] = False
        elif bool(space.get("occupied", False)) and not occupied_by:
            # AMR bays should not be blocked by a bare occupied flag.  If there
            # is no payload and no AMR id, this is stale state from a prior
            # stow/return and must not prevent a compatible AMR using the bay.
            if not str(space.get("payload", "") or "").strip():
                space["occupied"] = False

        reserved_by = str(space.get("reserved_by_amr", "") or "").strip()
        if reserved_by and reserved_by != amr_id:
            other = self._amr_by_id(reserved_by)
            other_target_loc = (
                str(getattr(other, "target_charge_location", "") or "").strip()
                if other
                else ""
            )
            other_loc = (
                str(getattr(other, "location_name", "") or "").strip() if other else ""
            )
            other_target = (
                str(getattr(other, "target_inventory_space_name", "") or "").strip()
                if other
                else ""
            )
            if (
                other is not None
                and other_target == space_name
                and (
                    not loc_name
                    or other_target_loc == loc_name
                    or other_loc == loc_name
                )
            ):
                return False
            # Stale reservation marker.
            space["reserved_by_amr"] = ""

        return True

    def _space_is_other_amr_home(
        self, location_name: str, space: dict, amr: AMR
    ) -> bool:
        """Avoid stealing another AMR's assigned home bay while it is out working."""
        amr_id = str(getattr(amr, "id", "") or "").strip()
        loc = str(location_name or "").strip()
        name = self._space_name(space)
        if not loc or not name:
            return False
        for other in getattr(self, "amrs", []) or []:
            if str(getattr(other, "id", "") or "").strip() == amr_id:
                continue
            if str(getattr(other, "home_charge_location", "") or "").strip() != loc:
                continue
            if (
                str(getattr(other, "home_inventory_space_name", "") or "").strip()
                == name
            ):
                return True
        return False

    @staticmethod
    def _inventory_space_has_charger(space: dict) -> bool:
        return bool((space or {}).get("has_charger", False))

    def _find_named_amr_inventory_space(
        self, location_name: str, space_name: str, amr: AMR, require_charger: bool = False
    ) -> Optional[dict]:
        space_name = str(space_name or "").strip()
        if not space_name:
            return None
        for space in self.inventory_spaces_by_location.get(
            str(location_name or "").strip(), []
        ):
            if self._space_name(space) != space_name:
                continue
            if not self._inventory_space_accepts_amr(space, amr):
                return None
            if require_charger and not self._inventory_space_has_charger(space):
                return None
            if not self._space_is_available_for_amr(space, amr, location_name):
                return None
            return space
        return None

    def _find_free_amr_inventory_space(
        self, location_name: str, amr: AMR, require_charger: bool = False
    ) -> Optional[dict]:
        """Return the nearest compatible AMR bay at a location.

        AMR bays are a shared charging/stowage resource.  The previous home-bay
        preference could make a location appear full when every compatible bay
        was assigned as somebody else's home, even though a bay was physically
        free.  For return/stow movements we only reject bays occupied or
        reserved by another AMR, then choose the nearest free compatible bay.
        """
        location_name = str(location_name or "").strip()
        source_loc = self.locations.get(
            str(getattr(amr, "location_name", "") or "").strip()
        )

        candidates = []
        for index, space in enumerate(
            self.inventory_spaces_by_location.get(location_name, [])
        ):
            if not self._inventory_space_accepts_amr(space, amr):
                continue
            if require_charger and not self._inventory_space_has_charger(space):
                continue
            if not self._space_is_available_for_amr(space, amr, location_name):
                continue
            centre = self._inventory_space_centre_location(location_name, space)
            if source_loc is not None and centre is not None:
                dist = math.hypot(
                    float(centre.x) - float(source_loc.x),
                    float(centre.y) - float(source_loc.y),
                )
            else:
                dist = float(index)
            candidates.append((dist, index, space))

        if not candidates:
            return None

        candidates.sort(key=lambda item: (item[0], item[1]))
        return candidates[0][2]

    def _reserved_amr_inventory_space(
        self, location_name: str, amr: AMR, require_charger: bool = False
    ) -> Optional[dict]:
        """Return the AMR bay already reserved for this AMR at a location."""
        location_name = str(location_name or "").strip()
        amr_id = str(getattr(amr, "id", "") or "").strip()
        target_name = str(getattr(amr, "target_inventory_space_name", "") or "").strip()
        home_name = ""
        if str(getattr(amr, "home_charge_location", "") or "").strip() == location_name:
            home_name = str(getattr(amr, "home_inventory_space_name", "") or "").strip()
        for space in self.inventory_spaces_by_location.get(location_name, []):
            if not self._inventory_space_accepts_amr(space, amr):
                continue
            if require_charger and not self._inventory_space_has_charger(space):
                continue
            space_name = self._space_name(space)
            reserved_by = str(space.get("reserved_by_amr", "") or "").strip()
            occupied_by = str(space.get("amr_id", "") or "").strip()
            if amr_id and reserved_by == amr_id:
                return space
            if amr_id and occupied_by == amr_id:
                return space
            if (
                target_name
                and space_name == target_name
                and self._space_is_available_for_amr(space, amr, location_name)
            ):
                return space
            if (
                home_name
                and space_name == home_name
                and self._space_is_available_for_amr(space, amr, location_name)
            ):
                return space
        return None

    def _reserve_amr_inventory_space(
        self, amr: AMR, location_name: str, require_charger: bool = False
    ) -> Optional[dict]:
        """Reserve exactly one compatible AMR bay for a return/charge movement."""
        self._clear_amr_inventory_space_reservations(amr)
        space = self._reserved_amr_inventory_space(location_name, amr, require_charger=require_charger)
        if space is None:
            space = self._find_free_amr_inventory_space(location_name, amr, require_charger=require_charger)
        if space is None:
            setattr(amr, "target_inventory_space_name", "")
            return None
        space["reserved_by_amr"] = str(getattr(amr, "id", "") or "").strip()
        setattr(amr, "target_inventory_space_name", str(space.get("name", "") or ""))
        setattr(amr, "target_charge_location", str(location_name or "").strip())
        return space

    def _clear_amr_inventory_space_reservations(self, amr: AMR) -> None:
        amr_id = str(getattr(amr, "id", "") or "").strip()
        if not amr_id:
            return
        for spaces in self.inventory_spaces_by_location.values():
            for space in spaces:
                if str(space.get("reserved_by_amr", "") or "").strip() == amr_id:
                    space["reserved_by_amr"] = ""

    def _free_amr_inventory_space(
        self, amr: AMR, keep_target_reservation: bool = False
    ) -> None:
        amr_id = str(getattr(amr, "id", "") or "").strip()
        if not amr_id:
            return
        for spaces in self.inventory_spaces_by_location.values():
            for space in spaces:
                if str(space.get("amr_id", "") or "").strip() == amr_id:
                    space["amr_id"] = ""
                    if not str(space.get("payload", "") or "").strip():
                        space["occupied"] = False
                if str(space.get("reserved_by_amr", "") or "").strip() == amr_id:
                    if (
                        keep_target_reservation
                        and self._space_name(space)
                        == str(
                            getattr(amr, "target_inventory_space_name", "") or ""
                        ).strip()
                    ):
                        continue
                    space["reserved_by_amr"] = ""
        setattr(amr, "inventory_space_name", "")
        setattr(amr, "rotation_deg", 0.0)
        if not keep_target_reservation:
            setattr(amr, "target_inventory_space_name", "")

    def _occupy_amr_inventory_space(
        self, amr: AMR, location_name: str, require_charger: bool = False
    ) -> Optional[dict]:
        """Claim a compatible AMR bay at arrival time.

        Return/charge planning may reserve a bay while the AMR is still travelling,
        but the live bay state can change before arrival.  Do not fail simply
        because the originally planned bay is no longer usable.  When the AMR
        arrives, release its old bay/reservation, then claim the originally
        targeted bay only if it is still compatible and available; otherwise
        reselect any compatible free AMR bay at that charging location.
        """
        location_name = str(location_name or "").strip()
        amr_id = str(getattr(amr, "id", "") or "").strip()
        target_space_name = str(
            getattr(amr, "target_inventory_space_name", "") or ""
        ).strip()

        # Capture the planned bay name first.  _free_amr_inventory_space() clears
        # this AMR's current occupancy and reservation markers, so we re-resolve
        # the bay from the current live space state afterwards.
        self._free_amr_inventory_space(amr)

        target_space = None
        if target_space_name:
            target_space = self._find_named_amr_inventory_space(
                location_name, target_space_name, amr, require_charger=require_charger
            )

        if target_space is None:
            target_space = self._reserved_amr_inventory_space(location_name, amr, require_charger=require_charger)

        if target_space is None:
            target_space = self._find_free_amr_inventory_space(location_name, amr, require_charger=require_charger)

        if target_space is None:
            setattr(amr, "target_inventory_space_name", "")
            return None

        # Final guard: if the selected bay became occupied/reserved by another
        # AMR between selection and claim, try once more to reselect a free bay
        # instead of blocking the arriving AMR at the location node.
        occupied_by = str(target_space.get("amr_id", "") or "").strip()
        reserved_by = str(target_space.get("reserved_by_amr", "") or "").strip()
        if (occupied_by and occupied_by != amr_id) or (
            reserved_by and reserved_by != amr_id
        ):
            target_space = self._find_free_amr_inventory_space(location_name, amr, require_charger=require_charger)
            if target_space is None:
                setattr(amr, "target_inventory_space_name", "")
                return None

        target_space["amr_id"] = amr_id
        target_space["occupied"] = True
        target_space["reserved_by_amr"] = ""
        setattr(amr, "inventory_space_name", str(target_space.get("name", "") or ""))
        setattr(amr, "rotation_deg", self._inventory_space_rotation_deg(target_space))
        setattr(amr, "target_charge_location", location_name)
        setattr(amr, "target_inventory_space_name", "")
        return target_space

    def _amr_display_location(self, amr: AMR, location_name: str) -> Optional[Location]:
        location_name = str(location_name or "").strip()
        space_name = str(getattr(amr, "inventory_space_name", "") or "").strip()
        if space_name:
            for space in self.inventory_spaces_by_location.get(location_name, []):
                if str(space.get("name", "") or "").strip() == space_name and (
                    str(space.get("amr_id", "") or "").strip()
                    == str(getattr(amr, "id", "") or "").strip()
                    or str(space.get("reserved_by_amr", "") or "").strip()
                    == str(getattr(amr, "id", "") or "").strip()
                ):
                    loc = self._inventory_space_centre_location(location_name, space)
                    if loc is not None:
                        return loc
        return self.locations.get(location_name)

    def _find_free_amr_charge_space(self, amr: AMR) -> Tuple[str, Optional[dict]]:
        """Return a compatible free AMR inventory space at a configured charge location."""
        for location_name in list(getattr(self, "charge_location_names", []) or []):
            location_name = str(location_name or "").strip()
            if not location_name:
                continue
            space = self._find_free_amr_inventory_space(location_name, amr)
            if space is not None:
                return location_name, space
        return "", None

    def _reserve_best_idle_return_destination(
        self,
        amr: AMR,
        task: Task,
        now: float,
        exclude_locations: Optional[set] = None,
    ) -> Tuple[str, Optional[dict]]:
        """Atomically choose and reserve a bay across all charging locations."""
        excluded = {str(x or "").strip() for x in (exclude_locations or set())}
        current_name = str(getattr(amr, "location_name", "") or "").strip()
        current_loc = self.locations.get(current_name)

        ordered_names: List[str] = []
        if current_loc is not None:
            selected = self._select_charge_location_for_amr(amr, current_loc, now)
            if selected is not None:
                ordered_names.append(selected.name)
        ordered_names.extend(list(getattr(self, "charge_location_names", []) or []))
        legacy = str(getattr(self, "charge_location_name", "") or "").strip()
        if legacy:
            ordered_names.append(legacy)

        seen = set()
        for raw_name in ordered_names:
            location_name = str(raw_name or "").strip()
            if (
                not location_name
                or location_name in seen
                or location_name in excluded
                or location_name not in self.locations
            ):
                continue
            seen.add(location_name)

            has_amr_bays = self._location_has_any_amr_inventory_spaces(location_name)
            compatible_spaces = [
                space
                for space in self.inventory_spaces_by_location.get(location_name, [])
                if self._inventory_space_accepts_amr(space, amr)
            ]
            if has_amr_bays and not compatible_spaces:
                continue

            reserved_space = None
            if compatible_spaces:
                reserved_space = self._reserved_amr_inventory_space(location_name, amr)
                if reserved_space is None:
                    reserved_space = self._reserve_amr_inventory_space(
                        amr, location_name
                    )
                if reserved_space is None:
                    continue
            else:
                self._clear_amr_inventory_space_reservations(amr)
                setattr(amr, "target_inventory_space_name", "")
                setattr(amr, "target_charge_location", location_name)

            task.pickup = current_name
            task.dropoff = location_name
            task.target_charge_location = location_name
            task.assigned_amr_inventory_space = str(
                (reserved_space or {}).get("name", "") or ""
            )
            return location_name, reserved_space

        return "", None

    def _assign_initial_amrs_to_charge_inventory_spaces(self) -> None:
        """Place AMRs at charging locations at simulation start.

        Legacy JSON can still contain start_location on AMR definitions, but the
        runtime now treats charging locations as the initial home positions.  If
        a compatible AMR inventory space exists at any configured charging
        location it is occupied; otherwise the AMR starts at the first charging
        location point so older layouts still run while validation reports the
        missing spaces.
        """
        fallback_charge_location = (
            str((getattr(self, "charge_location_names", []) or [""])[0] or "").strip()
            or str(getattr(self, "charge_location_name", "") or "").strip()
        )
        if not fallback_charge_location and self.locations:
            fallback_charge_location = next(iter(self.locations.keys()))

        for amr in getattr(self, "amrs", []):
            location_name, _space = self._find_free_amr_charge_space(amr)
            if not location_name:
                location_name = fallback_charge_location
            if location_name:
                amr.location_name = location_name
                occupied_space = self._occupy_amr_inventory_space(amr, location_name)
                if occupied_space is not None:
                    setattr(amr, "home_charge_location", location_name)
                    setattr(
                        amr,
                        "home_inventory_space_name",
                        str(occupied_space.get("name", "") or ""),
                    )
                    setattr(amr, "target_charge_location", location_name)
                else:
                    setattr(amr, "home_charge_location", location_name)
                    setattr(amr, "home_inventory_space_name", "")
                display_loc = self._amr_display_location(
                    amr, location_name
                ) or self.locations.get(location_name)
                self.log_step(
                    event_time=0.0,
                    event_type="initial_amr_charging_location",
                    amr_id=amr.id,
                    from_location=location_name,
                    to_location=location_name,
                    details=(
                        f"{amr.id} initially placed at charging location {location_name}"
                        + (
                            f" / {occupied_space.get('name', '')}"
                            if occupied_space
                            else ""
                        )
                    ),
                    segment_type="initial_charge_location",
                    start_time=0.0,
                    end_time=0.0,
                    start_node=location_name,
                    end_node=location_name,
                    start_x=getattr(display_loc, "x", None),
                    start_y=getattr(display_loc, "y", None),
                    start_floor=getattr(display_loc, "floor", None),
                    end_x=getattr(display_loc, "x", None),
                    end_y=getattr(display_loc, "y", None),
                    end_floor=getattr(display_loc, "floor", None),
                    status="charging_location",
                    battery_soc_before=amr.battery_soc_percent,
                    battery_soc_after=amr.battery_soc_percent,
                    is_charging=bool(getattr(amr, "is_charging", False)),
                )

    def _location_has_inventory_spaces(self, location_name: str) -> bool:
        # Inventory rules only apply where at least one valid inventory space has
        # been configured and the global ignore/disable flag is not active.
        # When disabled, payload instances are still stored at locations so the
        # report can calculate true operating occupancy from enter/exit events,
        # but finite slot capacity no longer blocks or fails tasks.
        if bool(getattr(self, "disable_inventory_spaces", False)):
            return False
        return bool(self.inventory_spaces_by_location.get(location_name, []))

    def _space_is_amr_only(self, space: dict) -> bool:
        """Return True for inventory spaces reserved for AMR parking/charging.

        AMR bays live in the same inventory_spaces structure as payload slots,
        but they must not be counted as compatible payload stowage.  Empty AMR
        home-return tasks use the synthetic __empty__ payload and previously
        matched every AMR bay because its dimensions are zero, which caused all
        AMR bays to be reserved by payload-task logic.
        """
        if not isinstance(space, dict):
            return False
        if bool(space.get("stores_amr", False)):
            return True
        if str(space.get("space_type", "") or "").strip().lower() == "amr":
            return True
        if str(space.get("amr_type", "") or "").strip():
            return True
        for slot in space.get("payload_slots", []) or []:
            if not isinstance(slot, dict):
                continue
            if str(slot.get("slot_type", "") or "").strip().lower() == "amr":
                return True
            if str(slot.get("amr_type", "") or "").strip():
                return True
        return False

    def _location_has_payload_inventory_spaces(self, location_name: str) -> bool:
        if not self._location_has_inventory_spaces(location_name):
            return False
        return any(
            not self._space_is_amr_only(space)
            for space in self.inventory_spaces_by_location.get(
                str(location_name or "").strip(), []
            )
        )

    def _inventory_space_can_fit_payload(
        self, space: dict, payload: PayloadType
    ) -> bool:
        if self._space_is_amr_only(space):
            return False
        if payload is None or is_empty_payload_name(getattr(payload, "name", "")):
            return False
        allowed_payloads = self._inventory_space_allowed_payloads(space)
        if allowed_payloads and getattr(payload, "name", "") not in allowed_payloads:
            return False
        length_m = float(space.get("length_m", 0.0) or 0.0)
        width_m = float(space.get("width_m", 0.0) or 0.0)
        height_m = float(space.get("height_m", 999999.0) or 999999.0)

        # Allow the trolley/bin to be rotated in plan, but not laid on its side.
        eps = 1e-6
        fits_normal = (
            payload.length_m <= length_m + eps and payload.width_m <= width_m + eps
        )
        fits_rotated = (
            payload.length_m <= width_m + eps and payload.width_m <= length_m + eps
        )
        return (fits_normal or fits_rotated) and payload.height_m <= height_m + eps

    def _inventory_space_allowed_payloads(self, space: dict) -> set:
        allowed = set()
        if not isinstance(space, dict):
            return allowed
        payload_name = str(space.get("payload", "") or "").strip()
        if payload_name and payload_name in self.payloads:
            allowed.add(payload_name)
        for slot in space.get("payload_slots", []) or []:
            if not isinstance(slot, dict):
                continue
            payload_name = str(slot.get("payload", "") or "").strip()
            if payload_name and payload_name in self.payloads:
                allowed.add(payload_name)
        return allowed

    def _mark_task_activity_changed(self) -> None:
        self._task_activity_version += 1

    def _active_task_ids(self) -> set:
        """Return task ids that can still legitimately hold reservations.

        The result is cached until the pending-task or event heap changes. This
        avoids an O(pending + future events) scan for every inventory-space test.
        """
        if self._active_task_ids_cache_version == self._task_activity_version:
            return self._active_task_ids_cache

        active = set()
        removed = self._removed_pending_task_ids
        for _priority, _release, _counter, task in self.pending_tasks:
            task_id = str(getattr(task, "id", "") or "").strip()
            if task_id and task_id not in removed:
                active.add(task_id)
        for event in self.events:
            payload = getattr(event, "payload", {}) or {}
            if not isinstance(payload, dict):
                continue
            task = payload.get("task")
            if task is not None:
                task_id = str(getattr(task, "id", "") or "").strip()
                if task_id:
                    active.add(task_id)
            for task in payload.get("tasks", []) or []:
                task_id = str(getattr(task, "id", "") or "").strip()
                if task_id:
                    active.add(task_id)
            task_id = str(payload.get("task_id", "") or "").strip()
            if task_id:
                active.add(task_id)

        self._active_task_ids_cache = active
        self._active_task_ids_cache_version = self._task_activity_version
        return active

    def _clear_stale_payload_inventory_reservations(
        self, location_name: str = ""
    ) -> None:
        """Remove reservations that no live task can still use.

        Availability checks only need to clean the location being queried. The
        previous global sweep visited every inventory space in the model for each
        candidate task, which scaled badly on large estates.
        """
        active = self._active_task_ids()
        failed = self.failed_task_ids
        location_name = str(location_name or "").strip()
        if location_name:
            space_groups = (self.inventory_spaces_by_location.get(location_name, []),)
        else:
            space_groups = self.inventory_spaces_by_location.values()
        for spaces in space_groups:
            for space in spaces or []:
                reserved_by = str(space.get("reserved_by_task", "") or "").strip()
                if not reserved_by:
                    continue
                if reserved_by in active and reserved_by not in failed:
                    continue
                space["reserved_by_task"] = ""

    def _clear_inventory_space_reservation_for_task(self, task: Task) -> None:
        task_id = str(getattr(task, "id", "") or "").strip()
        if not task_id:
            return
        for spaces in self.inventory_spaces_by_location.values():
            for space in spaces or []:
                if str(space.get("reserved_by_task", "") or "").strip() == task_id:
                    space["reserved_by_task"] = ""

    def _find_free_inventory_space(
        self, location_name: str, payload: PayloadType, task: Optional[Task] = None
    ) -> Optional[dict]:
        task_id = str(getattr(task, "id", "") or "").strip() if task is not None else ""
        self._clear_stale_payload_inventory_reservations(location_name)
        for space in self.inventory_spaces_by_location.get(location_name, []):
            if bool(space.get("occupied", False)):
                continue
            reserved_by = str(space.get("reserved_by_task", "") or "").strip()
            if reserved_by and reserved_by != task_id:
                continue
            if not self._inventory_space_can_fit_payload(space, payload):
                continue
            return space
        return None

    def _inventory_pending_reason(
        self, location_name: str, payload: PayloadType
    ) -> str:
        self._clear_stale_payload_inventory_reservations(location_name)
        spaces = self.inventory_spaces_by_location.get(location_name, [])
        if not spaces:
            return ""

        compatible_spaces = [
            space
            for space in spaces
            if self._inventory_space_can_fit_payload(space, payload)
        ]
        compatible_count = len(compatible_spaces)
        occupied_count = sum(
            1 for space in compatible_spaces if bool(space.get("occupied", False))
        )
        reserved_count = sum(
            1
            for space in compatible_spaces
            if str(space.get("reserved_by_task", "") or "").strip()
        )
        free_count = sum(
            1
            for space in compatible_spaces
            if not bool(space.get("occupied", False))
            and not str(space.get("reserved_by_task", "") or "").strip()
        )

        if compatible_count <= 0:
            return (
                f"No compatible inventory space at {location_name} for payload "
                f"{payload.name} ({payload.length_m}m x {payload.width_m}m x {payload.height_m}m); "
                f"configured_spaces={len(spaces)}"
            )

        return (
            f"All compatible inventory spaces are full at {location_name}; cannot stow payload "
            f"{payload.name}; compatible_spaces={compatible_count}; occupied={occupied_count}; "
            f"reserved={reserved_count}; free={free_count}"
        )

    def _reserve_inventory_space_for_task(
        self, task: Task, payload: PayloadType
    ) -> Optional[dict]:
        if payload is None or is_empty_payload_name(getattr(payload, "name", "")):
            return None
        if not self._location_has_payload_inventory_spaces(task.dropoff):
            return None

        task_id = str(getattr(task, "id", "") or "").strip()
        assigned_name = str(getattr(task, "assigned_inventory_space", "") or "").strip()
        self._clear_stale_payload_inventory_reservations(task.dropoff)

        # Reuse this task's existing reservation/assignment instead of reserving
        # a second bay at completion time.  The previous behaviour could make a
        # store look full with reserved=2 even though those reservations belonged
        # to the same already-planned task flow.
        for space in self.inventory_spaces_by_location.get(task.dropoff, []):
            space_name = str(space.get("name", "") or "").strip()
            reserved_by = str(space.get("reserved_by_task", "") or "").strip()
            if assigned_name and space_name != assigned_name:
                continue
            if assigned_name or (task_id and reserved_by == task_id):
                if bool(space.get("occupied", False)) and reserved_by != task_id:
                    return None
                if not self._inventory_space_can_fit_payload(space, payload):
                    return None
                space["reserved_by_task"] = task_id
                task.assigned_inventory_space = space_name
                return space

        space = self._find_free_inventory_space(task.dropoff, payload, task=task)
        if space is None:
            return None

        space["reserved_by_task"] = task_id
        task.assigned_inventory_space = str(space.get("name", ""))
        return space

    def _occupy_inventory_space_for_completed_task(
        self, task: Task, payload: PayloadType
    ) -> bool:
        if payload is None or is_empty_payload_name(getattr(payload, "name", "")):
            self._record_location_storage_peak(task.dropoff)
            return True
        if not self._location_has_payload_inventory_spaces(task.dropoff):
            self._record_location_storage_peak(task.dropoff)
            return True

        target_name = str(getattr(task, "assigned_inventory_space", "")).strip()
        spaces = self.inventory_spaces_by_location.get(task.dropoff, [])

        target_space = None
        for space in spaces:
            if target_name and str(space.get("name", "")) == target_name:
                target_space = space
                break

        if target_space is None:
            target_space = next(
                (
                    space
                    for space in spaces
                    if str(space.get("reserved_by_task", "")) == task.id
                ),
                None,
            )

        if target_space is None:
            target_space = self._find_free_inventory_space(
                task.dropoff, payload, task=task
            )

        if target_space is None:
            self._set_task_pending_reason(
                task, self._inventory_pending_reason(task.dropoff, payload)
            )
            self._record_location_storage_peak(task.dropoff)
            return False

        if (
            bool(target_space.get("occupied", False))
            and str(target_space.get("reserved_by_task", "") or "").strip() != task.id
        ):
            self._set_task_pending_reason(
                task, self._inventory_pending_reason(task.dropoff, payload)
            )
            self._record_location_storage_peak(task.dropoff)
            return False

        target_space["occupied"] = True
        target_space["payload"] = payload.name
        target_space["payload_instance_id"] = str(
            getattr(task, "payload_instance_id", "") or ""
        )
        target_space["task_id"] = task.id
        target_space["reserved_by_task"] = ""
        task.assigned_inventory_space = str(target_space.get("name", ""))
        self._record_location_storage_peak(task.dropoff)
        return True

    def _free_inventory_space_for_pickup(
        self, task: Task, payload: PayloadType
    ) -> None:
        if payload is None or is_empty_payload_name(getattr(payload, "name", "")):
            return
        if not self._location_has_payload_inventory_spaces(task.pickup):
            return

        spaces = self.inventory_spaces_by_location.get(task.pickup, [])
        for space in spaces:
            if not bool(space.get("occupied", False)):
                continue
            stored_payload = str(space.get("payload", "")).strip()
            if stored_payload and stored_payload != payload.name:
                continue
            wanted_instance_id = str(
                getattr(task, "payload_instance_id", "") or ""
            ).strip()
            stored_instance_id = str(space.get("payload_instance_id", "") or "").strip()
            if (
                wanted_instance_id
                and stored_instance_id
                and wanted_instance_id != stored_instance_id
            ):
                continue
            space["occupied"] = False
            space["payload"] = ""
            space["payload_instance_id"] = ""
            space["task_id"] = ""
            space["reserved_by_task"] = ""
            self._record_location_storage_peak(task.pickup)
            return

    def _set_task_pending_reason(self, task: Optional[Task], reason: str) -> None:
        if task is None:
            return
        try:
            task.pending_reason = str(reason or "").strip()
        except Exception:
            pass

    def _task_tracking_log_kwargs(self, task: Optional[Task]) -> dict:
        if task is None:
            return {}
        return {
            "tracked_item_exchange": bool(
                getattr(task, "tracked_item_exchange", False)
            ),
            "exchange_mode": str(getattr(task, "exchange_mode", "") or ""),
            "tracked_item_source_payload": str(
                getattr(task, "tracked_item_source_payload", "") or ""
            ),
            "tracked_items": getattr(task, "tracked_items", {}) or {},
        }

    def _task_category_key(self, task: Optional[Task]) -> str:
        if task is None:
            return ""
        explicit = str(getattr(task, "staff_category_key", "") or "").strip().lower()
        if explicit:
            return explicit
        labels = [
            str(label).strip().lower()
            for label in (getattr(task, "labels", []) or [])
            if str(label).strip()
        ]
        for label in labels:
            if label != "return":
                return label
        task_id = str(getattr(task, "id", "") or "").strip().upper()
        if task_id.startswith("GEN_"):
            parts = task_id.split("_")
            if len(parts) > 1 and parts[1]:
                return parts[1].lower()
        source = str(getattr(task, "task_source", "") or "").strip().lower()
        if source == "department_waste":
            return "waste"
        return source or normalise_payload_name(getattr(task, "payload", "")).lower()

    def _task_is_stores_delivery(self, task: Optional[Task]) -> bool:
        if task is None or bool(getattr(task, "is_return_task", False)):
            return False
        labels = {
            str(label).strip().lower()
            for label in (getattr(task, "labels", []) or [])
            if str(label).strip()
        }
        if "stores" in labels:
            return True
        task_id = str(getattr(task, "id", "") or "").strip().upper()
        if task_id.startswith("GEN_STORES_"):
            return True
        source = str(getattr(task, "task_source", "") or "").strip().lower()
        payload = normalise_payload_name(getattr(task, "payload", ""))
        return source == "stores" or payload == "stores"

    def _staff_pool_for_category(
        self,
        category_key: str,
        initial_count: int,
        resource_name: str = "",
        shift_pattern: str = "none",
    ) -> dict:
        category_key = str(category_key or "").strip().lower() or "staff"
        shift_pattern = self._normalise_staff_shift_pattern(shift_pattern)
        shift_multiplier = self._staff_shift_multiplier(shift_pattern)
        pool = self.staff_resource_pools.get(category_key)
        if pool is not None:
            if shift_pattern != "none":
                pool["shift_pattern"] = shift_pattern
                pool["shift_multiplier"] = max(
                    float(pool.get("shift_multiplier", 1.0) or 1.0),
                    shift_multiplier,
                )
            return pool

        initial_count = max(1, int(initial_count or 1))
        label = str(resource_name or "").strip() or f"{category_key.title()} staff"
        pool = {
            "category_key": category_key,
            "resource_name": label,
            "initial_people": initial_count,
            "available_times": [0.0 for _ in range(initial_count)],
            "last_locations": ["" for _ in range(initial_count)],
            "assignments": [],
            "active_location_batches": {},
            "preferred_people_by_location": {},
            "shift_pattern": shift_pattern,
            "shift_multiplier": shift_multiplier,
        }
        self.staff_resource_pools[category_key] = pool
        return pool

    @staticmethod
    def _normalise_staff_shift_pattern(value: str) -> str:
        pattern = str(value or "").strip().lower()
        if pattern in {
            "4_on_4_off_12h",
            "four_on_four_off",
            "four_on_four_off_12_hour",
        }:
            pattern = "four_on_four_off_12h"
        return pattern if pattern in {"none", "four_on_four_off_12h"} else "none"

    @staticmethod
    def _staff_shift_multiplier(shift_pattern: str) -> float:
        return float(Simulation._staff_shift_team_count(shift_pattern))

    @staticmethod
    def _staff_shift_team_count(shift_pattern: str) -> int:
        return 2 if shift_pattern == "four_on_four_off_12h" else 1

    @staticmethod
    def _staff_rostered_count(on_shift_count: int, shift_multiplier: float) -> int:
        try:
            count = max(0, int(on_shift_count or 0))
            multiplier = max(1.0, float(shift_multiplier or 1.0))
        except Exception:
            count = max(0, int(on_shift_count or 0))
            multiplier = 1.0
        return int(math.ceil(count * multiplier))

    def _staff_shift_team_key(self, shift_pattern: str, sim_time_sec: float) -> str:
        shift_pattern = self._normalise_staff_shift_pattern(shift_pattern)
        if shift_pattern != "four_on_four_off_12h":
            return ""
        block = int(max(0.0, float(sim_time_sec or 0.0)) // (4 * 24 * 60 * 60))
        return "A" if block % 2 == 0 else "B"

    def _staff_shift_roster(self, pool: dict, sim_time_sec: float) -> dict:
        shift_pattern = str(pool.get("shift_pattern", "none") or "none")
        team_key = self._staff_shift_team_key(shift_pattern, sim_time_sec)
        if not team_key:
            return pool
        rosters = pool.setdefault("shift_rosters", {})
        roster = rosters.get(team_key)
        if roster is None:
            initial_count = max(1, int(pool.get("initial_people", 1) or 1))
            roster = {
                "shift_team": team_key,
                "available_times": [0.0 for _ in range(initial_count)],
                "last_locations": ["" for _ in range(initial_count)],
                "active_location_batches": {},
                "preferred_people_by_location": {},
            }
            rosters[team_key] = roster
        return roster

    def _staff_pool_on_shift_count(self, pool: dict) -> int:
        rosters = pool.get("shift_rosters")
        if isinstance(rosters, dict) and rosters:
            return max(
                len((roster or {}).get("available_times", []) or [])
                for roster in rosters.values()
            )
        return len(pool.get("available_times", []) or [])

    def _task_staff_handling_config(self, task: Optional[Task]) -> Optional[dict]:
        if task is None or bool(getattr(task, "is_return_task", False)):
            return None
        requires_staff = bool(
            getattr(task, "requires_staff", False)
            or getattr(task, "staff_required", False)
        )
        if not requires_staff and self._task_is_stores_delivery(task):
            # Backward-compatible default for older configs created before the
            # editor exposed category staff settings.
            requires_staff = True
        if not requires_staff:
            return None
        category_key = self._task_category_key(task) or "staff"
        resource_name = str(getattr(task, "staff_resource_name", "") or "").strip()
        try:
            initial_count = max(
                1, int(float(getattr(task, "staff_initial_count", 1) or 1))
            )
        except Exception:
            initial_count = 1
        policy = str(
            getattr(task, "staff_movement_policy", "batch_same_location") or ""
        ).strip().lower()
        if policy not in {
            "available_first",
            "batch_same_location",
            "minimise_movement",
            "minimize_movement",
        }:
            policy = "batch_same_location"
        if policy == "minimize_movement":
            policy = "minimise_movement"
        shift_pattern = self._normalise_staff_shift_pattern(
            getattr(task, "staff_shift_pattern", "none") or "none"
        )
        return {
            "category_key": category_key,
            "resource_name": resource_name,
            "initial_count": initial_count,
            "movement_policy": policy,
            "shift_pattern": shift_pattern,
            "handling_minutes": max(
                0.0, float(getattr(task, "staff_handling_minutes", 0.0) or 0.0)
            ),
            "use_custom_working_hours": bool(
                getattr(task, "staff_use_custom_working_hours", False)
            ),
            "working_hours": dict(
                getattr(task, "staff_working_hours", {}) or {}
            ),
        }

    @staticmethod
    def _parse_staff_hhmm(value: str) -> Optional[int]:
        text = str(value or "").strip()
        try:
            hour_text, minute_text = text.split(":", 1)
            hour = int(hour_text)
            minute = int(minute_text)
        except Exception:
            return None
        if hour == 24 and minute == 0:
            return 24 * 60
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour * 60 + minute
        return None

    def _staff_work_period_for_day(self, task: Task, day_dt) -> Optional[Tuple[float, float]]:
        day_keys = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
        day_key = day_keys[day_dt.weekday()]
        use_custom = bool(getattr(task, "staff_use_custom_working_hours", False))
        start_text = ""
        end_text = ""
        if use_custom:
            weekly = getattr(task, "staff_working_hours", {}) or {}
            day_cfg = weekly.get(day_key, {}) if isinstance(weekly, dict) else {}
            if not isinstance(day_cfg, dict) or not _bool_from_config(
                day_cfg.get("enabled", False), False
            ):
                return None
            start_text = str(day_cfg.get("start_time", "09:00") or "09:00")
            end_text = str(day_cfg.get("end_time", "17:00") or "17:00")
        else:
            start_text = str(getattr(task, "staff_shift_start_time", "") or "")
            end_text = str(getattr(task, "staff_shift_end_time", "") or "")
            active_days = {
                str(value or "").strip().lower()[:3]
                for value in (getattr(task, "staff_shift_days_active", []) or [])
                if str(value or "").strip()
            }
            pattern = self._normalise_staff_shift_pattern(
                getattr(task, "staff_shift_pattern", "none") or "none"
            )
            if pattern != "four_on_four_off_12h" and active_days and day_key not in active_days:
                return None
            # Older direct/static task records did not carry shift metadata. Keep
            # their previous 24-hour availability rather than silently dropping work.
            if not start_text and not end_text:
                start_minutes, end_minutes = 0, 24 * 60
            else:
                start_minutes = self._parse_staff_hhmm(start_text)
                end_minutes = self._parse_staff_hhmm(end_text)
        if use_custom or start_text or end_text:
            start_minutes = self._parse_staff_hhmm(start_text)
            end_minutes = self._parse_staff_hhmm(end_text)
        if start_minutes is None or end_minutes is None or start_minutes == end_minutes:
            return None
        day_start = day_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        start_dt = day_start + __import__("datetime").timedelta(minutes=start_minutes)
        end_dt = day_start + __import__("datetime").timedelta(minutes=end_minutes)
        if end_minutes <= start_minutes:
            end_dt += __import__("datetime").timedelta(days=1)
        origin = self.clock.start_datetime
        return (
            (start_dt - origin).total_seconds(),
            (end_dt - origin).total_seconds(),
        )

    def _next_staff_assignment_window(
        self,
        task: Task,
        requested_handling_start: float,
        person_available_time: float,
        travel_duration_sec: float,
        handling_duration_sec: float,
    ) -> dict:
        requested = max(0.0, float(requested_handling_start or 0.0))
        available = max(0.0, float(person_available_time or 0.0))
        travel = max(0.0, float(travel_duration_sec or 0.0))
        handling = max(0.0, float(handling_duration_sec or 0.0))
        reference_dt = self.clock.sim_seconds_to_datetime(requested)
        first_day = reference_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        # Include the previous day so an overnight shift remains usable after midnight.
        first_day -= __import__("datetime").timedelta(days=1)
        for day_offset in range(0, 35):
            day_dt = first_day + __import__("datetime").timedelta(days=day_offset)
            period = self._staff_work_period_for_day(task, day_dt)
            if period is None:
                continue
            period_start, period_end = period
            earliest_travel_start = max(available, period_start)
            earliest_arrival = earliest_travel_start + travel
            handling_start = max(requested, earliest_arrival)
            # Travel is scheduled just in time where possible, but never before
            # the person is free or before their working period starts.
            travel_start = max(earliest_travel_start, handling_start - travel)
            travel_finish = travel_start + travel
            handling_start = max(handling_start, travel_finish)
            finish_time = handling_start + handling
            if finish_time <= period_end + 1e-9:
                return {
                    "travel_start_time": travel_start,
                    "travel_finish_time": travel_finish,
                    "start_time": handling_start,
                    "finish_time": finish_time,
                    "work_period_start": period_start,
                    "work_period_end": period_end,
                }
        # Invalid schedules should not make the simulation unusable. This fallback
        # preserves chronological movement and is exposed in assignment metadata.
        travel_start = max(available, requested - travel)
        travel_finish = travel_start + travel
        handling_start = max(requested, travel_finish)
        return {
            "travel_start_time": travel_start,
            "travel_finish_time": travel_finish,
            "start_time": handling_start,
            "finish_time": handling_start + handling,
            "work_period_start": None,
            "work_period_end": None,
            "working_hours_fallback": True,
        }

    def _staff_node_coordinates(self, node_name: str, floor: int) -> Optional[Tuple[float, float]]:
        location = self.locations.get(str(node_name or ""))
        if location is not None and int(location.floor) == int(floor):
            return float(location.x), float(location.y)
        node_name = str(node_name or "")
        for lift in self.lifts:
            if node_name == f"{lift.id}-F{int(floor)}":
                coords = (getattr(lift, "floor_locations", {}) or {}).get(int(floor))
                if coords is not None and len(coords) >= 2:
                    return float(coords[0]), float(coords[1])
        return None

    def _staff_same_floor_distance(self, floor: int, start_name: str, end_name: str) -> float:
        if not start_name or not end_name or start_name == end_name:
            return 0.0
        try:
            route = self._shortest_path_same_floor(int(floor), start_name, end_name)
        except Exception:
            route = None
        if route is not None:
            try:
                return max(0.0, float(route.get("distance_m", 0.0) or 0.0))
            except Exception:
                pass
        start_xy = self._staff_node_coordinates(start_name, floor)
        end_xy = self._staff_node_coordinates(end_name, floor)
        if start_xy is not None and end_xy is not None:
            return math.hypot(end_xy[0] - start_xy[0], end_xy[1] - start_xy[1])
        return 0.0

    def _staff_travel_between_locations(
        self, from_location: str, to_location: str
    ) -> Tuple[float, float, str]:
        from_name = str(from_location or "").strip()
        to_name = str(to_location or "").strip()
        if not from_name or not to_name or from_name == to_name:
            return 0.0, 0.0, ""
        cache_key = (from_name, to_name)
        cached = self._staff_travel_cache.get(cache_key)
        if cached is not None:
            return cached
        start = self.locations.get(from_name)
        finish = self.locations.get(to_name)
        speed = max(0.1, float(self.staff_walk_speed_m_per_sec or 1.2))
        if start is None or finish is None:
            result = (0.0, 0.0, "")
            self._staff_travel_cache[cache_key] = result
            return result
        if int(start.floor) == int(finish.floor):
            distance = self._staff_same_floor_distance(start.floor, from_name, to_name)
            result = (distance / speed, distance, "")
            self._staff_travel_cache[cache_key] = result
            return result

        best = None
        for lift in self.lifts:
            if not lift.can_serve(int(start.floor), int(finish.floor)):
                continue
            start_node = f"{lift.id}-F{int(start.floor)}"
            finish_node = f"{lift.id}-F{int(finish.floor)}"
            first_distance = self._staff_same_floor_distance(
                start.floor, from_name, start_node
            )
            last_distance = self._staff_same_floor_distance(
                finish.floor, finish_node, to_name
            )
            vertical_distance = abs(int(finish.floor) - int(start.floor)) * float(
                getattr(self, "floor_height_m", 4.0) or 4.0
            )
            try:
                lift_travel = float(
                    lift.vertical_travel_duration_sec(
                        int(finish.floor) - int(start.floor),
                        float(getattr(self, "floor_height_m", 4.0) or 4.0),
                    )
                )
            except Exception:
                lift_travel = abs(int(finish.floor) - int(start.floor)) / max(
                    float(getattr(lift, "speed_floors_per_sec", 1.0) or 1.0), 1e-9
                )
            service_time = (
                float(self.staff_lift_wait_seconds or 0.0)
                + 2.0 * max(0.0, float(getattr(lift, "door_time_sec", 0.0) or 0.0))
                + 2.0 * max(0.0, float(getattr(lift, "boarding_time_sec", 0.0) or 0.0))
            )
            total_duration = (first_distance + last_distance) / speed + lift_travel + service_time
            total_distance = first_distance + last_distance + vertical_distance
            candidate = (total_duration, total_distance, str(lift.id))
            if best is None or candidate[0] < best[0]:
                best = candidate
        if best is None:
            horizontal = math.hypot(float(finish.x) - float(start.x), float(finish.y) - float(start.y))
            floor_delta = abs(int(finish.floor) - int(start.floor))
            best = (
                horizontal / speed + floor_delta * 60.0 + float(self.staff_lift_wait_seconds or 0.0),
                horizontal + floor_delta * float(getattr(self, "floor_height_m", 4.0) or 4.0),
                "",
            )
        self._staff_travel_cache[cache_key] = best
        return best

    def _staff_handling_duration_sec(self, task: Task, return_delay_sec: float) -> float:
        try:
            explicit_minutes = max(
                0.0, float(getattr(task, "staff_handling_minutes", 0.0) or 0.0)
            )
        except Exception:
            explicit_minutes = 0.0
        if explicit_minutes > 0.0:
            return explicit_minutes * 60.0
        # Preserve old projects where return_delay_minutes doubled as the payload
        # handling period before a return was generated.
        if return_delay_sec > 0.0:
            return float(return_delay_sec)
        return max(0.0, float(self.staff_default_handling_minutes or 0.0) * 60.0)

    def _assign_staff_for_handling(
        self,
        task: Task,
        start_time: float,
        handling_duration_sec: float,
    ) -> Optional[dict]:
        staff_cfg = self._task_staff_handling_config(task)
        if staff_cfg is None:
            return None
        duration = max(0.0, float(handling_duration_sec or 0.0))
        if duration <= 0.0:
            return None

        requested_start = max(0.0, float(start_time or 0.0))
        pool = self._staff_pool_for_category(
            staff_cfg["category_key"],
            staff_cfg["initial_count"],
            staff_cfg["resource_name"],
            staff_cfg["shift_pattern"],
        )
        baseline_window = self._next_staff_assignment_window(
            task, requested_start, 0.0, 0.0, duration
        )
        baseline_start = float(baseline_window["start_time"] or requested_start)
        roster = self._staff_shift_roster(pool, baseline_start)
        available_times = roster.setdefault("available_times", [])
        last_locations = roster.setdefault(
            "last_locations", ["" for _ in range(len(available_times))]
        )
        while len(last_locations) < len(available_times):
            last_locations.append("")
        shift_team = str(roster.get("shift_team", "") or "")
        shift_pattern = str(pool.get("shift_pattern", "none") or "none")
        shift_multiplier = float(pool.get("shift_multiplier", 1.0) or 1.0)
        location_name = str(getattr(task, "dropoff", "") or "").strip()
        movement_policy = str(
            staff_cfg.get("movement_policy", "batch_same_location") or ""
        ).strip().lower()
        active_batches = roster.setdefault("active_location_batches", {})
        active_assignment = (
            active_batches.get(location_name)
            if movement_policy in {"batch_same_location", "minimise_movement"}
            else None
        )
        if location_name and active_assignment is not None:
            active_finish = float(active_assignment.get("finish_time", 0.0) or 0.0)
            if active_finish > baseline_start + 1e-9:
                person_index = max(
                    0, int(active_assignment.get("person_index", 1) or 1) - 1
                )
                actual_start = max(
                    baseline_start, float(active_assignment.get("start_time", baseline_start) or baseline_start)
                )
                finish_time = max(actual_start + duration, active_finish)
                if person_index < len(available_times):
                    available_times[person_index] = max(
                        float(available_times[person_index] or 0.0), finish_time
                    )
                    last_locations[person_index] = location_name
                assignment = {
                    "task_id": str(getattr(task, "id", "") or ""),
                    "category_key": str(pool["category_key"]),
                    "resource_name": str(pool.get("resource_name", "") or ""),
                    "person_id": str(active_assignment.get("person_id", "")),
                    "person_index": person_index + 1,
                    "requested_start_time": round(requested_start, 3),
                    "start_time": round(actual_start, 3),
                    "finish_time": round(finish_time, 3),
                    "duration_sec": round(duration, 3),
                    "location": location_name,
                    "payload": self._payload_log_name(getattr(task, "payload", "")),
                    "people_required": self._staff_rostered_count(
                        self._staff_pool_on_shift_count(pool), shift_multiplier
                    ),
                    "staff_on_shift_people_required": self._staff_pool_on_shift_count(pool),
                    "staff_shift_pattern": shift_pattern,
                    "staff_shift_team": shift_team,
                    "staff_shift_multiplier": shift_multiplier,
                    "staff_initial_on_shift_people": int(pool.get("initial_people", 0) or 0),
                    "staff_initial_rostered_people": self._staff_rostered_count(
                        int(pool.get("initial_people", 0) or 0), shift_multiplier
                    ),
                    "added_person": False,
                    "shared_location_batch": True,
                    "travel_from_location": location_name,
                    "travel_to_location": location_name,
                    "travel_duration_sec": 0.0,
                    "travel_distance_m": 0.0,
                    "travel_lift_id": "",
                    "travel_start_time": round(actual_start, 3),
                    "travel_finish_time": round(actual_start, 3),
                    "staff_wait_for_travel_sec": round(max(0.0, actual_start - requested_start), 3),
                    "working_hours_fallback": bool(
                        baseline_window.get("working_hours_fallback", False)
                    ),
                }
                active_batches[location_name] = assignment
                pool["assignments"].append(assignment)
                self.staff_assignments.append(assignment)
                return assignment

        candidates = []
        preferred_people = roster.setdefault("preferred_people_by_location", {})
        preferred_index = preferred_people.get(location_name)
        for idx, available_time in enumerate(available_times):
            previous_location = str(last_locations[idx] or "")
            travel_duration, travel_distance, lift_id = self._staff_travel_between_locations(
                previous_location, location_name
            )
            window = self._next_staff_assignment_window(
                task, requested_start, float(available_time or 0.0), travel_duration, duration
            )
            preference_rank = 0 if (
                movement_policy == "minimise_movement"
                and isinstance(preferred_index, int)
                and preferred_index == idx
            ) else 1
            candidates.append(
                {
                    "person_index": idx,
                    "available_time": float(available_time or 0.0),
                    "previous_location": previous_location,
                    "travel_duration_sec": travel_duration,
                    "travel_distance_m": travel_distance,
                    "travel_lift_id": lift_id,
                    "window": window,
                    "sort_key": (
                        float(window["start_time"]),
                        preference_rank,
                        travel_duration,
                        idx,
                    ),
                }
            )
        candidates.sort(key=lambda item: item["sort_key"])

        chosen = None
        if candidates and candidates[0]["window"]["start_time"] <= baseline_start + 1e-9:
            chosen = candidates[0]
        else:
            free_before_baseline = [
                item for item in candidates
                if item["available_time"] <= baseline_start + 1e-9
            ]
            if free_before_baseline:
                # A person is free but must physically travel. Delay the handling
                # instead of inventing another person at the destination.
                chosen = min(free_before_baseline, key=lambda item: item["sort_key"])

        added_person = False
        if chosen is None:
            available_times.append(0.0)
            last_locations.append("")
            person_index = len(available_times) - 1
            chosen = {
                "person_index": person_index,
                "available_time": 0.0,
                "previous_location": "",
                "travel_duration_sec": 0.0,
                "travel_distance_m": 0.0,
                "travel_lift_id": "",
                "window": baseline_window,
            }
            added_person = True

        person_index = int(chosen["person_index"])
        window = chosen["window"]
        actual_start = float(window["start_time"] or baseline_start)
        finish_time = float(window["finish_time"] or (actual_start + duration))
        available_times[person_index] = finish_time
        last_locations[person_index] = location_name
        category_key = str(pool["category_key"])
        team_prefix = f"shift-{shift_team}-" if shift_team else ""
        assignment = {
            "task_id": str(getattr(task, "id", "") or ""),
            "category_key": category_key,
            "resource_name": str(pool.get("resource_name", "") or ""),
            "person_id": f"{category_key}-{team_prefix}person-{person_index + 1}",
            "person_index": person_index + 1,
            "requested_start_time": round(requested_start, 3),
            "start_time": round(actual_start, 3),
            "finish_time": round(finish_time, 3),
            "duration_sec": round(duration, 3),
            "location": location_name,
            "payload": self._payload_log_name(getattr(task, "payload", "")),
            "people_required": self._staff_rostered_count(
                self._staff_pool_on_shift_count(pool), shift_multiplier
            ),
            "staff_on_shift_people_required": self._staff_pool_on_shift_count(pool),
            "staff_shift_pattern": shift_pattern,
            "staff_shift_team": shift_team,
            "staff_shift_multiplier": shift_multiplier,
            "staff_initial_on_shift_people": int(pool.get("initial_people", 0) or 0),
            "staff_initial_rostered_people": self._staff_rostered_count(
                int(pool.get("initial_people", 0) or 0), shift_multiplier
            ),
            "added_person": added_person,
            "shared_location_batch": False,
            "travel_from_location": str(chosen.get("previous_location", "") or ""),
            "travel_to_location": location_name,
            "travel_duration_sec": round(float(chosen.get("travel_duration_sec", 0.0) or 0.0), 3),
            "travel_distance_m": round(float(chosen.get("travel_distance_m", 0.0) or 0.0), 3),
            "travel_lift_id": str(chosen.get("travel_lift_id", "") or ""),
            "travel_start_time": round(float(window.get("travel_start_time", actual_start) or actual_start), 3),
            "travel_finish_time": round(float(window.get("travel_finish_time", actual_start) or actual_start), 3),
            "staff_wait_for_travel_sec": round(max(0.0, actual_start - requested_start), 3),
            "working_hours_fallback": bool(window.get("working_hours_fallback", False)),
        }
        if location_name:
            if movement_policy in {"batch_same_location", "minimise_movement"}:
                active_batches[location_name] = assignment
            if movement_policy == "minimise_movement":
                preferred_people[location_name] = person_index
        pool["assignments"].append(assignment)
        self.staff_assignments.append(assignment)
        return assignment

    def _log_staff_handling_assignment(self, task: Task, assignment: Optional[dict]) -> None:
        if assignment is None:
            return
        category_key = str(assignment.get("category_key", "") or "")
        resource_name = str(assignment.get("resource_name", "") or category_key or "Staff")
        location_name = str(assignment.get("location", "") or getattr(task, "dropoff", "") or "")
        location = self.locations.get(location_name)
        travel_from = str(assignment.get("travel_from_location", "") or "")
        travel_to = str(assignment.get("travel_to_location", "") or location_name)
        travel_start = float(assignment.get("travel_start_time", assignment.get("start_time", 0.0)) or 0.0)
        travel_finish = float(assignment.get("travel_finish_time", travel_start) or travel_start)
        travel_duration = max(0.0, float(assignment.get("travel_duration_sec", 0.0) or 0.0))
        if travel_duration > 1e-9 and travel_from:
            from_loc = self.locations.get(travel_from)
            to_loc = self.locations.get(travel_to)
            self.log_step(
                event_time=travel_start,
                event_type="staff_travel",
                task_id=str(getattr(task, "id", "") or ""),
                details=(
                    f"{assignment['person_id']} travels from {travel_from} to {travel_to} "
                    f"before handling {resource_name} payload"
                ),
                from_location=travel_from,
                to_location=travel_to,
                duration_sec=travel_duration,
                distance_m=float(assignment.get("travel_distance_m", 0.0) or 0.0),
                start_time=travel_start,
                end_time=travel_finish,
                start_node=travel_from,
                end_node=travel_to,
                start_x=getattr(from_loc, "x", None),
                start_y=getattr(from_loc, "y", None),
                start_floor=getattr(from_loc, "floor", None),
                end_x=getattr(to_loc, "x", None),
                end_y=getattr(to_loc, "y", None),
                end_floor=getattr(to_loc, "floor", None),
                lift_id=str(assignment.get("travel_lift_id", "") or ""),
                status="staff_travel",
                task_source=getattr(task, "task_source", ""),
                department_id=getattr(task, "department_id", ""),
                container_type=getattr(task, "container_type", ""),
                person_resource=f"{category_key}_staff_travel",
                person_id=str(assignment["person_id"]),
                people_required=int(assignment.get("people_required", 0) or 0),
                staff_on_shift_people_required=int(assignment.get("staff_on_shift_people_required", 0) or 0),
                staff_shift_pattern=str(assignment.get("staff_shift_pattern", "") or ""),
                staff_shift_team=str(assignment.get("staff_shift_team", "") or ""),
                staff_shift_multiplier=float(assignment.get("staff_shift_multiplier", 1.0) or 1.0),
                staff_initial_on_shift_people=int(assignment.get("staff_initial_on_shift_people", 0) or 0),
                staff_initial_rostered_people=int(assignment.get("staff_initial_rostered_people", 0) or 0),
            )
        start = float(assignment.get("start_time", 0.0) or 0.0)
        finish = float(assignment.get("finish_time", start) or start)
        self.log_step(
            event_time=start,
            event_type="staff_payload_handling",
            task_id=str(getattr(task, "id", "") or ""),
            amr_id="",
            details=(
                f"{assignment['person_id']} handling {resource_name} payload "
                f"until {self.clock.format_sim_time(finish)}"
            ),
            from_location=location_name,
            to_location=location_name,
            payload_name=self._payload_log_name(getattr(task, "payload", "")),
            payload_instance_id=str(getattr(task, "payload_instance_id", "") or ""),
            duration_sec=float(assignment.get("duration_sec", max(0.0, finish - start)) or 0.0),
            staff_wait_for_travel_sec=float(
                assignment.get("staff_wait_for_travel_sec", 0.0) or 0.0
            ),
            start_time=start,
            end_time=finish,
            start_node=location_name,
            end_node=location_name,
            start_x=getattr(location, "x", None),
            start_y=getattr(location, "y", None),
            start_floor=getattr(location, "floor", None),
            end_x=getattr(location, "x", None),
            end_y=getattr(location, "y", None),
            end_floor=getattr(location, "floor", None),
            status="handling",
            task_source=getattr(task, "task_source", ""),
            department_id=getattr(task, "department_id", ""),
            container_type=getattr(task, "container_type", ""),
            person_resource=f"{category_key}_payload_handling",
            person_id=str(assignment["person_id"]),
            people_required=int(assignment.get("people_required", 0) or 0),
            staff_on_shift_people_required=int(assignment.get("staff_on_shift_people_required", 0) or 0),
            staff_shift_pattern=str(assignment.get("staff_shift_pattern", "") or ""),
            staff_shift_team=str(assignment.get("staff_shift_team", "") or ""),
            staff_shift_multiplier=float(assignment.get("staff_shift_multiplier", 1.0) or 1.0),
            staff_initial_on_shift_people=int(assignment.get("staff_initial_on_shift_people", 0) or 0),
            staff_initial_rostered_people=int(assignment.get("staff_initial_rostered_people", 0) or 0),
        )

    def _schedule_configured_return_task(
        self, task: Task, finish_time: float, amr_id: str = ""
    ) -> None:
        return_enabled = bool(getattr(task, "return_enabled", False))
        staff_required = self._task_staff_handling_config(task) is not None
        if not return_enabled and not staff_required:
            return

        delay_sec = (
            max(0.0, float(getattr(task, "return_delay_minutes", 0.0) or 0.0)) * 60.0
        )
        handling_duration_sec = self._staff_handling_duration_sec(task, delay_sec)
        staff_assignment = self._assign_staff_for_handling(
            task, finish_time, handling_duration_sec
        )
        self._log_staff_handling_assignment(task, staff_assignment)
        if not return_enabled:
            return

        return_payload = str(getattr(task, "return_payload", "") or "").strip()
        if not return_payload:
            return_payload = normalise_payload_name(getattr(task, "payload", ""))
        if not return_payload or return_payload not in self.payloads:
            return

        self.synthetic_task_counter += 1
        return_release_time = finish_time + delay_sec
        if staff_assignment is not None:
            return_release_time = max(
                return_release_time, float(staff_assignment.get("finish_time", finish_time) or finish_time)
            )
        return_task = Task(
            id=f"RETURN-{task.id}-{self.synthetic_task_counter}",
            pickup=task.dropoff,
            dropoff=task.pickup,
            payload=return_payload,
            release_time=return_release_time,
            target_time=0.0,
            quantity=1,
            priority=int(
                getattr(task, "return_priority", 0) or getattr(task, "priority", 100)
            ),
            created_during_runtime=True,
            labels=list(getattr(task, "labels", []) or []) + ["return"],
            route_profile=(
                str(getattr(task, "return_route_profile", "") or "").strip() or None
            ),
            task_source="task_generation_return",
            department_id=str(getattr(task, "department_id", "") or ""),
            waste_stream=str(getattr(task, "waste_stream", "") or ""),
            container_type=return_payload,
            payload_instance_id=(
                str(getattr(task, "payload_instance_id", "") or "").strip()
                if bool(getattr(task, "return_same_payload_instance", False))
                else
                # For normal returns, carry the same physical object that was
                # just delivered to the department.  Creating a fresh instance
                # here means the later return pickup cannot remove the delivered
                # trolley/bin from the department store, so occupancy accumulates
                # by number of scheduled visits instead of simultaneous payloads.
                (
                    str(getattr(task, "payload_instance_id", "") or "").strip()
                    if (
                        not str(getattr(task, "waste_stream", "") or "").strip()
                        and not self._location_has_inventory_mass_collection_rotation(
                            task.dropoff, return_payload
                        )
                        and (
                            normalise_payload_name(getattr(task, "payload", ""))
                            == return_payload
                            or bool(
                                getattr(task, "reusable_return_pool_enabled", False)
                            )
                        )
                    )
                    else (
                        str(
                            getattr(task, "exchange_empty_payload_instance_id", "")
                            or ""
                        ).strip()
                        or (
                            ""
                            if self._location_has_inventory_mass_collection_rotation(
                                task.dropoff, return_payload
                            )
                            else self.payload_instance_store.make_instance_id(
                                return_payload, f"{task.id}-empty-return"
                            )
                        )
                    )
                )
            ),
            is_return_task=True,
        )
        # Dynamic waste/shared-bin metadata must follow the return task so the
        # payload instance still knows which shared physical container group it
        # belongs to after it is returned to the department/shared waste room.
        return_task.container_group = str(getattr(task, "container_group", "") or "")
        return_task.shared_container_group = str(
            getattr(task, "shared_container_group", "") or ""
        )
        return_task.shared_container = bool(getattr(task, "shared_container", False))
        return_task.initial_container_present = bool(
            getattr(task, "initial_container_present", True)
        )
        return_task.generator_volume_key = str(
            getattr(task, "generator_volume_key", "") or ""
        )
        return_task.generator_threshold_volume_m3 = float(
            getattr(task, "generator_threshold_volume_m3", 0.0) or 0.0
        )
        return_task.generator_collection_task_id = str(
            getattr(task, "generator_collection_task_id", task.id) or task.id
        )
        return_task.generator_waits_for_return = bool(
            getattr(task, "generator_waits_for_return", False)
        )
        return_task.returns_same_payload_instance = bool(
            getattr(task, "return_same_payload_instance", False)
        )
        staged_empty_id = str(
            getattr(task, "exchange_empty_payload_instance_id", "") or ""
        ).strip()
        same_physical_return_id = str(
            getattr(task, "payload_instance_id", "") or ""
        ).strip()
        same_physical_return = bool(
            same_physical_return_id
            and str(getattr(return_task, "payload_instance_id", "") or "").strip()
            == same_physical_return_id
            and normalise_payload_name(getattr(task, "payload", "")) == return_payload
        )
        if bool(getattr(return_task, "returns_same_payload_instance", False)):
            # Seeded waste containers are emptied at the waste destination and
            # immediately returned as the same physical bin.  They are not drawn
            # from, or stowed into, the finite mass-collection empty-bin store.
            return_task.requires_existing_payload_instance = False
            return_task.creates_new_payload_instance = True
        elif staged_empty_id:
            # The outbound full-bin arrival has already exchanged with a real empty
            # bin at the store.  The return leg carries that staged empty instance.
            return_task.requires_existing_payload_instance = False
            return_task.creates_new_payload_instance = True
            return_task.payload_instance_metadata = dict(
                getattr(task, "exchange_empty_payload_metadata", {}) or {}
            )
        elif self._location_has_inventory_mass_collection_rotation(
            return_task.pickup, return_payload
        ):
            # Finite bin stores must provide a real empty bin from inventory.
            # If none is available, this return fails rather than remaining pending.
            return_task.requires_existing_payload_instance = True
            return_task.creates_new_payload_instance = False
        else:
            if same_physical_return:
                # Normal trolley return: collect the physical object already stored
                # at the department/location.  This is what keeps occupancy as a
                # simultaneous count instead of a cumulative delivery count.
                return_task.requires_existing_payload_instance = True
                return_task.creates_new_payload_instance = False
            else:
                # No finite store spaces: model the store as having unlimited empty
                # equivalents, while mass collection still removes the full bins only.
                return_task.requires_existing_payload_instance = False
                return_task.creates_new_payload_instance = True
        return_task.exchanged_full_payload_instance_id = str(
            getattr(task, "payload_instance_id", "") or ""
        )
        # A physical bin return is a continuation of the collection/swap cycle.
        # Keep it on the same AMR so the simulator does not model one AMR
        # collecting the full bin while another AMR independently delivers the
        # empty/returned bin.
        return_task.locked_amr_id = str(amr_id or "").strip()
        if return_task.locked_amr_id:
            self._remove_pending_idle_return_tasks_for_amr(return_task.locked_amr_id)

        # If the bin is ready to return immediately, put the physical-bin return
        # straight into the pending queue.  Pushing a same-time task_release event
        # leaves the pending queue briefly empty, which lets the idle-return
        # scheduler insert an empty AMR-centre trip ahead of the real bin return.
        if return_task.release_time <= max(self.current_time, finish_time) + 1e-9:
            self._prepare_task_payload_instance(return_task)
            self._queue_pending_task(return_task)
        else:
            self.schedule_task_release(return_task)

        pickup = self.locations.get(return_task.pickup)
        dropoff = self.locations.get(return_task.dropoff)
        self.log_step(
            event_time=return_task.release_time,
            event_type="return_task_generated",
            task_id=return_task.id,
            details=f"Generated delayed return for {task.id}",
            from_location=return_task.pickup,
            to_location=return_task.dropoff,
            payload_name=self._payload_log_name(return_task.payload),
            payload_instance_id=getattr(return_task, "payload_instance_id", ""),
            duration_sec=0.0,
            wait_time_sec=0.0,
            distance_m=0.0,
            start_time=return_task.release_time,
            end_time=return_task.release_time,
            start_node=return_task.pickup,
            end_node=return_task.dropoff,
            start_x=getattr(pickup, "x", None),
            start_y=getattr(pickup, "y", None),
            start_floor=getattr(pickup, "floor", None),
            end_x=getattr(dropoff, "x", None),
            end_y=getattr(dropoff, "y", None),
            end_floor=getattr(dropoff, "floor", None),
            status="generated",
            energy_kwh=0.0,
            task_source=return_task.task_source,
            department_id=return_task.department_id,
            container_type=return_payload,
            person_resource=(
                f"{staff_assignment.get('category_key', '')}_payload_handling"
                if staff_assignment is not None
                else ""
            ),
            person_id=(
                str(staff_assignment["person_id"])
                if staff_assignment is not None
                else ""
            ),
            people_required=(
                int(staff_assignment["people_required"])
                if staff_assignment is not None
                else 0
            ),
            staff_on_shift_people_required=(
                int(staff_assignment.get("staff_on_shift_people_required", 0) or 0)
                if staff_assignment is not None
                else 0
            ),
            staff_shift_pattern=(
                str(staff_assignment.get("staff_shift_pattern", "") or "")
                if staff_assignment is not None
                else ""
            ),
            staff_shift_team=(
                str(staff_assignment.get("staff_shift_team", "") or "")
                if staff_assignment is not None
                else ""
            ),
            staff_shift_multiplier=(
                float(staff_assignment.get("staff_shift_multiplier", 1.0) or 1.0)
                if staff_assignment is not None
                else 1.0
            ),
            staff_initial_on_shift_people=(
                int(staff_assignment.get("staff_initial_on_shift_people", 0) or 0)
                if staff_assignment is not None
                else 0
            ),
            staff_initial_rostered_people=(
                int(staff_assignment.get("staff_initial_rostered_people", 0) or 0)
                if staff_assignment is not None
                else 0
            ),
        )

    def _notify_task_generation_state(self, task: Task, state: str) -> None:
        manager = getattr(self, "task_generation_manager", None)
        if manager is not None:
            manager.task_state_changed(task, state)

    def _release_payload_instance_reservation_for_task(self, task: Task) -> None:
        instance_id = str(getattr(task, "payload_instance_id", "") or "").strip()
        if instance_id:
            self.payload_instance_store.release_reservation(
                instance_id, str(getattr(task, "id", "") or "")
            )

    def _fail_task(self, task: Task, reason: str, now: Optional[float] = None) -> None:
        reason = str(reason or "Task failed").strip()
        self._set_task_pending_reason(task, reason)
        self._remove_pending_task(task)
        self._clear_inventory_space_reservation_for_task(task)

        task_id = str(getattr(task, "id", "")).strip()
        if task_id in self.failed_task_ids:
            return

        self.failed_task_ids.add(task_id)
        self._release_payload_instance_reservation_for_task(task)
        self._notify_task_generation_state(task, "failed")
        self.failed_tasks.append({"task_id": task.id, "reason": reason})
        event_time = self.current_time if now is None else now
        self.log_step(
            event_time=event_time,
            event_type="task_failed",
            task_id=task.id,
            details=reason,
            from_location=task.pickup,
            to_location=task.dropoff,
            payload_name=self._payload_log_name(task.payload),
            payload_instance_id=getattr(task, "payload_instance_id", ""),
            start_time=event_time,
            end_time=event_time,
            status="failed",
            task_source=getattr(task, "task_source", ""),
            department_id=getattr(task, "department_id", ""),
            waste_stream=getattr(task, "waste_stream", ""),
            waste_volume_m3=getattr(task, "waste_volume_m3", 0.0),
            container_type=getattr(task, "container_type", ""),
            pending_reason=reason,
        )

    def _rules_cache_key(
        self, rules: Optional[dict]
    ) -> Tuple[Tuple[str, ...], Tuple[str, ...], Tuple[Tuple[str, str], ...]]:
        if not rules:
            return ((), (), ())
        allowed_lifts = tuple(sorted(rules.get("allowed_lifts", set())))
        allowed_nodes = tuple(sorted(rules.get("allowed_nodes", set())))
        allowed_edges = tuple(sorted(rules.get("allowed_edges", set())))
        return (allowed_lifts, allowed_nodes, allowed_edges)

    def _empty_route_rules(self) -> dict:
        return {
            "allowed_lifts": set(),
            "allowed_nodes": set(),
            "allowed_edges": set(),
        }

    def _edge_key(self, a_name: str, b_name: str) -> Tuple[str, str]:
        return (a_name, b_name)

    def _resolve_task_route_rules(self, task: Task) -> dict:
        rules = self._empty_route_rules()

        profile_name = getattr(task, "route_profile", None)
        if not profile_name and "dirty" in getattr(task, "labels", []):
            profile_name = "dirty"

        if profile_name:
            profile = self.route_profiles.get(profile_name, {})
            rules["allowed_lifts"].update(profile.get("allowed_lifts", []))
            rules["allowed_nodes"].update(profile.get("allowed_nodes", []))
            rules["allowed_edges"].update(
                self._edge_key(a, b) for a, b in profile.get("allowed_edges", [])
            )

        rules["allowed_lifts"].update(getattr(task, "allowed_lifts", []))
        rules["allowed_nodes"].update(getattr(task, "allowed_nodes", []))
        rules["allowed_edges"].update(
            self._edge_key(a, b) for a, b in getattr(task, "allowed_edges", [])
        )

        return rules

    def _node_allowed(self, node_name: str, rules: Optional[dict]) -> bool:
        if not rules:
            return True
        allowed_nodes = rules.get("allowed_nodes", set())
        if not allowed_nodes:
            return True
        return node_name in allowed_nodes

    def _edge_allowed(
        self, from_name: str, to_name: str, rules: Optional[dict]
    ) -> bool:
        if not rules:
            return True
        allowed_edges = rules.get("allowed_edges", set())
        if not allowed_edges:
            return True
        return (from_name, to_name) in allowed_edges

    def _lift_allowed(self, lift: Lift, rules: Optional[dict]) -> bool:
        if not rules:
            return True
        allowed_lifts = rules.get("allowed_lifts", set())
        if not allowed_lifts:
            return True
        return lift.id in allowed_lifts

    def _build_floor_graphs(self, corridor_cfg: dict):
        for location in self.locations.values():
            self.graph_nodes[location.name] = location
            self.floor_graphs[location.floor][location.name]
            self.floor_reverse_graphs[location.floor][location.name]

        for lift in self.lifts:
            for floor in lift.served_floors:
                node = lift.location_on_floor(floor)
                self.graph_nodes[node.name] = node
                self.floor_graphs[floor][node.name]
                self.floor_reverse_graphs[floor][node.name]

        for node_data in corridor_cfg.get("nodes", []):
            node = Location(
                name=node_data["name"],
                floor=int(node_data["floor"]),
                x=float(node_data["x"]),
                y=float(node_data["y"]),
                has_door=bool(node_data.get("has_door", False)),
                door_clear_width_m=max(
                    0.1,
                    float(
                        node_data.get(
                            "door_clear_width_m", self.default_door_clear_width_m
                        )
                        or self.default_door_clear_width_m
                    ),
                ),
            )
            self.graph_nodes[node.name] = node
            self.floor_graphs[node.floor][node.name]
            self.floor_reverse_graphs[node.floor][node.name]

        def add_directed_edge(
            a_name: str,
            b_name: str,
            distance_m: float,
            *,
            bidirectional: bool = True,
            width_m: Optional[float] = None,
            people_area_type: str = "none",
            people_profile_ids: Optional[List[str]] = None,
        ):
            a = self.graph_nodes[a_name]
            b = self.graph_nodes[b_name]
            configured_width = max(
                0.1, float(width_m or self.default_corridor_width_m)
            )
            effective_width = configured_width
            door_nodes = []
            for node in (a, b):
                if bool(getattr(node, "has_door", False)):
                    opening = max(
                        0.1,
                        float(
                            getattr(
                                node,
                                "door_clear_width_m",
                                self.default_door_clear_width_m,
                            )
                            or self.default_door_clear_width_m
                        ),
                    )
                    effective_width = min(effective_width, opening)
                    door_nodes.append(node.name)
            profile_ids = [
                str(x).strip()
                for x in (people_profile_ids or [])
                if str(x).strip()
            ]
            edge_data = {
                "to": b_name,
                "distance_m": distance_m,
                "bidirectional": bool(bidirectional),
                "configured_width_m": configured_width,
                "width_m": effective_width,
                "door_restricted": effective_width < configured_width - 1e-9,
                "door_nodes": door_nodes,
                "people_area_type": str(people_area_type or "none").strip().lower(),
                "people_profile_ids": profile_ids,
            }
            self.floor_graphs[a.floor][a_name].append(edge_data)
            reverse_data = dict(edge_data)
            reverse_data["to"] = a_name
            self.floor_reverse_graphs[a.floor][b_name].append(reverse_data)

        def add_edge(
            a_name: str,
            b_name: str,
            distance_m: Optional[float] = None,
            bidirectional: bool = True,
            width_m: Optional[float] = None,
            people_area_type: str = "none",
            people_profile_ids: Optional[List[str]] = None,
        ):
            if a_name not in self.graph_nodes or b_name not in self.graph_nodes:
                raise ValueError(
                    f"Corridor edge references unknown node: {a_name} -> {b_name}"
                )
            a = self.graph_nodes[a_name]
            b = self.graph_nodes[b_name]
            if a.floor != b.floor:
                raise ValueError(
                    f"Same-floor graph edge crosses floors: {a_name} -> {b_name}"
                )
            dist = (
                distance_m
                if distance_m is not None
                else self._distance_same_floor(a, b)
            )
            add_directed_edge(
                a_name,
                b_name,
                dist,
                bidirectional=bidirectional,
                width_m=width_m,
                people_area_type=people_area_type,
                people_profile_ids=people_profile_ids,
            )
            if bidirectional:
                add_directed_edge(
                    b_name,
                    a_name,
                    dist,
                    bidirectional=bidirectional,
                    width_m=width_m,
                    people_area_type=people_area_type,
                    people_profile_ids=people_profile_ids,
                )

        for edge in corridor_cfg.get("edges", []):
            add_edge(
                edge["from"],
                edge["to"],
                edge.get("distance_m"),
                edge.get("bidirectional", True),
                edge.get("width_m", self.default_corridor_width_m),
                edge.get("people_area_type", "none"),
                edge.get("people_profile_ids", []),
            )

        # Optional: connect locations/lifts to nearby graph nodes when explicit edges are not supplied
        auto_connect = corridor_cfg.get("auto_connect", True)
        if auto_connect:
            for floor, nodes in self.floor_graphs.items():
                existing_names = list(nodes.keys())
                corridor_names = [
                    name
                    for name in existing_names
                    if name not in self.locations
                    and not name.startswith(tuple(l.id + "-F" for l in self.lifts))
                ]
                if not corridor_names:
                    continue
                for loc in self.locations.values():
                    if loc.floor != floor:
                        continue
                    if nodes[loc.name]:
                        continue
                    nearest = min(
                        corridor_names,
                        key=lambda n: self._distance_same_floor(
                            loc, self.graph_nodes[n]
                        ),
                    )
                    add_edge(loc.name, nearest)
                for lift in self.lifts:
                    lift_name = f"{lift.id}-F{floor}"
                    if lift_name not in nodes or nodes[lift_name]:
                        continue
                    lift_node = self.graph_nodes[lift_name]
                    nearest = min(
                        corridor_names,
                        key=lambda n: self._distance_same_floor(
                            lift_node, self.graph_nodes[n]
                        ),
                    )
                    add_edge(lift_name, nearest)

    def _scenario_resource_matches(self, resource_type: str, resource_id: str, candidate: str) -> bool:
        resource_type = str(resource_type or "").strip().lower()
        resource_id = str(resource_id or "").strip()
        candidate = str(candidate or "").strip()
        if resource_type == "corridor":
            normalise = lambda value: " -> ".join(x.strip() for x in value.replace("<->", "->").split("->") if x.strip())
            wanted = normalise(resource_id)
            actual = normalise(candidate)
            if wanted == actual:
                return True
            try:
                wa, wb = [x.strip() for x in wanted.split("->")]
                aa, ab = [x.strip() for x in actual.split("->")]
                return {wa, wb} == {aa, ab}
            except Exception:
                return False
        if resource_type == "amr":
            return candidate == resource_id or candidate.startswith(resource_id + "-")
        return candidate == resource_id

    def _scenario_event_state(self, resource_type: str, candidate: str, sim_time_sec: float) -> dict:
        state = {"availability_percent": 100.0, "speed_factor": 1.0, "blocked_until": float(sim_time_sec), "notes": ""}
        if not self.scenario_mode:
            return state
        dt = self.clock.sim_seconds_to_datetime(float(sim_time_sec))
        day_key = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][dt.weekday()]
        minute = dt.hour * 60 + dt.minute + dt.second / 60.0
        for event in self.scenario_events:
            if str(event.get("resource_type", "") or "").strip().lower() != resource_type:
                continue
            resource_ids = event.get("resource_ids", [])
            if isinstance(resource_ids, str):
                resource_ids = [x.strip() for x in resource_ids.split(",") if x.strip()]
            if not resource_ids and event.get("resource_id"):
                resource_ids = [event.get("resource_id")]
            if not any(
                self._scenario_resource_matches(resource_type, resource_id, candidate)
                for resource_id in resource_ids
            ):
                continue
            days = {str(x).strip().lower()[:3] for x in (event.get("days_active", []) or []) if str(x).strip()}
            if days and day_key not in days:
                continue
            start_min = self._parse_hhmm_to_minutes(event.get("start_time"), 0)
            end_min = self._parse_hhmm_to_minutes(event.get("end_time"), 24 * 60)
            if end_min == start_min:
                active = True
            elif end_min > start_min:
                active = start_min <= minute < end_min
            else:
                active = minute >= start_min or minute < end_min
            if not active:
                continue
            availability = max(0.0, min(100.0, float(event.get("availability_percent", 100.0) or 0.0)))
            speed_factor = max(0.0, min(1.0, float(event.get("speed_factor", 1.0) or 0.0)))
            state["availability_percent"] = min(state["availability_percent"], availability)
            state["speed_factor"] = min(state["speed_factor"], speed_factor)
            state["notes"] = str(event.get("notes", "") or state["notes"])
            if availability <= 0.0:
                if end_min > start_min:
                    seconds_to_end = max(0.0, (end_min - minute) * 60.0)
                elif minute >= start_min:
                    seconds_to_end = max(0.0, ((24 * 60 - minute) + end_min) * 60.0)
                else:
                    seconds_to_end = max(0.0, (end_min - minute) * 60.0)
                state["blocked_until"] = max(state["blocked_until"], float(sim_time_sec) + seconds_to_end)
        return state

    def _lift_health_speed_factor(self, lift: Lift) -> float:
        health = max(0.0, min(100.0, float(getattr(lift, "health_percent", 100.0) or 0.0)))
        minimum = max(0.0, min(100.0, float(getattr(lift, "minimum_operational_health_percent", 20.0) or 0.0)))
        if health < minimum:
            return 0.0
        at_zero = max(0.05, min(1.0, float(getattr(lift, "health_speed_penalty_at_zero", 0.5) or 0.5)))
        return at_zero + (1.0 - at_zero) * (health / 100.0)

    def _resource_speed_factor(self, resource_type: str, resource_id: str, sim_time_sec: float) -> Tuple[float, float, str]:
        state = self._scenario_event_state(resource_type, resource_id, sim_time_sec)
        availability_factor = max(0.0, min(1.0, float(state["availability_percent"]) / 100.0))
        speed_factor = min(float(state["speed_factor"]), availability_factor if availability_factor > 0 else 0.0)
        return speed_factor, float(state["blocked_until"]), str(state.get("notes", ""))

    def _shortest_people_path_same_floor(
        self,
        floor: int,
        start_name: str,
        end_name: str,
        group_type: str,
        profile_id: str = "",
    ) -> Optional[dict]:
        graph = self.floor_graphs.get(floor, {})
        if start_name not in graph or end_name not in graph:
            return None
        group_type = str(group_type or "staff").strip().lower()
        heap = [(0.0, start_name)]
        best = {start_name: 0.0}
        previous = {}
        while heap:
            distance, node = heapq.heappop(heap)
            if distance > best.get(node, math.inf):
                continue
            if node == end_name:
                break
            for edge in graph.get(node, []):
                area = str(edge.get("people_area_type", "none") or "none").strip().lower()
                if group_type == "both":
                    allowed_areas = {"none", "both", "staff", "public"}
                else:
                    allowed_areas = {"none", "both", group_type}
                if area not in allowed_areas:
                    continue
                assigned_profiles = {
                    str(x).strip()
                    for x in (edge.get("people_profile_ids", []) or [])
                    if str(x).strip()
                }
                if assigned_profiles and profile_id and profile_id not in assigned_profiles:
                    continue
                nxt = edge["to"]
                new_distance = distance + float(edge.get("distance_m", 0.0) or 0.0)
                if new_distance < best.get(nxt, math.inf):
                    best[nxt] = new_distance
                    previous[nxt] = (node, dict(edge))
                    heapq.heappush(heap, (new_distance, nxt))
        if end_name not in best:
            return None
        edges = []
        node = end_name
        while node != start_name:
            parent, edge = previous[node]
            item = dict(edge)
            item.update({"from": parent, "to": node})
            edges.append(item)
            node = parent
        edges.reverse()
        return {"distance_m": best[end_name], "edges": edges}

    def _people_route_edges(
        self,
        start: Location,
        end: Location,
        group_type: str = "staff",
        profile_id: str = "",
    ) -> List[dict]:
        if start.floor == end.floor:
            route = self._shortest_people_path_same_floor(
                start.floor, start.name, end.name, group_type, profile_id
            )
            return list(route.get("edges", [])) if route else []
        best = None
        best_distance = math.inf
        for lift in self.lifts:
            if not lift.can_serve(start.floor, end.floor):
                continue
            origin = lift.location_on_floor(start.floor)
            destination = lift.location_on_floor(end.floor)
            first = self._shortest_people_path_same_floor(
                start.floor, start.name, origin.name, group_type, profile_id
            )
            second = self._shortest_people_path_same_floor(
                end.floor, destination.name, end.name, group_type, profile_id
            )
            if first is None or second is None:
                continue
            distance = float(first["distance_m"]) + float(second["distance_m"])
            if distance < best_distance:
                best_distance = distance
                best = list(first["edges"]) + list(second["edges"])
        return best or []

    @staticmethod
    def _people_profile_count(profile: dict, minute_of_window: float, window_minutes: float) -> int:
        """Return the people represented by one profile interval.

        Legacy profiles remain constant.  Ramp profiles interpolate between the
        configured minimum and peak counts within the daily timeframe.
        """
        peak = max(1, int(profile.get("people_per_trip", 1) or 1))
        minimum = max(
            0,
            min(peak, int(profile.get("minimum_people_per_interval", 0) or 0)),
        )
        shape = str(profile.get("profile_shape", "constant") or "constant").strip().lower()
        elapsed = max(0.0, float(minute_of_window))
        total = max(0.1, float(window_minutes))
        remaining = max(0.0, total - elapsed)
        ramp_up = max(0.0, float(profile.get("ramp_up_minutes", 0.0) or 0.0))
        ramp_down = max(0.0, float(profile.get("ramp_down_minutes", 0.0) or 0.0))
        if bool(profile.get("fit_profile_to_timeframe", False)):
            if shape == "ramp_up":
                ramp_up, ramp_down = total, 0.0
            elif shape == "ramp_down":
                ramp_up, ramp_down = 0.0, total
            elif shape == "ramp_up_down":
                ramp_up = ramp_down = total / 2.0

        if shape == "ramp_up":
            factor = 1.0 if ramp_up <= 0.0 else min(1.0, elapsed / ramp_up)
        elif shape == "ramp_down":
            factor = 1.0 if ramp_down <= 0.0 else min(1.0, remaining / ramp_down)
        elif shape == "ramp_up_down":
            up_factor = 1.0 if ramp_up <= 0.0 else min(1.0, elapsed / ramp_up)
            down_factor = 1.0 if ramp_down <= 0.0 else min(1.0, remaining / ramp_down)
            factor = min(up_factor, down_factor)
        else:
            factor = 1.0

        return max(0, int(round(minimum + (peak - minimum) * max(0.0, min(1.0, factor)))))

    def _init_people_movements(self, raw_movements) -> None:
        self.people_movements = []
        if not isinstance(raw_movements, list):
            return

        def resolve_corridor_assets(values) -> List[dict]:
            resolved = []
            seen = set()
            for value in values or []:
                text = str(value or "").strip().replace("<->", "->")
                parts = [x.strip() for x in text.split("->") if x.strip()]
                if len(parts) < 2:
                    continue
                a_name, b_name = parts[0], parts[1]
                key = self._physical_edge_key(a_name, b_name)
                if key in seen:
                    continue
                found = None
                for floor_graph in self.floor_graphs.values():
                    for edge in floor_graph.get(a_name, []):
                        if edge.get("to") == b_name:
                            found = dict(edge)
                            found.update({"from": a_name, "to": b_name})
                            break
                    if found is not None:
                        break
                    for edge in floor_graph.get(b_name, []):
                        if edge.get("to") == a_name:
                            found = dict(edge)
                            found.update({"from": b_name, "to": a_name})
                            break
                    if found is not None:
                        break
                if found is not None:
                    seen.add(key)
                    resolved.append(found)
            return resolved

        horizon = max(
            0.0,
            float(getattr(self, "task_generation_horizon_sec", 0.0) or 0.0),
        )
        day_count = max(1, int(math.ceil(horizon / 86400.0)) + 2)
        for index, raw in enumerate(raw_movements, start=1):
            if not isinstance(raw, dict) or not bool(raw.get("enabled", True)):
                continue
            profile_id = str(raw.get("id", f"PEOPLE-{index}") or f"PEOPLE-{index}")
            group_type = str(raw.get("group_type", "staff") or "staff").strip().lower()
            if group_type == "mixed":
                group_type = "both"
            if group_type not in {"staff", "public", "both"}:
                group_type = "staff"

            explicit_edges = resolve_corridor_assets(raw.get("corridor_edges", []) or [])
            direct_corridor_profile = bool(explicit_edges)
            if direct_corridor_profile:
                edges = explicit_edges
            else:
                start = self.locations.get(
                    str(raw.get("start_location", "") or "").strip()
                )
                end = self.locations.get(
                    str(raw.get("end_location", "") or "").strip()
                )
                if start is None or end is None:
                    continue
                edges = self._people_route_edges(
                    start, end, group_type, profile_id=profile_id
                )
            if not edges:
                continue

            peak_people = max(1, int(float(raw.get("people_per_trip", 1) or 1)))
            profile_shape = str(raw.get("profile_shape", "constant") or "constant").strip().lower()
            if profile_shape not in {"constant", "ramp_up", "ramp_down", "ramp_up_down"}:
                profile_shape = "constant"
            profile = {
                "id": profile_id,
                "group_type": group_type,
                "people_per_trip": peak_people,
                "minimum_people_per_interval": max(
                    0,
                    min(
                        peak_people,
                        int(float(raw.get("minimum_people_per_interval", 0) or 0)),
                    ),
                ),
                "profile_shape": profile_shape,
                "timeframe_name": str(raw.get("timeframe_name", "") or "").strip(),
                "timeframe_preset": str(raw.get("timeframe_preset", "custom") or "custom").strip().lower(),
                "ramp_up_minutes": max(0.0, float(raw.get("ramp_up_minutes", 60.0) or 0.0)),
                "ramp_down_minutes": max(0.0, float(raw.get("ramp_down_minutes", 60.0) or 0.0)),
                "fit_profile_to_timeframe": bool(raw.get("fit_profile_to_timeframe", False)),
                "walking_speed_m_per_sec": max(
                    0.1,
                    float(raw.get("walking_speed_m_per_sec", 1.2) or 1.2),
                ),
                "amr_speed_factor": max(
                    0.05,
                    min(
                        1.0,
                        float(raw.get("amr_speed_factor", 0.7) or 0.7),
                    ),
                ),
                "days_active": {
                    str(x).strip().lower()[:3]
                    for x in (raw.get("days_active", []) or [])
                    if str(x).strip()
                },
                "start_time": str(raw.get("start_time", "08:00") or "08:00"),
                "end_time": str(raw.get("end_time", "18:00") or "18:00"),
                "interval_minutes": max(
                    0.1, float(raw.get("interval_minutes", 15.0) or 15.0)
                ),
                "edges": edges,
                "direct_corridor_profile": direct_corridor_profile,
            }
            self.people_movements.append(profile)
            start_min = self._parse_hhmm_to_minutes(profile["start_time"], 0)
            end_min = self._parse_hhmm_to_minutes(
                profile["end_time"], 24 * 60
            )
            for day_index in range(day_count):
                day_dt = (
                    self.clock.start_datetime + timedelta(days=day_index)
                ).replace(hour=0, minute=0, second=0, microsecond=0)
                day_key = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][
                    day_dt.weekday()
                ]
                if profile["days_active"] and day_key not in profile["days_active"]:
                    continue
                day_start_sec = (day_dt - self.clock.start_datetime).total_seconds()
                window_end = end_min if end_min > start_min else end_min + 24 * 60
                window_minutes = max(0.1, float(window_end - start_min))
                profile_span_minutes = window_minutes
                if bool(profile.get("fit_profile_to_timeframe", False)):
                    # The end of a timeframe is exclusive. Fit the ramp to the
                    # first and last generated intervals so the configured peak
                    # or quiet value is actually represented by a movement.
                    profile_span_minutes = max(
                        0.1,
                        window_minutes - float(profile["interval_minutes"]),
                    )
                departure_min = float(start_min)
                while departure_min < float(window_end) - 1e-9:
                    profile_count = self._people_profile_count(
                        profile,
                        departure_min - float(start_min),
                        profile_span_minutes,
                    )
                    departure_time = day_start_sec + departure_min * 60.0
                    sequential_time = departure_time
                    if profile_count <= 0:
                        departure_min += profile["interval_minutes"]
                        continue
                    for edge in edges:
                        duration = (
                            float(edge.get("distance_m", 0.0))
                            / profile["walking_speed_m_per_sec"]
                        )
                        reservation_start = (
                            departure_time
                            if direct_corridor_profile
                            else sequential_time
                        )
                        key = self._physical_edge_key(edge["from"], edge["to"])
                        self.people_edge_reservations[key].append(
                            {
                                "start": reservation_start,
                                "end": reservation_start + duration,
                                "count": profile_count,
                                "speed_factor": profile["amr_speed_factor"],
                                "group_type": profile["group_type"],
                                "movement_id": profile["id"],
                            }
                        )
                        if not direct_corridor_profile:
                            sequential_time += duration
                    departure_min += profile["interval_minutes"]

        for reservations in self.people_edge_reservations.values():
            reservations.sort(key=lambda item: float(item.get("start", 0.0)))

    def _people_on_edge(self, edge_key: Tuple[str, str], start_time: float, duration: float) -> Tuple[int, float, str]:
        count = 0
        factor = 1.0
        groups = set()
        end_time = float(start_time) + max(0.0, float(duration))
        for item in self.people_edge_reservations.get(edge_key, []):
            if float(item["start"]) >= end_time:
                break
            if float(item["end"]) <= float(start_time):
                continue
            count += int(item.get("count", 0) or 0)
            factor = min(factor, float(item.get("speed_factor", 1.0) or 1.0))
            groups.add(str(item.get("group_type", "") or ""))
        if count > 0:
            factor = min(factor, max(self.minimum_people_speed_factor, 1.0 - count * self.people_slowdown_per_person))
        return count, factor, ",".join(sorted(x for x in groups if x))

    def _corridor_lane_capacity(self, edge: dict, amr: AMR, payload: Optional[PayloadType], orientation: str = "lengthways") -> Tuple[int, float, float]:
        width = max(0.1, float(edge.get("width_m", self.default_corridor_width_m) or self.default_corridor_width_m))
        if not bool(edge.get("bidirectional", True)):
            return 1, width, width
        lane_width = width / 2.0
        payload_length = 0.0
        payload_cross_width = 0.0
        if payload is not None and not is_empty_payload_name(payload.name):
            payload_length, payload_cross_width, _ = self._payload_orientation_dimensions(payload, orientation)
        carrying_length = max(float(getattr(amr, "length_m", 0.0) or 0.0), payload_length)
        carrying_width = max(float(getattr(amr, "width_m", 0.0) or 0.0), payload_cross_width)
        if carrying_length > lane_width + 1e-9 or carrying_width > lane_width + 1e-9:
            return 1, lane_width, carrying_length
        return 2, lane_width, carrying_length

    def _static_route_endpoint_names_by_floor(self) -> Dict[int, List[str]]:
        """Return route endpoint nodes worth pre-caching.

        Corridor nodes can be numerous, but route estimates repeatedly ask for
        paths between locations and lift floor nodes.  Pre-caching those endpoint
        pairs removes the first large scheduling spike without exploding the
        cache for every corridor-node pair.
        """
        by_floor: Dict[int, List[str]] = defaultdict(list)
        for name, loc in self.locations.items():
            if name in self.graph_nodes:
                by_floor[loc.floor].append(name)
        for lift in self.lifts:
            for floor in lift.served_floors:
                name = f"{lift.id}-F{floor}"
                if name in self.graph_nodes:
                    by_floor[floor].append(name)
        return {
            floor: sorted(set(names))
            for floor, names in by_floor.items()
            if len(set(names)) > 1
        }

    def _precompute_static_routes(self) -> None:
        if not self.route_precompute_enabled:
            return

        endpoint_names = self._static_route_endpoint_names_by_floor()
        jobs = []
        for floor, names in endpoint_names.items():
            for start_name in names:
                for end_name in names:
                    if start_name != end_name:
                        jobs.append((floor, start_name, end_name))

        if not jobs:
            return
        if (
            self.route_precompute_max_pairs
            and len(jobs) > self.route_precompute_max_pairs
        ):
            return

        def run_job(job):
            floor, start_name, end_name = job
            self._shortest_path_same_floor(floor, start_name, end_name)

        if self.routing_executor is None or len(jobs) < 128:
            for job in jobs:
                run_job(job)
            return

        futures = [self.routing_executor.submit(run_job, job) for job in jobs]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception:
                pass

    def _shortest_path_same_floor(
        self,
        floor: int,
        start_name: str,
        end_name: str,
        rules: Optional[dict] = None,
    ) -> Optional[dict]:
        graph = self.floor_graphs.get(floor, {})
        reverse_graph = self.floor_reverse_graphs.get(floor, {})
        rules = rules or self._empty_route_rules()
        cache_key = (floor, start_name, end_name, *self._rules_cache_key(rules))
        cache_miss = object()

        with self.route_cache_lock:
            cached = self.route_cache.get(cache_key, cache_miss)
        if cached is not cache_miss:
            return cached

        def cache_and_return(value: Optional[dict]) -> Optional[dict]:
            with self.route_cache_lock:
                self.route_cache[cache_key] = value
            return value

        if start_name not in graph or end_name not in graph:
            return cache_and_return(None)
        if not self._node_allowed(start_name, rules):
            return cache_and_return(None)
        if not self._node_allowed(end_name, rules):
            return cache_and_return(None)
        if start_name == end_name:
            return cache_and_return({"distance_m": 0.0, "edges": []})

        # Bidirectional Dijkstra.  The forward search expands from the start and
        # the reverse search expands from the destination over reverse adjacency.
        # This normally touches far fewer corridor nodes than a single-source
        # search on large floors while still supporting directed edges and route
        # profile restrictions.
        f_heap = [(0.0, start_name)]
        b_heap = [(0.0, end_name)]
        f_best = {start_name: 0.0}
        b_best = {end_name: 0.0}
        f_prev: Dict[str, Tuple[str, dict]] = {}
        # b_prev maps a node to the next node on the path towards end_name.
        b_prev: Dict[str, Tuple[str, dict]] = {}
        best_distance = math.inf
        meeting_node = None

        while f_heap and b_heap:
            if f_heap[0][0] + b_heap[0][0] >= best_distance:
                break

            if f_heap[0][0] <= b_heap[0][0]:
                dist, node = heapq.heappop(f_heap)
                if dist > f_best.get(node, math.inf):
                    continue
                if node in b_best:
                    candidate = dist + b_best[node]
                    if candidate < best_distance:
                        best_distance = candidate
                        meeting_node = node
                for edge in graph.get(node, []):
                    nxt = edge["to"]
                    if not self._node_allowed(nxt, rules):
                        continue
                    if not self._edge_allowed(node, nxt, rules):
                        continue
                    new_dist = dist + edge["distance_m"]
                    if new_dist < f_best.get(nxt, math.inf):
                        f_best[nxt] = new_dist
                        f_prev[nxt] = (node, dict(edge))
                        heapq.heappush(f_heap, (new_dist, nxt))
                    if nxt in b_best:
                        candidate = new_dist + b_best[nxt]
                        if candidate < best_distance:
                            best_distance = candidate
                            meeting_node = nxt
            else:
                dist, node = heapq.heappop(b_heap)
                if dist > b_best.get(node, math.inf):
                    continue
                if node in f_best:
                    candidate = dist + f_best[node]
                    if candidate < best_distance:
                        best_distance = candidate
                        meeting_node = node
                for edge in reverse_graph.get(node, []):
                    nxt = edge["to"]
                    # Reverse edge node -> nxt corresponds to original edge nxt -> node.
                    if not self._node_allowed(nxt, rules):
                        continue
                    if not self._edge_allowed(nxt, node, rules):
                        continue
                    new_dist = dist + edge["distance_m"]
                    if new_dist < b_best.get(nxt, math.inf):
                        b_best[nxt] = new_dist
                        b_prev[nxt] = (node, dict(edge))
                        heapq.heappush(b_heap, (new_dist, nxt))
                    if nxt in f_best:
                        candidate = new_dist + f_best[nxt]
                        if candidate < best_distance:
                            best_distance = candidate
                            meeting_node = nxt

        if meeting_node is None:
            return cache_and_return(None)

        path_edges = []
        node = meeting_node
        while node != start_name:
            parent, edge_data = f_prev[node]
            path_edge = dict(edge_data)
            path_edge.update({"from": parent, "to": node})
            path_edges.append(path_edge)
            node = parent
        path_edges.reverse()

        node = meeting_node
        while node != end_name:
            child, edge_data = b_prev[node]
            path_edge = dict(edge_data)
            path_edge.update({"from": node, "to": child})
            path_edges.append(path_edge)
            node = child

        result = {"distance_m": best_distance, "edges": path_edges}
        return cache_and_return(result)

    def _reservation_scan_start(
        self,
        reservations: list,
        requested_time: float,
        spacing: float,
        max_duration: float,
    ) -> int:
        """Return the first interval that can still overlap requested_time.

        Reservation lists are sorted by start time. The maximum interval duration
        for each resource lets us bisect past history without missing a long
        interval that started earlier but is still active.
        """
        if not reservations:
            return 0
        threshold = (
            float(requested_time)
            - max(0.0, float(spacing or 0.0))
            - max(0.0, float(max_duration or 0.0))
        )
        return bisect_left(reservations, (threshold,))

    def _find_next_available_time(
        self,
        location_name: str,
        requested_start: float,
        duration: float,
    ) -> float:
        max_concurrency = self.location_max_concurrency.get(location_name, 999999)
        reservations = self.location_reservations[location_name]
        max_duration = self.location_reservation_max_duration.get(location_name, 0.0)

        t = requested_start
        while True:
            overlap_count = 0
            next_candidate = None
            scan_start = self._reservation_scan_start(
                reservations, t, 0.0, max_duration
            )

            for index in range(scan_start, len(reservations)):
                start, end = reservations[index]
                if start >= t + duration:
                    break
                if not (t + duration <= start or t >= end):
                    overlap_count += 1
                    if next_candidate is None or end < next_candidate:
                        next_candidate = end

            if overlap_count < max_concurrency:
                return t

            if next_candidate is None:
                return t

            t = next_candidate

    def _reserve_location(self, location_name: str, start_time: float, end_time: float):
        reservations = self.location_reservations[location_name]
        item = (start_time, end_time)
        insort_right(reservations, item)
        self.location_reservation_max_duration[location_name] = max(
            self.location_reservation_max_duration.get(location_name, 0.0),
            max(0.0, float(end_time) - float(start_time)),
        )

    def push_event(
        self, time_value: float, event_type: str, payload: Optional[dict] = None
    ):
        self.event_counter += 1
        heapq.heappush(
            self.events,
            Event(
                time=time_value,
                priority=self.event_counter,
                event_type=event_type,
                payload=payload or {},
            ),
        )
        self._mark_task_activity_changed()
        # self.log_step(
        #     event_time=time_value,
        #     event_type="event_scheduled",
        #     details=f"Scheduled event '{event_type}'",
        # )

    def schedule_task_release(self, task: Task):
        self._prepare_task_payload_instance(task)
        self.push_event(task.release_time, "task_release", {"task": task})

    def add_runtime_task(self, task_dict: dict):
        task_data = dict(task_dict)
        task_data["release_time"] = parse_release_time(
            task_data, self.clock.start_datetime
        )
        task_data.pop("release_datetime", None)
        task = Task(**task_data)
        task.created_during_runtime = True
        self._prepare_task_payload_instance(task)
        with self.lock:
            if task.release_time <= self.current_time:
                self._queue_pending_task(task)
                self._try_assign_tasks(self.current_time)
            else:
                self.schedule_task_release(task)

    def _department_id_from_config(self, dept: dict) -> str:
        return str(dept.get("id", "")).strip() or str(dept.get("name", "")).strip()

    def _normalise_department_waste_stream_items_for_seed(
        self, dept: dict
    ) -> List[dict]:
        result = []
        seen = set()
        for raw in dept.get("waste_streams", []) or []:
            if isinstance(raw, dict):
                item = dict(raw)
                name = str(item.get("name", "")).strip()
            else:
                name = str(raw).strip()
                item = {"name": name}
            if not name or name in seen or name not in self.waste_streams:
                continue
            item["name"] = name
            result.append(item)
            seen.add(name)
        return result

    def _department_waste_pickup_locations_for_seed(self, dept: dict) -> List[str]:
        locations = []
        category_locations = dept.get("task_generation_locations", {}) or {}
        waste_entry = category_locations.get("waste", {}) or {}
        if isinstance(waste_entry, dict):
            locations.extend(waste_entry.get("pickup_dropoff_locations", []) or [])
            locations.extend(waste_entry.get("locations", []) or [])
        elif isinstance(waste_entry, list):
            locations.extend(waste_entry)

        waste_cfg = dept.get("waste", {}) or {}
        if isinstance(waste_cfg, dict):
            if waste_cfg.get("pickup_location"):
                locations.append(waste_cfg.get("pickup_location"))
            locations.extend(waste_cfg.get("pickup_locations", []) or [])

        locations.extend(dept.get("waste_pickup_locations", []) or [])

        clean = []
        seen = set()
        for name in locations:
            text = str(name or "").strip()
            if text and text in self.locations and text not in seen:
                clean.append(text)
                seen.add(text)
        return clean

    def _waste_container_group_key_for_seed(
        self, dept_id: str, stream_name: str, stream_item: dict, pickup_location: str
    ) -> str:
        explicit = str(
            stream_item.get(
                "shared_container_group", stream_item.get("shared_container_id", "")
            )
            or ""
        ).strip()
        if explicit:
            return f"shared:{explicit}"
        if bool(stream_item.get("shared_container", False)):
            return f"pickup:{stream_name}:{pickup_location}"
        return f"department:{dept_id}:{stream_name}:{pickup_location}"

    def _claim_initial_inventory_space(
        self, location_name: str, payload: PayloadType, task_id: str
    ) -> Optional[dict]:
        if not self._location_has_payload_inventory_spaces(location_name):
            return None
        space = self._find_free_inventory_space(location_name, payload)
        if space is None:
            return None
        space["occupied"] = True
        space["payload"] = payload.name
        space["task_id"] = task_id
        space["reserved_by_task"] = ""
        return space

    def _seed_initial_waste_stream_containers(self) -> None:
        if not getattr(self, "seed_waste_stream_containers_at_start", False):
            return
        if not self.waste_streams or not self.departments:
            return

        seeded_groups = set()
        for dept in self.departments:
            if not bool(dept.get("enabled", True)):
                continue
            dept_id = self._department_id_from_config(dept)
            if not dept_id:
                continue
            pickup_locations = self._department_waste_pickup_locations_for_seed(dept)
            if not pickup_locations:
                continue
            for stream_item in self._normalise_department_waste_stream_items_for_seed(
                dept
            ):
                if not bool(stream_item.get("initial_container_present", True)):
                    continue
                stream_name = str(stream_item.get("name", "")).strip()
                stream_cfg = self.waste_streams.get(stream_name, {}) or {}
                payload_name = str(
                    stream_cfg.get("payload", stream_item.get("payload", "")) or ""
                ).strip()
                payload = self.payloads.get(payload_name)
                if payload is None:
                    continue
                for pickup_location in pickup_locations:
                    seeded_space = None
                    if self._location_has_payload_inventory_spaces(pickup_location):
                        seeded_space = self._claim_initial_inventory_space(
                            pickup_location,
                            payload,
                            "initial_waste_container",
                        )
                        if seeded_space is None:
                            continue
                    group_key = self._waste_container_group_key_for_seed(
                        dept_id, stream_name, stream_item, pickup_location
                    )
                    if group_key in seeded_groups:
                        if seeded_space is not None:
                            seeded_space["occupied"] = False
                            seeded_space["payload"] = ""
                            seeded_space["payload_instance_id"] = ""
                            seeded_space["task_id"] = ""
                            seeded_space["reserved_by_task"] = ""
                        continue
                    seeded_groups.add(group_key)
                    instance_id = self.payload_instance_store.make_instance_id(
                        payload_name, group_key
                    )
                    seeded_inventory_space = ""
                    if seeded_space is not None:
                        seeded_space["payload_instance_id"] = instance_id
                        seeded_inventory_space = str(seeded_space.get("name", "") or "")
                    self.payload_instance_store.store(
                        pickup_location,
                        payload_name,
                        instance_id,
                        source_task_id="initial_waste_container",
                        metadata={
                            "task_source": "initial_waste_container",
                            "department_id": dept_id,
                            "waste_stream": stream_name,
                            "container_group": group_key,
                            "container_type": payload_name,
                            "container_state": "empty",
                        },
                    )
                    self.initial_waste_container_instances[
                        (group_key, pickup_location, payload_name)
                    ] = instance_id
                    self._log_payload_location_event(
                        "location_payload_enter",
                        pickup_location,
                        payload_name,
                        instance_id,
                        inventory_space=seeded_inventory_space,
                    )
                    self._record_payload_population_snapshot()

    def _mark_generated_waste_task_requires_existing_container(
        self, task: Task
    ) -> None:
        if not getattr(self, "seed_waste_stream_containers_at_start", False):
            return

        is_waste_task = bool(str(getattr(task, "waste_stream", "") or "").strip()) or (
            str(getattr(task, "task_source", "") or "").strip() == "department_waste"
        )
        if not is_waste_task:
            return

        # Department waste streams may deliberately opt out of having a physical
        # container present at the start.  In that case the task should keep the
        # normal outbound behaviour and create a new payload instance instead of
        # waiting forever for an existing bin.
        if not bool(getattr(task, "initial_container_present", True)):
            return

        task.requires_existing_payload_instance = True
        self._assign_available_existing_payload_instance(task)

        # A seeded waste container is a physical bin, so after it has been emptied
        # at the waste destination it must be returned to the department pickup
        # location.  The task-generation category may have an empty return_payload
        # because the payload is resolved from the waste stream, so force the
        # return to use the same payload and instance when a seeded container was
        # found.
        if str(getattr(task, "payload_instance_id", "") or "").strip():
            task.return_enabled = True
            task.return_payload = normalise_payload_name(getattr(task, "payload", ""))
            task.return_same_payload_instance = True
            task.generator_waits_for_return = True
            if not getattr(task, "return_priority", 0):
                task.return_priority = int(getattr(task, "priority", 100) or 100)
            if not str(getattr(task, "return_route_profile", "") or "").strip():
                task.return_route_profile = str(
                    getattr(task, "route_profile", "") or ""
                ).strip()

    def _assign_available_existing_payload_instance(self, task: Task) -> None:
        payload_name = normalise_payload_name(getattr(task, "payload", ""))
        if not payload_name:
            return
        current_instance_id = str(
            getattr(task, "payload_instance_id", "") or ""
        ).strip()
        if current_instance_id:
            return

        # First try the task's requested pickup.  This is correct for
        # non-shared bins and for shared bins where all departments point at the
        # same physical waste room.
        for record in self.payload_instance_store.records_at(task.pickup):
            if record.payload != payload_name:
                continue
            if not self._record_is_available_empty_container(record):
                continue
            if not self.payload_instance_store.reserve_instance(
                record.instance_id, str(getattr(task, "id", "") or "")
            ):
                continue
            task.payload_instance_id = record.instance_id
            return

        # Shared-bin waste tasks may be generated by whichever department pushes
        # the shared volume over the threshold.  The physical seeded bin may
        # actually be stored at another department/shared waste room.  In that
        # case, move the task pickup to the current physical bin location rather
        # than leaving a generated task pending forever at the contributing
        # department's own pickup point.
        container_group = str(getattr(task, "container_group", "") or "").strip()
        if not container_group:
            return

        records = getattr(self.payload_instance_store, "_records", {}) or {}
        for record in list(records.values()):
            if getattr(record, "payload", "") != payload_name:
                continue
            if self.payload_instance_store.is_reserved(
                getattr(record, "instance_id", ""),
                excluding_owner=str(getattr(task, "id", "") or ""),
            ):
                continue
            if not self._record_is_available_empty_container(record):
                continue
            metadata = getattr(record, "metadata", {}) or {}
            if (
                str(metadata.get("container_group", "") or "").strip()
                != container_group
            ):
                continue
            if not self.payload_instance_store.reserve_instance(
                record.instance_id, str(getattr(task, "id", "") or "")
            ):
                continue
            task.payload_instance_id = record.instance_id
            task.pickup = record.location
            return

    def _init_department_runtime(self):
        self.department_runtime = {}

        for dept in self.departments:
            dept_id = str(dept.get("id", "")).strip()
            if not dept_id:
                continue

            waste_stream_names = [
                str(x).strip()
                for x in dept.get("waste_streams", [])
                if str(x).strip() in self.waste_streams
            ]
            if not waste_stream_names:
                continue

            self.department_runtime[dept_id] = {}

            for stream_name in waste_stream_names:
                stream_cfg = dict(self.waste_streams.get(stream_name, {}))
                container_capacity_m3 = float(
                    stream_cfg.get("container_capacity_m3", 0.0) or 0.0
                )
                full_threshold_fraction = float(
                    stream_cfg.get("full_threshold_fraction", 0.8) or 0.8
                )

                self.department_runtime[dept_id][stream_name] = {
                    "last_update_time": 0.0,
                    "fill_m3": 0.0,
                    "generated_m3_total": 0.0,
                    "tasks_created": 0,
                    "container_capacity_m3": container_capacity_m3,
                    "full_threshold_fraction": full_threshold_fraction,
                }

    def _day_key_for_sim_time(self, sim_time_sec: float) -> str:
        dt = self.clock.sim_seconds_to_datetime(sim_time_sec)
        return ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][dt.weekday()]

    def _parse_hhmm_to_minutes(self, value, default=None):
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

    def _department_operating_start_minutes(self, dept: dict) -> int:
        return int(
            self._parse_hhmm_to_minutes(dept.get("operating_start_time"), 0) or 0
        )

    def _department_operating_end_minutes(self, dept: dict) -> int:
        explicit = self._parse_hhmm_to_minutes(dept.get("operating_end_time"), None)
        if explicit is not None:
            return int(explicit)
        start = self._department_operating_start_minutes(dept)
        hours = float(dept.get("hours_operated_per_day", 24.0) or 24.0)
        if hours >= 24.0:
            return start + (24 * 60)
        return start + int(round(max(0.0, hours) * 60.0))

    def _department_operating_hours_per_day(self, dept: dict) -> float:
        start = self._department_operating_start_minutes(dept)
        end = self._department_operating_end_minutes(dept)
        if end == start:
            return 24.0
        if end < start:
            end += 24 * 60
        return max(1.0, min((end - start) / 60.0, 24.0))

    def _department_is_active(self, dept: dict, sim_time_sec: float) -> bool:
        if not bool(dept.get("enabled", True)):
            return False

        active_days = dept.get("days_active", [])
        if active_days:
            if self._day_key_for_sim_time(sim_time_sec) not in set(active_days):
                return False

        start = self._department_operating_start_minutes(dept)
        end = self._department_operating_end_minutes(dept)
        if end == start:
            return True

        dt = self.clock.sim_seconds_to_datetime(sim_time_sec)
        current = (dt.hour * 60) + dt.minute + (dt.second / 60.0)

        if end > 24 * 60:
            end = end % (24 * 60)

        if end > start:
            return start <= current < end

        # Overnight window, e.g. 20:00 to 06:00.
        return current >= start or current < end

    def _department_hourly_waste_rate_m3(
        self, dept: dict, sim_time_sec: float
    ) -> float:
        waste_cfg = dict(dept.get("waste", {}) or {})

        alpha = float(waste_cfg.get("alpha", 0.0) or 0.0)
        beta = float(waste_cfg.get("beta", 0.0) or 0.0)
        gamma = float(waste_cfg.get("gamma", 0.0) or 0.0)

        bed_count = float(dept.get("bed_count", 0.0) or 0.0)
        patient_turnover = float(dept.get("patient_turnover", 0.0) or 0.0)
        staff_count = float(dept.get("staff_count", 0.0) or 0.0)
        hours_operated = self._department_operating_hours_per_day(dept)

        # Turnover is assumed to be per active day, spread across operating hours
        turnover_per_hour = patient_turnover / hours_operated

        return (alpha * bed_count) + (beta * turnover_per_hour) + (gamma * staff_count)

    def _make_department_waste_task_id(self, dept_id: str, stream_name: str) -> str:
        self.department_task_counter += 1
        safe_stream = "".join(
            c if c.isalnum() else "_" for c in stream_name.upper()
        ).strip("_")
        return f"WASTE_{dept_id}_{safe_stream}_{self.department_task_counter:05d}"

    def _create_department_waste_task(
        self,
        dept: dict,
        stream_name: str,
        release_time: float,
        waste_volume_m3: float,
    ):
        dept_id = str(dept.get("id", "")).strip()
        dept_name = str(dept.get("name", dept_id)).strip()
        stream_cfg = dict(self.waste_streams.get(stream_name, {}))
        waste_cfg = dict(dept.get("waste", {}) or {})

        pickup_location = str(waste_cfg.get("pickup_location", "")).strip()
        dropoff_location = str(waste_cfg.get("dropoff_location", "")).strip()
        payload_name = str(stream_cfg.get("payload", "")).strip()

        if not pickup_location or pickup_location not in self.locations:
            return
        if not dropoff_location or dropoff_location not in self.locations:
            return
        if not payload_name or payload_name not in self.payloads:
            return

        task = Task(
            id=self._make_department_waste_task_id(dept_id, stream_name),
            pickup=pickup_location,
            dropoff=dropoff_location,
            payload=payload_name,
            release_time=release_time,
            target_time=0.0,
            quantity=1,
            priority=60,
            created_during_runtime=True,
            labels=["waste", stream_name],
            route_profile=None,
            task_source="department_waste",
            department_id=dept_id,
            waste_stream=stream_name,
            waste_volume_m3=float(waste_volume_m3),
            container_type=payload_name,
        )

        self.schedule_task_release(task)

        self.log_step(
            event_time=release_time,
            event_type="waste_task_generated",
            task_id=task.id,
            details=f"Generated waste collection for {dept_name} / {stream_name}",
            from_location=pickup_location,
            to_location=dropoff_location,
            payload_name=payload_name,
            duration_sec=0.0,
            wait_time_sec=0.0,
            distance_m=0.0,
            start_time=release_time,
            end_time=release_time,
            start_node=pickup_location,
            end_node=dropoff_location,
            start_x=self.locations[pickup_location].x,
            start_y=self.locations[pickup_location].y,
            start_floor=self.locations[pickup_location].floor,
            end_x=self.locations[dropoff_location].x,
            end_y=self.locations[dropoff_location].y,
            end_floor=self.locations[dropoff_location].floor,
            status="generated",
            energy_kwh=0.0,
            task_source="department_waste",
            department_id=dept_id,
            waste_stream=stream_name,
            waste_volume_m3=float(waste_volume_m3),
            container_type=payload_name,
        )

    def _update_department_waste_until(self, now: float):
        if now <= 0 or not self.departments:
            return

        for dept in self.departments:
            dept_id = str(dept.get("id", "")).strip()
            if not dept_id:
                continue
            runtime_by_stream = self.department_runtime.get(dept_id, {})
            if not runtime_by_stream:
                continue

            for stream_name, runtime in runtime_by_stream.items():
                last_time = float(runtime.get("last_update_time", 0.0) or 0.0)
                if now <= last_time:
                    continue

                if self._department_is_active(dept, now):
                    elapsed_hours = (now - last_time) / 3600.0
                    hourly_rate = self._department_hourly_waste_rate_m3(dept, now)
                    generated_m3 = max(0.0, elapsed_hours * hourly_rate)

                    runtime["fill_m3"] += generated_m3
                    runtime["generated_m3_total"] += generated_m3

                    container_capacity_m3 = float(
                        runtime.get("container_capacity_m3", 0.0) or 0.0
                    )
                    full_threshold_fraction = float(
                        runtime.get("full_threshold_fraction", 0.8) or 0.8
                    )
                    trigger_volume_m3 = container_capacity_m3 * full_threshold_fraction

                    if trigger_volume_m3 > 0:
                        while runtime["fill_m3"] >= trigger_volume_m3:
                            self._create_department_waste_task(
                                dept=dept,
                                stream_name=stream_name,
                                release_time=now,
                                waste_volume_m3=trigger_volume_m3,
                            )
                            runtime["fill_m3"] -= trigger_volume_m3
                            runtime["tasks_created"] += 1

                runtime["last_update_time"] = now

    def _queue_pending_task(self, task: Task):
        self.pending_task_counter += 1
        self._removed_pending_task_ids.discard(str(getattr(task, "id", "") or ""))
        heapq.heappush(
            self.pending_tasks,
            (task.priority, task.release_time, self.pending_task_counter, task),
        )
        self._mark_task_activity_changed()

    def _pending_task_removed(self, task: Task) -> bool:
        return str(getattr(task, "id", "") or "") in self._removed_pending_task_ids

    def _purge_removed_pending_task_heads(self) -> None:
        while self.pending_tasks and self._pending_task_removed(
            self.pending_tasks[0][3]
        ):
            heapq.heappop(self.pending_tasks)

    def _compact_pending_tasks_if_needed(self) -> None:
        removed_count = len(self._removed_pending_task_ids)
        if removed_count <= 0:
            return
        if removed_count < 128 and removed_count < max(1, len(self.pending_tasks) // 4):
            return
        self.pending_tasks = [
            item
            for item in self.pending_tasks
            if not self._pending_task_removed(item[3])
        ]
        heapq.heapify(self.pending_tasks)
        live_ids = {
            str(getattr(item[3], "id", "") or "") for item in self.pending_tasks
        }
        self._removed_pending_task_ids.intersection_update(live_ids)

    def _live_pending_task_items(self):
        return [
            item
            for item in self.pending_tasks
            if not self._pending_task_removed(item[3])
        ]

    def _distance_same_floor(self, a: Location, b: Location) -> float:
        return math.hypot(b.x - a.x, b.y - a.y)

    def _physical_edge_key(self, a_name: str, b_name: str) -> Tuple[str, str]:
        # Same physical corridor edge in either direction.
        return tuple(sorted((a_name, b_name)))

    def _spacing_time_sec(self, amr: AMR) -> float:
        return self.amr_spacing_m / max(amr.speed_m_per_sec, 1e-9)

    def _edge_recent_demand(self, edge_key: Tuple[str, str], t: float) -> int:
        reservations = self.edge_reservations.get(edge_key, [])
        window = self.edge_congestion_window_sec
        max_duration = self.edge_reservation_max_duration.get(edge_key, 0.0)
        scan_start = self._reservation_scan_start(
            reservations, t - window, 0.0, max_duration
        )
        count = 0
        for index in range(scan_start, len(reservations)):
            start, end, _ = reservations[index]
            if start > t + window:
                break
            if end >= t - window:
                count += 1
        return count

    def _directed_no_overtake_start(
        self,
        directed_edge_key: Tuple[str, str],
        requested_start: float,
        duration: float,
        spacing_time: float,
    ) -> float:
        """Return the earliest same-direction edge start that preserves FIFO order."""
        reservations = self.directed_edge_reservations.get(directed_edge_key, [])
        max_reserved_duration = self.directed_edge_reservation_max_duration.get(
            directed_edge_key, 0.0
        )
        t = float(requested_start)
        duration = max(0.0, float(duration or 0.0))
        spacing_time = max(0.0, float(spacing_time or 0.0))

        while True:
            adjusted = t
            scan_start = self._reservation_scan_start(
                reservations, t, spacing_time, max_reserved_duration
            )
            for index in range(scan_start, len(reservations)):
                start, end, _ = reservations[index]
                start = float(start)
                end = float(end)
                if start > t + duration + spacing_time:
                    break

                if t >= start - 1e-9:
                    adjusted = max(adjusted, start + spacing_time)
                    adjusted = max(adjusted, (end + spacing_time) - duration)
                elif t + duration > end - spacing_time:
                    adjusted = max(adjusted, end + spacing_time)

            if adjusted <= t + 1e-9:
                return t
            t = adjusted

    def _find_next_edge_start(
        self,
        edge_key: Tuple[str, str],
        requested_start: float,
        duration: float,
        spacing_time: float,
        directed_edge_key: Optional[Tuple[str, str]] = None,
        max_concurrency: Optional[int] = None,
    ) -> Tuple[float, int]:
        reservations = self.edge_reservations.get(edge_key, [])
        max_reserved_duration = self.edge_reservation_max_duration.get(edge_key, 0.0)
        t = float(requested_start)
        duration = max(0.0, float(duration or 0.0))
        spacing_time = max(0.0, float(spacing_time or 0.0))
        capacity = max(1, int(max_concurrency if max_concurrency is not None else self.edge_max_concurrency))

        while True:
            overlap_count = 0
            next_candidate = None
            scan_start = self._reservation_scan_start(
                reservations, t, spacing_time, max_reserved_duration
            )

            for index in range(scan_start, len(reservations)):
                start, end, _ = reservations[index]
                if start > t + duration + spacing_time:
                    break
                protected_start = float(start) - spacing_time
                protected_end = float(end) + spacing_time

                if not (t + duration <= protected_start or t >= protected_end):
                    overlap_count += 1
                    if next_candidate is None or protected_end < next_candidate:
                        next_candidate = protected_end

            candidate_t = t
            if overlap_count >= capacity:
                if next_candidate is None:
                    return t, overlap_count
                candidate_t = max(candidate_t, next_candidate)

            if directed_edge_key is not None:
                candidate_t = max(
                    candidate_t,
                    self._directed_no_overtake_start(
                        directed_edge_key,
                        candidate_t,
                        duration,
                        spacing_time,
                    ),
                )

            if candidate_t <= t + 1e-9 and overlap_count < capacity:
                return t, overlap_count

            t = candidate_t

    def _reserve_edge(
        self,
        from_name: str,
        to_name: str,
        start_time: float,
        end_time: float,
        amr_id: str,
    ):
        edge_key = self._physical_edge_key(from_name, to_name)
        item = (start_time, end_time, amr_id)

        reservations = self.edge_reservations[edge_key]
        insort_right(reservations, item)
        interval_duration = max(0.0, float(end_time) - float(start_time))
        self.edge_reservation_max_duration[edge_key] = max(
            self.edge_reservation_max_duration.get(edge_key, 0.0), interval_duration
        )

        directed_key = (from_name, to_name)
        directed_reservations = self.directed_edge_reservations[directed_key]
        insort_right(directed_reservations, item)
        self.directed_edge_reservation_max_duration[directed_key] = max(
            self.directed_edge_reservation_max_duration.get(directed_key, 0.0),
            interval_duration,
        )

    def _reserve_node(
        self,
        node_name: str,
        start_time: float,
        end_time: float,
        amr_id: str,
    ):
        reservations = self.node_reservations[node_name]
        item = (start_time, end_time, amr_id)
        insort_right(reservations, item)
        self.node_reservation_max_duration[node_name] = max(
            self.node_reservation_max_duration.get(node_name, 0.0),
            max(0.0, float(end_time) - float(start_time)),
        )

    def _find_next_node_arrival(
        self,
        node_name: str,
        requested_arrival: float,
        spacing_time: float,
    ) -> float:
        reservations = self.node_reservations.get(node_name, [])
        max_reserved_duration = self.node_reservation_max_duration.get(node_name, 0.0)
        t = requested_arrival

        while True:
            blocked = False
            next_candidate = None
            scan_start = self._reservation_scan_start(
                reservations, t, spacing_time, max_reserved_duration
            )

            for index in range(scan_start, len(reservations)):
                start, end, _ = reservations[index]
                if start > t + spacing_time:
                    break
                protected_start = start - spacing_time
                protected_end = end + spacing_time

                if protected_start <= t < protected_end:
                    blocked = True
                    if next_candidate is None or protected_end < next_candidate:
                        next_candidate = protected_end

            if not blocked:
                return t

            if next_candidate is None:
                return t

            t = next_candidate

    def _reserve_corridor_segments(
        self,
        amr: AMR,
        segments: List[dict],
        start_time: float,
    ):
        t = start_time
        spacing_time = self._spacing_time_sec(amr)

        for segment in segments:
            duration = float(segment.get("duration", 0.0))
            seg_type = segment.get("type", "")

            if seg_type == "corridor":
                self._reserve_edge(
                    segment["from"],
                    segment["to"],
                    t,
                    t + duration,
                    amr.id,
                )
                self._reserve_node(
                    segment["to"],
                    t + duration,
                    t + duration + self.node_clearance_time_sec,
                    amr.id,
                )

            elif seg_type in {"wait_for_edge", "wait_for_node"}:
                node_name = segment.get("from") or segment.get("to")
                if node_name:
                    self._reserve_node(
                        node_name,
                        t,
                        t + duration,
                        amr.id,
                    )

            t += duration

    def _travel_same_floor(self, amr: AMR, start: Location, end: Location) -> float:
        route = self._shortest_path_same_floor(start.floor, start.name, end.name)
        if route is None:
            return math.inf
        return route["distance_m"] / max(amr.speed_m_per_sec, 1e-9)

    def _same_floor_segments(
        self,
        amr: AMR,
        start: Location,
        end: Location,
        rules: Optional[dict] = None,
        start_time_value: Optional[float] = None,
        payload: Optional[PayloadType] = None,
        orientation: str = "lengthways",
    ) -> Optional[Tuple[List[dict], float, float]]:
        route = self._shortest_path_same_floor(start.floor, start.name, end.name, rules=rules)
        if route is None:
            return None
        segments: List[dict] = []
        total_duration = 0.0
        current = start_time_value
        spacing_time = self._spacing_time_sec(amr)
        for edge in route["edges"]:
            base_duration = float(edge["distance_m"]) / max(float(amr.speed_m_per_sec), 1e-9)
            lane_count, lane_width, carrying_length = self._corridor_lane_capacity(edge, amr, payload, orientation)
            congestion_count = 0
            people_count = 0
            people_groups = ""
            people_factor = 1.0
            scenario_factor = 1.0
            scenario_notes = ""
            scenario_wait = 0.0
            edge_wait = 0.0
            node_wait = 0.0
            if current is None:
                duration = base_duration
                speed_factor = 1.0
            else:
                corridor_id = f"{edge['from']} -> {edge['to']}"
                corridor_factor, blocked_until, corridor_notes = self._resource_speed_factor(
                    "corridor", corridor_id, current
                )
                from_node_factor, from_node_blocked, from_node_notes = self._resource_speed_factor(
                    "corridor_node", edge["from"], current
                )
                to_node_factor, to_node_blocked, to_node_notes = self._resource_speed_factor(
                    "corridor_node", edge["to"], current
                )
                amr_factor, amr_blocked_until, amr_notes = self._resource_speed_factor(
                    "amr", amr.id, current
                )
                blocked_until = max(
                    blocked_until,
                    from_node_blocked,
                    to_node_blocked,
                    amr_blocked_until,
                )
                if blocked_until > current + 1e-9:
                    scenario_wait = blocked_until - current
                    segments.append({
                        "type": "wait_for_scenario",
                        "from": edge["from"],
                        "to": edge["from"],
                        "duration": scenario_wait,
                        "distance_m": 0.0,
                        "scenario_name": self.scenario_name,
                        "scenario_reason": (
                            corridor_notes
                            or from_node_notes
                            or to_node_notes
                            or amr_notes
                            or "Configured resource outage"
                        ),
                    })
                    total_duration += scenario_wait
                    current = blocked_until
                    corridor_factor, _blocked, corridor_notes = self._resource_speed_factor(
                        "corridor", corridor_id, current
                    )
                    from_node_factor, _blocked, from_node_notes = self._resource_speed_factor(
                        "corridor_node", edge["from"], current
                    )
                    to_node_factor, _blocked, to_node_notes = self._resource_speed_factor(
                        "corridor_node", edge["to"], current
                    )
                    amr_factor, _blocked, amr_notes = self._resource_speed_factor(
                        "amr", amr.id, current
                    )
                scenario_factor = min(
                    corridor_factor or 1e-9,
                    from_node_factor or 1e-9,
                    to_node_factor or 1e-9,
                    amr_factor or 1e-9,
                )
                scenario_notes = (
                    corridor_notes or from_node_notes or to_node_notes or amr_notes
                )
                edge_key = self._physical_edge_key(edge["from"], edge["to"])
                congestion_count = self._edge_recent_demand(edge_key, current)
                congestion_factor = max(self.min_congestion_speed_factor, 1.0 - congestion_count * self.edge_slowdown_per_amr)
                people_count, people_factor, people_groups = self._people_on_edge(edge_key, current, base_duration)
                speed_factor = max(0.01, min(congestion_factor, people_factor, scenario_factor))
                travel_duration = base_duration / speed_factor
                edge_start, _ = self._find_next_edge_start(
                    edge_key=edge_key, requested_start=current, duration=travel_duration,
                    spacing_time=spacing_time, directed_edge_key=(edge["from"], edge["to"]),
                    max_concurrency=lane_count,
                )
                edge_wait = max(0.0, edge_start - current)
                if edge_wait > 0:
                    segments.append({
                        "type": "wait_for_edge", "from": edge["from"], "to": edge["from"],
                        "duration": edge_wait, "distance_m": 0.0,
                        "congestion_count": congestion_count, "route_lane_count": lane_count,
                    })
                    total_duration += edge_wait
                    current = edge_start
                proposed_arrival = current + travel_duration
                safe_arrival = self._find_next_node_arrival(edge["to"], proposed_arrival, spacing_time)
                node_wait = max(0.0, safe_arrival - proposed_arrival)
                if node_wait > 0:
                    # Preserve the existing smooth-congestion behaviour: absorb
                    # a node conflict by slowing along the edge where possible.
                    # If that would require an impractically low speed, wait at
                    # the edge start and then travel at the lowest active safety
                    # factor (congestion, people or scenario).
                    adjusted_duration = travel_duration + node_wait
                    effective_speed_factor = base_duration / max(adjusted_duration, 1e-9)
                    minimum_active_factor = max(
                        0.01,
                        min(
                            self.min_congestion_speed_factor,
                            people_factor,
                            scenario_factor,
                        ),
                    )
                    if effective_speed_factor >= minimum_active_factor:
                        duration = adjusted_duration
                        speed_factor = min(speed_factor, effective_speed_factor)
                        node_wait = 0.0
                    else:
                        duration = base_duration / minimum_active_factor
                        stop_wait = max(0.0, safe_arrival - (current + duration))
                        if stop_wait > 0:
                            segments.append({
                                "type": "wait_for_node",
                                "from": edge["from"],
                                "to": edge["from"],
                                "blocked_node": edge["to"],
                                "duration": stop_wait,
                                "distance_m": 0.0,
                                "congestion_count": congestion_count,
                                "route_lane_count": lane_count,
                            })
                            total_duration += stop_wait
                            current += stop_wait
                        node_wait = stop_wait
                        speed_factor = minimum_active_factor
                else:
                    duration = travel_duration
            people_delay = max(0.0, base_duration / max(people_factor, 1e-9) - base_duration) if people_count else 0.0
            scenario_delay = max(0.0, base_duration / max(scenario_factor, 1e-9) - base_duration) + scenario_wait if self.scenario_mode else 0.0
            segments.append({
                "type": "corridor", "from": edge["from"], "to": edge["to"],
                "duration": duration, "distance_m": edge["distance_m"],
                "speed_factor": speed_factor, "congestion_count": congestion_count,
                "people_count": people_count, "people_groups": people_groups,
                "people_speed_factor": people_factor, "people_delay_sec": people_delay,
                "scenario_name": self.scenario_name if self.scenario_mode else "",
                "scenario_reason": scenario_notes, "scenario_delay_sec": scenario_delay,
                "route_lane_count": lane_count,
                "corridor_width_m": float(edge.get("width_m", self.default_corridor_width_m)),
                "configured_corridor_width_m": float(edge.get("configured_width_m", edge.get("width_m", self.default_corridor_width_m))),
                "door_restricted": bool(edge.get("door_restricted", False)),
                "door_nodes": ",".join(edge.get("door_nodes", []) or []),
                "lane_width_m": lane_width, "carrying_length_m": carrying_length,
                "payload_orientation": orientation if payload is not None else "",
            })
            total_duration += duration
            if current is not None:
                current += duration
        return segments, total_duration, route["distance_m"]

    def _lift_location_on_floor(self, lift: Lift, floor: int) -> Location:
        return lift.location_on_floor(floor)

    def _lift_vertical_seconds(
        self, lift: Lift, from_floor: int, to_floor: int, at_time: Optional[float] = None
    ) -> float:
        health_factor = self._lift_health_speed_factor(lift)
        scenario_factor, _blocked_until, _notes = self._resource_speed_factor(
            "lift", lift.id, self.current_time if at_time is None else at_time
        )
        combined_factor = health_factor * scenario_factor
        if combined_factor <= 0.0:
            return math.inf
        base_speed = max(float(lift.speed_floors_per_sec or 0.0), 1e-9)
        return abs(int(to_floor) - int(from_floor)) / max(base_speed * combined_factor, 1e-9)

    def _lift_service_finish_time(
        self,
        lift: Lift,
        reposition_start: float,
        reposition_sec: float,
        loaded_travel_sec: float,
    ) -> Tuple[float, float, float, float]:
        reposition_finish = float(reposition_start) + float(reposition_sec)
        board_start = reposition_finish
        loaded_start = board_start + lift.door_time_sec + lift.boarding_time_sec
        loaded_finish = loaded_start + loaded_travel_sec
        unload_finish = loaded_finish + lift.door_time_sec + lift.boarding_time_sec
        return reposition_finish, loaded_start, loaded_finish, unload_finish

    def _find_lift_journey_slot(
        self,
        lift: Lift,
        arrival_at_lift: float,
        origin_floor: int,
        destination_floor: int,
    ) -> dict:
        reservations = self.lift_reservations.get(lift.id, [])
        initial_floor = int(
            self.lift_initial_floors.get(lift.id, getattr(lift, "current_floor", 0))
        )
        previous_finish = 0.0
        previous_floor = initial_floor
        blackout_until = max(
            0.0,
            float(getattr(lift, "failed_until", 0.0) or 0.0),
            float(getattr(lift, "available_time", 0.0) or 0.0)
            if not reservations
            else 0.0,
        )

        loaded_travel_sec = self._lift_vertical_seconds(
            lift, origin_floor, destination_floor
        )

        for (
            next_start,
            next_finish,
            next_origin,
            next_destination,
            _owner,
        ) in reservations:
            reposition_sec = self._lift_vertical_seconds(
                lift, previous_floor, origin_floor
            )
            reposition_start = max(
                float(arrival_at_lift), previous_finish, blackout_until
            )
            reposition_finish, loaded_start, loaded_finish, unload_finish = (
                self._lift_service_finish_time(
                    lift, reposition_start, reposition_sec, loaded_travel_sec
                )
            )
            reset_for_next_sec = self._lift_vertical_seconds(
                lift, destination_floor, int(next_origin)
            )
            if unload_finish + reset_for_next_sec <= float(next_start) + 1e-9:
                return {
                    "lift_start": reposition_start,
                    "lift_finish": unload_finish,
                    "reposition_from_floor": previous_floor,
                    "reposition_to_floor": origin_floor,
                    "reposition_sec": reposition_sec,
                    "reposition_finish": reposition_finish,
                    "loaded_start": loaded_start,
                    "loaded_finish": loaded_finish,
                    "loaded_travel_sec": loaded_travel_sec,
                }

            previous_finish = max(previous_finish, float(next_finish))
            previous_floor = int(next_destination)

        reposition_sec = self._lift_vertical_seconds(lift, previous_floor, origin_floor)
        reposition_start = max(float(arrival_at_lift), previous_finish, blackout_until)
        reposition_finish, loaded_start, loaded_finish, unload_finish = (
            self._lift_service_finish_time(
                lift, reposition_start, reposition_sec, loaded_travel_sec
            )
        )
        return {
            "lift_start": reposition_start,
            "lift_finish": unload_finish,
            "reposition_from_floor": previous_floor,
            "reposition_to_floor": origin_floor,
            "reposition_sec": reposition_sec,
            "reposition_finish": reposition_finish,
            "loaded_start": loaded_start,
            "loaded_finish": loaded_finish,
            "loaded_travel_sec": loaded_travel_sec,
        }

    def _reserve_lift_journey(self, plan: dict, amr_id: str = "") -> None:
        lift = plan["lift"]
        item = (
            float(plan["lift_start"]),
            float(plan["lift_finish"]),
            int(plan["reposition_to_floor"]),
            int(plan["destination_lift"].floor),
            str(amr_id or ""),
        )
        insort_right(self.lift_reservations[lift.id], item)
        tail = max(
            self.lift_reservations[lift.id],
            key=lambda reservation: reservation[1],
            default=None,
        )
        if tail is not None:
            lift.available_time = max(
                float(getattr(lift, "failed_until", 0.0) or 0.0),
                float(tail[1]),
            )
            lift.current_floor = int(tail[3])

    def _nearest_compatible_lift_plan(
        self,
        ready_time: float,
        amr: AMR,
        from_loc: Location,
        to_loc: Location,
        payload: PayloadType,
        rules: Optional[dict] = None,
        orientation: str = "lengthways",
    ) -> Optional[dict]:
        best_plan = None
        best_finish = math.inf
        rules = rules or self._empty_route_rules()

        for lift in self.lifts:
            if not self._lift_allowed(lift, rules):
                continue
            if not lift.can_serve(from_loc.floor, to_loc.floor):
                continue
            if self._lift_health_speed_factor(lift) <= 0.0:
                continue
            lift_scenario = self._scenario_event_state("lift", lift.id, ready_time)
            if float(lift_scenario.get("availability_percent", 100.0)) <= 0.0:
                continue
            if not lift.can_fit(payload, amr, orientation=orientation):
                continue

            origin_lift = self._lift_location_on_floor(lift, from_loc.floor)
            destination_lift = self._lift_location_on_floor(lift, to_loc.floor)

            if not self._node_allowed(origin_lift.name, rules):
                continue
            if not self._node_allowed(destination_lift.name, rules):
                continue

            ##
            to_lift_route = self._same_floor_segments(
                amr,
                from_loc,
                origin_lift,
                rules=rules,
                start_time_value=ready_time,
                payload=payload,
                orientation=orientation,
            )
            if to_lift_route is None:
                continue

            to_lift_segments, to_lift_sec, to_lift_distance_m = to_lift_route

            arrival_at_lift = ready_time + to_lift_sec
            lift_slot = self._find_lift_journey_slot(
                lift,
                arrival_at_lift,
                int(from_loc.floor),
                int(to_loc.floor),
            )
            lift_start = lift_slot["lift_start"]
            lift_finish = lift_slot["lift_finish"]

            from_lift_route = self._same_floor_segments(
                amr,
                destination_lift,
                to_loc,
                rules=rules,
                start_time_value=lift_finish,
                payload=payload,
                orientation=orientation,
            )
            if from_lift_route is None:
                continue

            from_lift_segments, from_lift_sec, from_lift_distance_m = from_lift_route
            final_finish = lift_finish + from_lift_sec

            ##

            if final_finish < best_finish:
                best_finish = final_finish
                best_plan = {
                    "lift": lift,
                    "origin_lift": origin_lift,
                    "destination_lift": destination_lift,
                    "to_lift_segments": to_lift_segments,
                    "from_lift_segments": from_lift_segments,
                    "to_lift_distance_m": to_lift_distance_m,
                    "from_lift_distance_m": from_lift_distance_m,
                    "to_lift_sec": to_lift_sec,
                    "from_lift_sec": from_lift_sec,
                    "lift_start": lift_start,
                    "lift_finish": lift_finish,
                    "wait_time": max(0.0, (lift_start - arrival_at_lift)),
                    "vertical_distance_m": abs(to_loc.floor - from_loc.floor)
                    * self.floor_height_m,
                    "final_finish": final_finish,
                    "reposition_from_floor": lift_slot["reposition_from_floor"],
                    "reposition_to_floor": lift_slot["reposition_to_floor"],
                    "reposition_sec": lift_slot["reposition_sec"],
                    "loaded_travel_sec": lift_slot["loaded_travel_sec"],
                    "reposition_start": lift_start,
                    "reposition_finish": lift_slot["reposition_finish"],
                    "loaded_start": lift_slot["loaded_start"],
                    "loaded_finish": lift_slot["loaded_finish"],
                }

        return best_plan

    def _charge_location_candidates(self) -> List[Location]:
        candidates = []
        for name in self.charge_location_names:
            if name in self.locations:
                candidates.append(self.locations[name])
        if not candidates and self.charge_location_name in self.locations:
            candidates.append(self.locations[self.charge_location_name])
        return candidates

    def _select_charge_location_for_amr(
        self, amr: AMR, current_loc: Location, now: float
    ) -> Optional[Location]:
        candidates = self._charge_location_candidates()
        if not candidates:
            return None
        best_loc = None
        best_finish = math.inf
        dummy_payload = self.payloads.get(EMPTY_PAYLOAD_NAME)
        if dummy_payload is None:
            dummy_payload = PayloadType("empty", 0.0)
        for charge_loc in candidates:
            # Charging locations that define AMR bays must have a compatible bay
            # for this AMR type.  Previously a location with AMR-B bays but no
            # AMR-C bay was still treated as a valid AMR-C charger, which left
            # AMR-C units stranded on the location node.
            location_has_amr_bays = self._location_has_any_amr_inventory_spaces(
                charge_loc.name
            )
            amr_spaces = [
                space
                for space in self.inventory_spaces_by_location.get(charge_loc.name, [])
                if self._inventory_space_accepts_amr(space, amr)
                and self._inventory_space_has_charger(space)
            ]
            if location_has_amr_bays and not amr_spaces:
                continue
            if amr_spaces and (
                self._reserved_amr_inventory_space(charge_loc.name, amr, require_charger=True) is None
                and self._find_free_amr_inventory_space(charge_loc.name, amr, require_charger=True) is None
            ):
                continue

            if current_loc.floor == charge_loc.floor:
                route = self._same_floor_segments(
                    amr, current_loc, charge_loc, start_time_value=now,
                    payload=self.payloads.get(EMPTY_PAYLOAD_NAME), orientation="lengthways"
                )
                if route is None:
                    continue
                finish = now + route[1]
            else:
                plan = self._nearest_compatible_lift_plan(
                    now, amr, current_loc, charge_loc, dummy_payload
                )
                if plan is None:
                    continue
                finish = plan["final_finish"]
            if finish < best_finish:
                best_finish = finish
                best_loc = charge_loc
        return best_loc

    def _apply_lift_journey_wear(
        self,
        lift: Lift,
        journey_operating_sec: float = 0.0,
        journey_finish_time: Optional[float] = None,
    ) -> None:
        lift.apply_journey_wear()
        lift.operating_time_since_failure_sec += max(
            0.0, float(journey_operating_sec or 0.0)
        )

        health_factor = max(0.1, min(1.0, float(getattr(lift, "health_percent", 100.0) or 0.0) / 100.0))
        mtbf_sec = (
            max(0.0, float(lift.mean_time_between_failures_hours or 0.0)) * 3600.0 * health_factor
        )
        if mtbf_sec <= 0.0:
            return
        if lift.operating_time_since_failure_sec < mtbf_sec:
            return

        repair_sec = max(0.0, float(lift.mean_time_to_repair_hours or 0.0)) * 3600.0
        start_repair = max(
            float(lift.available_time),
            float(journey_finish_time or lift.available_time),
        )
        lift.failed_until = start_repair + repair_sec
        lift.available_time = max(lift.available_time, lift.failed_until)
        lift.failures_count += 1
        lift.operating_time_since_failure_sec = 0.0

    def _plan_return_to_charge(
        self,
        amr: AMR,
        current_loc: Location,
        current_time_value: float,
        reserve: bool = False,
    ) -> Optional[dict]:
        charge_loc = self._select_charge_location_for_amr(
            amr, current_loc, current_time_value
        )
        if charge_loc is None:
            return None
        # Keep the selected charger on the returned plan rather than mutating
        # the legacy global charge_location_name.  Multiple AMRs can plan/charge
        # concurrently at different configured charging locations; a single
        # mutable global makes later events appear to use AMR-CENTRE or whichever
        # charger was selected most recently.
        setattr(amr, "target_charge_location", charge_loc.name)

        reserved_charge_space = None
        if reserve:
            reserved_charge_space = self._reserve_amr_inventory_space(
                amr, charge_loc.name, require_charger=True
            )
            if reserved_charge_space is None and any(
                self._inventory_space_accepts_amr(space, amr)
                for space in self.inventory_spaces_by_location.get(charge_loc.name, [])
            ):
                return None

        if current_loc.floor == charge_loc.floor:
            route = self._same_floor_segments(
                amr, current_loc, charge_loc, start_time_value=current_time_value,
                payload=self.payloads.get(EMPTY_PAYLOAD_NAME), orientation="lengthways"
            )
            if route is None:
                return None
            segments, travel_sec, distance_m = route
            finish_time = current_time_value + travel_sec

            if reserve:
                amr.location_name = charge_loc.name

            return {
                "segments": segments,
                "travel_sec": travel_sec,
                "distance_m": distance_m,
                "finish_time": finish_time,
                "end_location": charge_loc.name,
                "charge_location": charge_loc.name,
                "amr_inventory_space": str(
                    (reserved_charge_space or {}).get("name", "") or ""
                ),
            }

        dummy_payload = self.payloads.get(EMPTY_PAYLOAD_NAME)
        if dummy_payload is None:
            dummy_payload = PayloadType("empty", 0.0)
        plan = self._nearest_compatible_lift_plan(
            current_time_value, amr, current_loc, charge_loc, dummy_payload
        )
        if plan is None:
            return None

        transfer_segments = list(plan["to_lift_segments"])

        if plan["wait_time"] > 0:
            transfer_segments.append(
                {
                    "type": "wait_for_lift",
                    "lift_id": plan["lift"].id,
                    "from": plan["origin_lift"].name,
                    "to": plan["origin_lift"].name,
                    "duration": plan["wait_time"],
                    "distance_m": 0.0,
                }
            )

        if plan.get("reposition_sec", 0.0) > 0:
            transfer_segments.append(
                {
                    "type": "lift_reposition",
                    "lift_id": plan["lift"].id,
                    "from": f"{plan['lift'].id}-F{plan['reposition_from_floor']}",
                    "to": f"{plan['lift'].id}-F{plan['reposition_to_floor']}",
                    "from_floor": plan["reposition_from_floor"],
                    "to_floor": plan["reposition_to_floor"],
                    "wait_time": 0.0,
                    "duration": plan["reposition_sec"],
                    "distance_m": abs(
                        plan["reposition_to_floor"] - plan["reposition_from_floor"]
                    )
                    * self.floor_height_m,
                    "vertical_distance_m": abs(
                        plan["reposition_to_floor"] - plan["reposition_from_floor"]
                    )
                    * self.floor_height_m,
                }
            )

        transfer_segments.append(
            {
                "type": "lift_transfer",
                "lift_id": plan["lift"].id,
                "from": plan["origin_lift"].name,
                "to": plan["destination_lift"].name,
                "from_floor": current_loc.floor,
                "to_floor": charge_loc.floor,
                "wait_time": 0.0,
                "duration": max(
                    0.0,
                    plan["lift_finish"]
                    - plan.get(
                        "reposition_finish",
                        plan["lift_start"] + plan.get("reposition_sec", 0.0),
                    ),
                ),
                "distance_m": plan["vertical_distance_m"],
                "vertical_distance_m": plan["vertical_distance_m"],
            }
        )

        transfer_segments.extend(plan["from_lift_segments"])

        if reserve:
            self._reserve_lift_journey(plan, amr.id)
            self._apply_lift_journey_wear(
                plan["lift"],
                journey_operating_sec=float(plan.get("reposition_sec", 0.0))
                + float(plan.get("loaded_travel_sec", 0.0)),
                journey_finish_time=plan["lift_finish"],
            )
            amr.location_name = charge_loc.name

        return {
            "segments": transfer_segments,
            "travel_sec": plan["final_finish"] - current_time_value,
            "distance_m": (
                plan["to_lift_distance_m"]
                + plan["vertical_distance_m"]
                + plan["from_lift_distance_m"]
            ),
            "finish_time": plan["final_finish"],
            "end_location": charge_loc.name,
            "charge_location": charge_loc.name,
            "amr_inventory_space": str(
                (reserved_charge_space or {}).get("name", "") or ""
            ),
        }

    def _amr_is_at_operational_charger(self, amr: AMR) -> bool:
        location_name = str(getattr(amr, "location_name", "") or "").strip()
        if location_name not in self.charge_location_names:
            return False
        spaces = self.inventory_spaces_by_location.get(location_name, []) or []
        amr_spaces = [
            space for space in spaces
            if bool(space.get("stores_amr", False))
            or str(space.get("space_type", "") or "").strip().lower() == "amr"
        ]
        # Preserve legacy location-level charging only where no AMR bays exist.
        if not amr_spaces:
            return True
        occupied_name = str(getattr(amr, "inventory_space_name", "") or "").strip()
        return any(
            self._space_name(space) == occupied_name
            and self._inventory_space_accepts_amr(space, amr)
            and self._inventory_space_has_charger(space)
            for space in amr_spaces
        )

    def _plan_charge_cycle_if_needed(
        self,
        amr: AMR,
        payload: PayloadType,
        to_pickup_sec: float,
        loaded_sec: float,
        ready_time: float,
    ) -> Tuple[float, List[dict], float]:
        required_energy_kwh = total_route_energy_kwh(
            amr, payload, to_pickup_sec, loaded_sec
        )
        extra_segments: List[dict] = []
        adjusted_ready_time = ready_time

        if requires_recharge_before_route(amr, required_energy_kwh):
            # Never charge on an arbitrary corridor/location node.  In-place
            # pre-route charging is valid only when the AMR is actually parked
            # in a charger-equipped bay (or at a legacy charger location with no
            # explicit AMR bays).  Otherwise the assignment is deferred and the
            # scheduler sends the AMR to a charger.
            if self._amr_is_at_operational_charger(amr):
                charge_duration = amr.charge_duration_sec_to_full()
                extra_segments.append(
                    {
                        "type": "charge",
                        "location": amr.location_name,
                        "duration": charge_duration,
                        "battery_soc_before": amr.battery_soc_percent,
                        "battery_soc_after": 100.0,
                        "charger_space": str(getattr(amr, "inventory_space_name", "") or ""),
                    }
                )
                adjusted_ready_time += charge_duration

        return adjusted_ready_time, extra_segments, required_energy_kwh

    def _needs_post_task_recharge(self, amr: AMR) -> bool:
        return amr.battery_energy_kwh() < amr.min_reserve_energy_kwh()

    def _create_wait_event_for_pending_tasks(self, now: float):
        if any(e.event_type == "task_wait" and e.time > now for e in self.events):
            return

        if not self.pending_tasks:
            return

        next_times = []

        for amr in self.amrs:
            next_times.append(amr.available_time)

        if self.events:
            next_event_time = self.events[0].time
            if next_event_time > now:
                next_times.append(next_event_time)

        future_times = [t for t in next_times if t > now]
        if not future_times:
            return

        wait_until = min(future_times)

        self.push_event(
            wait_until,
            "task_wait",
            {
                "start_time": now,
                "end_time": wait_until,
                "pending_task_ids": [
                    task.id
                    for _, _, _, task in self.pending_tasks
                    if not self._pending_task_removed(task)
                ],
                "reason": "No AMRs currently available",
            },
        )

    def _schedule_charge_cycle(self, amr: AMR, now: float) -> bool:
        if getattr(amr, "is_charging", False):
            return True

        current_loc = self.locations[amr.location_name]
        self._free_amr_inventory_space(amr)
        plan = self._plan_return_to_charge(amr, current_loc, now, reserve=True)
        if plan is None:
            self.failed_tasks.append(
                {
                    "task_id": f"CHARGE-{amr.id}",
                    "reason": f"No route to charge location for {amr.id}",
                }
            )
            return False

        charge_duration = amr.charge_duration_sec_to_full()
        charge_start = plan["finish_time"]
        charge_finish = charge_start + charge_duration
        self.charge_intervals.append({
            "amr_id": amr.id,
            "location": plan.get("charge_location", plan.get("end_location", "")),
            "space": plan.get("amr_inventory_space", ""),
            "start_time": charge_start,
            "end_time": charge_finish,
            "duration_sec": charge_duration,
        })

        amr.is_charging = True
        amr.available_time = charge_finish
        amr.location_name = plan["end_location"]

        self.push_event(
            now,
            "charge_cycle_start",
            {
                "amr_id": amr.id,
                "travel_segments": plan["segments"],
                "travel_finish": plan["finish_time"],
                "charge_start": charge_start,
                "charge_finish": charge_finish,
                "charge_duration": charge_duration,
                "charge_location": plan.get(
                    "charge_location", plan.get("end_location", amr.location_name)
                ),
                "amr_inventory_space": plan.get("amr_inventory_space", ""),
            },
        )

        self.push_event(
            charge_finish,
            "charge_cycle_complete",
            {
                "amr_id": amr.id,
                "charge_duration": charge_duration,
                "charge_location": plan.get(
                    "charge_location", plan.get("end_location", amr.location_name)
                ),
                "amr_inventory_space": plan.get("amr_inventory_space", ""),
            },
        )
        return True

    def _schedule_recharge_for_amr(self, amr: AMR, now: float):
        current_loc = self.locations[amr.location_name]
        self._free_amr_inventory_space(amr)
        charge_plan = self._plan_return_to_charge(
            amr,
            current_loc,
            now,
            reserve=True,
        )

        if charge_plan is None:
            self.failed_tasks.append(
                {
                    "task_id": f"RECHARGE-{amr.id}",
                    "reason": f"Could not route {amr.id} to charge location",
                }
            )
            return

        charge_duration = amr.charge_duration_sec_to_full()
        charge_start = charge_plan["finish_time"]
        charge_finish = charge_start + charge_duration

        amr.available_time = charge_finish
        amr.total_busy_time += charge_plan["travel_sec"] + charge_duration

        self.push_event(
            now,
            "recharge_start",
            {
                "amr_id": amr.id,
                "segments": charge_plan["segments"],
                "start_time": now,
                "arrival_time": charge_plan["finish_time"],
                "charge_start": charge_start,
                "charge_finish": charge_finish,
            },
        )

        self.push_event(
            charge_finish,
            "recharge_complete",
            {
                "amr_id": amr.id,
                "finish_time": charge_finish,
            },
        )

    def _estimate_task_for_amr(self, amr: AMR, task: Task, reserve: bool = False):
        try:
            availability_time = max(self.current_time, float(getattr(amr, "available_time", 0.0) or 0.0), float(getattr(task, "release_time", 0.0) or 0.0))
            amr_scenario_state = self._scenario_event_state("amr", amr.id, availability_time)
            if float(amr_scenario_state.get("availability_percent", 100.0)) <= 0.0:
                self._set_task_pending_reason(task, f"AMR {amr.id} unavailable in scenario {self.scenario_name}")
                return None
            if getattr(task, "is_idle_return", False):
                if getattr(task, "amr_id", "") != amr.id:
                    return None
                if reserve:
                    selected_location, _selected_space = (
                        self._reserve_best_idle_return_destination(
                            amr, task, max(self.current_time, task.release_time)
                        )
                    )
                    if not selected_location:
                        self._set_task_pending_reason(
                            task,
                            f"No compatible AMR space available at any configured charging location for {amr.id}",
                        )
                        return None
                else:
                    current_loc = self.locations.get(
                        str(getattr(amr, "location_name", "") or "").strip()
                    )
                    selected = (
                        self._select_charge_location_for_amr(
                            amr, current_loc, max(self.current_time, task.release_time)
                        )
                        if current_loc is not None
                        else None
                    )
                    if selected is None:
                        self._set_task_pending_reason(
                            task,
                            f"No compatible AMR space available at any configured charging location for {amr.id}",
                        )
                        return None
                    task.pickup = str(getattr(amr, "location_name", "") or "").strip()
                    task.dropoff = selected.name
                    task.target_charge_location = selected.name
            if task.pickup not in self.locations or task.dropoff not in self.locations:
                return None

            payload = self._payload_for_task(task)
            if payload is None:
                return None

            if not self._pickup_instance_available(task):
                self._set_task_pending_reason(
                    task, self._pickup_instance_pending_reason(task)
                )
                return None
            if not self._amr_can_carry_payload(amr, payload):
                self._set_task_pending_reason(
                    task, "No AMR has sufficient payload weight/dimensions/orientation"
                )
                return None
            payload_slot_name, payload_orientation = self._choose_payload_orientation(amr, payload)
            if not is_empty_payload_name(payload.name) and not payload_slot_name:
                self._set_task_pending_reason(task, "No compatible AMR payload orientation is available")
                return None
            task.payload_orientation = payload_orientation or "lengthways"

            if self._location_has_payload_inventory_spaces(task.dropoff):
                free_space = self._find_free_inventory_space(task.dropoff, payload)
                if free_space is None and not self._task_can_exchange_with_store_empty(
                    task, payload
                ):
                    self._set_task_pending_reason(
                        task,
                        self._inventory_pending_reason(task.dropoff, payload),
                    )
                    return None

            amr_loc = self.locations[amr.location_name]
            pickup_loc = self.locations[task.pickup]
            dropoff_loc = self.locations[task.dropoff]

            lift_energy_kwh_total = 0.0

            # No restrictions before pickup
            pre_pickup_rules = None

            # Apply route profile only once the load has been picked up
            loaded_route_rules = self._resolve_task_route_rules(task)

            to_pickup_est = (
                self._same_floor_segments(
                    amr, amr_loc, pickup_loc, rules=pre_pickup_rules,
                    payload=self.payloads.get(EMPTY_PAYLOAD_NAME), orientation="lengthways"
                )
                if amr_loc.floor == pickup_loc.floor
                else None
            )
            loaded_est = (
                self._same_floor_segments(
                    amr, pickup_loc, dropoff_loc, rules=loaded_route_rules,
                    payload=payload, orientation=task.payload_orientation
                )
                if pickup_loc.floor == dropoff_loc.floor
                else None
            )
            to_pickup_sec = to_pickup_est[1] if to_pickup_est else 0.0
            loaded_sec = loaded_est[1] if loaded_est else 0.0

            t = max(self.current_time, amr.available_time, task.release_time)
            charge_ready_time, charge_segments, required_route_energy_kwh = self._plan_charge_cycle_if_needed(
                amr, payload, to_pickup_sec, loaded_sec, t
            )
            if requires_recharge_before_route(amr, required_route_energy_kwh) and not charge_segments:
                self._set_task_pending_reason(
                    task,
                    f"AMR {amr.id} requires a charger-equipped bay before this route",
                )
                return None
            t = charge_ready_time
            task_start_time = t

            total = sum(seg["duration"] for seg in charge_segments)
            segments = list(charge_segments)
            current_location = amr_loc

            departure_space_name = str(
                getattr(amr, "inventory_space_name", "") or ""
            ).strip()
            if departure_space_name:
                departure_segments, departure_duration, _departure_distance = (
                    self._local_manoeuvre_segments_from_inventory_space(
                        amr, amr_loc.name, departure_space_name, t, purpose="amr_unstow"
                    )
                )
                if departure_segments:
                    segments.extend(departure_segments)
                    t += departure_duration
                    total += departure_duration

            lift_empty_sec_total = 0.0
            lift_loaded_sec_total = 0.0

            def move_between(
                location_a: Location,
                location_b: Location,
                current_time_value: float,
                rules: Optional[dict] = None,
                payload_for_leg: Optional[PayloadType] = None,
                orientation_for_leg: str = "lengthways",
            ) -> Tuple[float, Location, Optional[List[dict]], float]:
                nonlocal total

                if location_a.floor == location_b.floor:
                    route = self._same_floor_segments(
                        amr,
                        location_a,
                        location_b,
                        rules=rules,
                        start_time_value=current_time_value,
                        payload=payload_for_leg,
                        orientation=orientation_for_leg,
                    )
                    if route is None:
                        return math.inf, location_b, None, 0.0

                    same_segments, route_duration, _ = route
                    total += route_duration

                    if reserve:
                        self._reserve_corridor_segments(
                            amr=amr,
                            segments=same_segments,
                            start_time=current_time_value,
                        )

                    return (
                        current_time_value + route_duration,
                        location_b,
                        same_segments,
                        route_duration,
                    )

                plan = self._nearest_compatible_lift_plan(
                    current_time_value,
                    amr,
                    location_a,
                    location_b,
                    payload_for_leg or self.payloads.get(EMPTY_PAYLOAD_NAME, payload),
                    rules=rules,
                    orientation=orientation_for_leg,
                )
                if plan is None:
                    return math.inf, location_b, None, 0.0

                nonlocal lift_energy_kwh_total
                lift_energy_kwh_total += total_lift_energy_kwh(
                    lift=plan["lift"],
                    payload=payload_for_leg or self.payloads.get(EMPTY_PAYLOAD_NAME, payload),
                    floor_height_m=self.floor_height_m,
                    reposition_floor_delta=(
                        plan["reposition_to_floor"] - plan["reposition_from_floor"]
                    ),
                    loaded_floor_delta=(location_b.floor - location_a.floor),
                    wait_time_sec=plan["wait_time"],
                    door_time_sec=plan["lift"].door_time_sec,
                )

                segment_duration = plan["final_finish"] - current_time_value
                total += segment_duration

                nonlocal lift_empty_sec_total, lift_loaded_sec_total
                lift_empty_sec_total += float(plan.get("reposition_sec", 0.0))
                lift_loaded_sec_total += float(plan.get("loaded_travel_sec", 0.0))

                if reserve:
                    self._reserve_corridor_segments(
                        amr=amr,
                        segments=plan["to_lift_segments"],
                        start_time=current_time_value,
                    )
                    self._reserve_corridor_segments(
                        amr=amr,
                        segments=plan["from_lift_segments"],
                        start_time=plan["lift_finish"],
                    )
                    self._reserve_lift_journey(plan, amr.id)
                    self._apply_lift_journey_wear(
                        plan["lift"],
                        journey_operating_sec=float(plan.get("reposition_sec", 0.0))
                        + float(plan.get("loaded_travel_sec", 0.0)),
                        journey_finish_time=plan["lift_finish"],
                    )

                transfer_segments = list(plan["to_lift_segments"])

                if plan["wait_time"] > 0:
                    transfer_segments.append(
                        {
                            "type": "wait_for_lift",
                            "lift_id": plan["lift"].id,
                            "from": plan["origin_lift"].name,
                            "to": plan["origin_lift"].name,
                            "duration": plan["wait_time"],
                            "distance_m": 0.0,
                        }
                    )

                if plan.get("reposition_sec", 0.0) > 0:
                    transfer_segments.append(
                        {
                            "type": "lift_reposition",
                            "lift_id": plan["lift"].id,
                            "from": f"{plan['lift'].id}-F{plan['reposition_from_floor']}",
                            "to": f"{plan['lift'].id}-F{plan['reposition_to_floor']}",
                            "amr_wait_node": plan["origin_lift"].name,
                            "from_floor": plan["reposition_from_floor"],
                            "to_floor": plan["reposition_to_floor"],
                            "wait_time": 0.0,
                            "duration": plan["reposition_sec"],
                            "distance_m": abs(
                                plan["reposition_to_floor"]
                                - plan["reposition_from_floor"]
                            )
                            * self.floor_height_m,
                            "vertical_distance_m": abs(
                                plan["reposition_to_floor"]
                                - plan["reposition_from_floor"]
                            )
                            * self.floor_height_m,
                            "energy_kwh": total_lift_energy_kwh(
                                lift=plan["lift"],
                                payload=payload_for_leg or self.payloads.get(EMPTY_PAYLOAD_NAME, payload),
                                floor_height_m=self.floor_height_m,
                                reposition_floor_delta=(
                                    plan["reposition_to_floor"]
                                    - plan["reposition_from_floor"]
                                ),
                                loaded_floor_delta=0,
                                wait_time_sec=0.0,
                                door_time_sec=0.0,
                            ),
                        }
                    )

                transfer_segments.append(
                    {
                        "type": "lift_transfer",
                        "lift_id": plan["lift"].id,
                        "from": plan["origin_lift"].name,
                        "to": plan["destination_lift"].name,
                        "from_floor": location_a.floor,
                        "to_floor": location_b.floor,
                        "wait_time": 0.0,
                        "duration": max(
                            0.0,
                            plan["lift_finish"]
                            - plan.get(
                                "reposition_finish",
                                plan["lift_start"] + plan.get("reposition_sec", 0.0),
                            ),
                        ),
                        "distance_m": plan["vertical_distance_m"],
                        "vertical_distance_m": plan["vertical_distance_m"],
                        "energy_kwh": total_lift_energy_kwh(
                            lift=plan["lift"],
                            payload=payload,
                            floor_height_m=self.floor_height_m,
                            reposition_floor_delta=0,
                            loaded_floor_delta=(location_b.floor - location_a.floor),
                            wait_time_sec=plan["wait_time"],
                            door_time_sec=plan["lift"].door_time_sec,
                        ),
                    }
                )

                transfer_segments.extend(plan["from_lift_segments"])

                return (
                    plan["final_finish"],
                    location_b,
                    transfer_segments,
                    segment_duration,
                )

            travel_to_pickup_sec = 0.0
            t, current_location, new_segments, seg_time = move_between(
                current_location, pickup_loc, t, rules=pre_pickup_rules,
                payload_for_leg=self.payloads.get(EMPTY_PAYLOAD_NAME), orientation_for_leg="lengthways"
            )
            if new_segments is None or math.isinf(t):
                return None
            travel_to_pickup_sec += seg_time
            segments.extend(new_segments)

            pickup_start = self._find_next_available_time(
                pickup_loc.name,
                t,
                self.load_unload_time_sec,
            )
            pickup_wait = pickup_start - t
            if pickup_wait > 0:
                segments.append(
                    {
                        "type": "wait_for_location",
                        "from": pickup_loc.name,
                        "to": pickup_loc.name,
                        "duration": pickup_wait,
                        "distance_m": 0.0,
                        "location": pickup_loc.name,
                    }
                )
                total += pickup_wait
                t = pickup_start

            if reserve:
                self._reserve_location(
                    pickup_loc.name,
                    t,
                    t + self.load_unload_time_sec,
                )

            t += self.load_unload_time_sec
            total += self.load_unload_time_sec
            segments.append(
                {
                    "type": "pickup",
                    "location": pickup_loc.name,
                    "duration": self.load_unload_time_sec,
                }
            )

            loaded_travel_sec = 0.0
            t, current_location, new_segments, seg_time = move_between(
                current_location, dropoff_loc, t, rules=loaded_route_rules,
                payload_for_leg=payload, orientation_for_leg=task.payload_orientation
            )
            if new_segments is None or math.isinf(t):
                return None
            loaded_travel_sec += seg_time
            segments.extend(new_segments)

            dropoff_start = self._find_next_available_time(
                dropoff_loc.name,
                t,
                self.load_unload_time_sec,
            )
            dropoff_wait = dropoff_start - t
            if dropoff_wait > 0:
                segments.append(
                    {
                        "type": "wait_for_location",
                        "from": dropoff_loc.name,
                        "to": dropoff_loc.name,
                        "duration": dropoff_wait,
                        "distance_m": 0.0,
                        "location": dropoff_loc.name,
                    }
                )
                total += dropoff_wait
                t = dropoff_start

            inventory_space_name = ""
            reserved_space = None
            if reserve:
                self._reserve_location(
                    dropoff_loc.name,
                    t,
                    t + self.load_unload_time_sec,
                )
                if getattr(task, "is_idle_return", False):
                    reserved_space = self._reserved_amr_inventory_space(
                        dropoff_loc.name, amr
                    )
                    if reserved_space is None:
                        reserved_space = self._reserve_amr_inventory_space(
                            amr, dropoff_loc.name
                        )
                    if (
                        reserved_space is None
                        and self._location_has_any_amr_inventory_spaces(
                            dropoff_loc.name
                        )
                    ):
                        self._set_task_pending_reason(
                            task,
                            f"No compatible AMR space available at {dropoff_loc.name} for {amr.id}",
                        )
                        return None
                else:
                    reserved_space = self._reserve_inventory_space_for_task(
                        task, payload
                    )
                    if (
                        self._location_has_payload_inventory_spaces(dropoff_loc.name)
                        and reserved_space is None
                        and not self._task_can_exchange_with_store_empty(task, payload)
                    ):
                        self._set_task_pending_reason(
                            task,
                            self._inventory_pending_reason(dropoff_loc.name, payload),
                        )
                        return None
                if reserved_space is not None:
                    inventory_space_name = str(reserved_space.get("name", ""))
                    local_segments, local_duration, _local_distance = (
                        self._local_manoeuvre_segments_to_inventory_space(
                            amr,
                            dropoff_loc.name,
                            reserved_space,
                            t,
                            purpose=(
                                "amr_stow"
                                if getattr(task, "is_idle_return", False)
                                else "payload_dropoff"
                            ),
                        )
                    )
                    if local_segments:
                        for local_segment in local_segments:
                            local_segment.setdefault("task_id", task.id)
                            local_segment.setdefault("task_ids", [task.id])
                            local_segment.setdefault(
                                "payload", getattr(task, "payload", "")
                            )
                            local_segment.setdefault(
                                "payload_instance_id",
                                getattr(task, "payload_instance_id", ""),
                            )
                        segments.extend(local_segments)
                        t += local_duration
                        total += local_duration
                        loaded_travel_sec += local_duration

            t += self.load_unload_time_sec
            total += self.load_unload_time_sec
            segments.append(
                {
                    "type": "dropoff",
                    "location": dropoff_loc.name,
                    "duration": self.load_unload_time_sec,
                    "inventory_space": inventory_space_name
                    or getattr(task, "assigned_inventory_space", ""),
                }
            )

            wash_trigger = None
            for candidate in (pickup_loc, dropoff_loc):
                if bool(getattr(candidate, "wash_cycle_required", False)):
                    wash_trigger = candidate
            if wash_trigger is not None and not getattr(task, "is_idle_return", False):
                task.wash_cycle_required = True
                wash_target_name = str(getattr(wash_trigger, "wash_location", "") or "").strip()
                wash_target = self.locations.get(wash_target_name) or dropoff_loc
                if wash_target.name != current_location.name:
                    t, current_location, wash_travel_segments, wash_travel_sec = move_between(
                        current_location, wash_target, t, rules=None,
                        payload_for_leg=self.payloads.get(EMPTY_PAYLOAD_NAME),
                        orientation_for_leg="lengthways",
                    )
                    if wash_travel_segments is None or math.isinf(t):
                        self._set_task_pending_reason(task, f"No route to wash location {wash_target.name}")
                        return None
                    for wash_segment in wash_travel_segments:
                        wash_segment.setdefault("wash_transfer", True)
                    segments.extend(wash_travel_segments)
                    travel_to_pickup_sec += wash_travel_sec
                wash_duration = max(0.0, float(getattr(wash_trigger, "wash_cycle_duration_sec", 0.0) or 0.0))
                if wash_duration > 0.0:
                    segments.append({
                        "type": "wash_cycle", "from": wash_target.name, "to": wash_target.name,
                        "location": wash_target.name, "duration": wash_duration, "distance_m": 0.0,
                        "wash_trigger_location": wash_trigger.name,
                    })
                    total += wash_duration
                    t += wash_duration
                end_location_name = wash_target.name
            else:
                end_location_name = dropoff_loc.name

            corridor_energy_kwh = total_route_energy_kwh(
                amr, payload, travel_to_pickup_sec, loaded_travel_sec
            )

            lift_energy_kwh = lift_energy_kwh_total

            actual_energy_kwh = corridor_energy_kwh + lift_energy_kwh

            projected_battery_soc_after = (
                100.0
                * max(0.0, amr.battery_energy_kwh() - actual_energy_kwh)
                / max(amr.battery_capacity_kwh, 1e-9)
            )

            if reserve:
                if charge_segments:
                    charge_duration = float(charge_segments[0]["duration"] or 0.0)
                    charge_start = float(task_start_time) - charge_duration
                    self.charge_intervals.append({
                        "amr_id": amr.id,
                        "location": str(amr.location_name or ""),
                        "space": str(getattr(amr, "inventory_space_name", "") or ""),
                        "start_time": charge_start,
                        "end_time": float(task_start_time),
                        "duration_sec": charge_duration,
                    })
                    amr.total_charge_time += charge_duration
                    amr.charge_to_full()

                amr.consume_energy(actual_energy_kwh)
                battery_soc_after = amr.battery_soc_percent
            else:
                battery_soc_after = projected_battery_soc_after

            if reserve:
                self._record_committed_segment_impacts(segments)
            return {
                "task_start_time": task_start_time,
                "finish_time": t,
                "duration": total,
                "segments": segments,
                "end_location": end_location_name,
                "energy_kwh": actual_energy_kwh,
                "battery_soc_after": battery_soc_after,
                "corridor_energy_kwh": corridor_energy_kwh,
                "lift_energy_kwh": lift_energy_kwh,
                "lift_empty_sec_total": lift_empty_sec_total,
                "lift_loaded_sec_total": lift_loaded_sec_total,
                "amr_inventory_space": inventory_space_name,
                "payload_slot": payload_slot_name,
                "payload_orientation": task.payload_orientation,
            }
        except Exception as exc:
            print(f"_estimate_task_for_amr failed for {task.id} on {amr.id}: {exc}")
            return None

    def _route_estimate_time_bucket(self, value: float) -> int:
        bucket = max(
            1.0, float(getattr(self, "route_estimate_time_bucket_sec", 30.0) or 30.0)
        )
        return int(float(value or 0.0) // bucket)

    def _route_estimate_cache_key(self, amr: AMR, task: Task) -> tuple:
        payload = self._payload_for_task(task)
        payload_name = getattr(payload, "name", str(getattr(task, "payload", "") or ""))
        rules = self._resolve_task_route_rules(task) or {}
        rules_key = self._rules_cache_key(rules)
        return (
            int(getattr(self, "route_estimate_cache_version", 0)),
            self._route_estimate_time_bucket(
                max(
                    self.current_time,
                    getattr(amr, "available_time", 0.0),
                    getattr(task, "release_time", 0.0),
                )
            ),
            str(getattr(amr, "id", "")),
            str(getattr(amr, "location_name", "")),
            self._route_estimate_time_bucket(getattr(amr, "available_time", 0.0)),
            round(float(getattr(amr, "battery_soc_percent", 0.0) or 0.0), 2),
            str(getattr(task, "id", "")),
            str(getattr(task, "pickup", "")),
            str(getattr(task, "dropoff", "")),
            str(payload_name),
            self._route_estimate_time_bucket(getattr(task, "release_time", 0.0)),
            bool(getattr(task, "is_return_task", False)),
            bool(getattr(task, "is_idle_return", False)),
            str(getattr(task, "payload_instance_id", "") or ""),
            rules_key,
        )

    def _get_cached_task_estimate(self, amr: AMR, task: Task) -> Optional[dict]:
        return self.route_estimate_cache.get(self._route_estimate_cache_key(amr, task))

    def _set_cached_task_estimate(
        self, amr: AMR, task: Task, estimate: Optional[dict]
    ) -> None:
        max_entries = int(getattr(self, "route_estimate_cache_max_entries", 0) or 0)
        if max_entries <= 0:
            return
        if len(self.route_estimate_cache) >= max_entries:
            self.route_estimate_cache.clear()
        self.route_estimate_cache[self._route_estimate_cache_key(amr, task)] = estimate

    def _invalidate_route_estimate_cache(self) -> None:
        self.route_estimate_cache_version += 1
        self.route_estimate_cache.clear()

    def _estimate_task_for_amr_cached(
        self, amr: AMR, task: Task, reserve: bool = False
    ):
        if reserve:
            return self._estimate_task_for_amr(amr, task, reserve=True)
        key = self._route_estimate_cache_key(amr, task)
        if key in self.route_estimate_cache:
            return self.route_estimate_cache[key]
        estimate = self._estimate_task_for_amr(amr, task, reserve=False)
        self._set_cached_task_estimate(amr, task, estimate)
        return estimate

    def _estimate_candidate_jobs(
        self, jobs: List[dict]
    ) -> List[Tuple[dict, Optional[dict]]]:
        """Run independent, read-only route estimates.

        Only reserve=False estimation jobs are submitted here.  The event loop and
        all reservation/commit updates remain single-threaded so that simulation
        ordering stays deterministic.
        """
        if not jobs:
            return []

        if self.routing_executor is None or len(jobs) < int(
            getattr(self, "parallel_routing_min_jobs", 64) or 64
        ):
            results = []
            for job in jobs:
                try:
                    results.append((job, job["fn"](*job.get("args", ()))))
                except Exception as exc:
                    print(f"Route estimate job failed: {exc}")
                    results.append((job, None))
            return results

        futures = {
            self.routing_executor.submit(job["fn"], *job.get("args", ())): job
            for job in jobs
        }
        results = []
        for future in as_completed(futures):
            job = futures[future]
            try:
                estimate = future.result()
            except Exception as exc:
                print(f"Parallel estimate job failed: {exc}")
                estimate = None
            results.append((job, estimate))
        return results

    def _task_allowed_for_amr(self, task: Task, amr: AMR) -> bool:
        locked_amr_id = str(getattr(task, "locked_amr_id", "") or "").strip()
        if locked_amr_id and str(getattr(amr, "id", "") or "").strip() != locked_amr_id:
            return False
        return True

    def _select_best_assignment(self) -> Optional[Tuple[AMR, Task, dict]]:
        if not self.pending_tasks:
            return None

        multi_stop_jobs = []
        for order, amr in enumerate(self.amrs):
            if getattr(amr, "is_charging", False):
                continue
            if self._needs_post_task_recharge(amr):
                continue
            batch = self._multi_stop_batch_for_amr(amr)
            if not batch:
                continue
            multi_stop_jobs.append(
                {
                    "order": order,
                    "amr": amr,
                    "task_or_tasks": batch,
                    "fn": self._estimate_multi_stop_for_amr,
                    "args": (amr, batch, False),
                }
            )

        multi_stop_best = None
        multi_stop_best_finish = math.inf
        multi_stop_best_order = math.inf
        for job, estimate in self._estimate_candidate_jobs(multi_stop_jobs):
            if estimate is None:
                continue
            finish_time = estimate["finish_time"]
            order = int(job.get("order", 0))
            if (finish_time, order) < (multi_stop_best_finish, multi_stop_best_order):
                multi_stop_best_finish = finish_time
                multi_stop_best_order = order
                multi_stop_best = (job["amr"], job["task_or_tasks"], estimate)

        # Multi-stop remains a route-shape preference.  If a feasible batch exists,
        # commit it before comparing against single-task assignments.
        if multi_stop_best is not None:
            return multi_stop_best

        candidate_tasks = []
        for item in self.pending_tasks:
            if self._pending_task_removed(item[3]):
                continue
            candidate_tasks.append(item)
            if len(candidate_tasks) >= self.max_single_candidate_tasks:
                break

        single_jobs = []
        for task_order, (_, _, _, task) in enumerate(candidate_tasks):
            if task.release_time > self.current_time:
                self._set_task_pending_reason(task, "Waiting for release time")
                continue

            payload_for_inventory = self._payload_for_task(task)
            if (
                payload_for_inventory is not None
                and self._location_has_payload_inventory_spaces(task.dropoff)
            ):
                payload = payload_for_inventory
                if self._find_free_inventory_space(
                    task.dropoff, payload
                ) is None and not self._task_can_exchange_with_store_empty(
                    task, payload
                ):
                    self._set_task_pending_reason(
                        task,
                        self._inventory_pending_reason(task.dropoff, payload),
                    )
                    continue

            task_prefers_multi_stop = self._task_prefers_multi_stop_amr(task)
            for amr_order, amr in enumerate(self.amrs):
                if not self._task_allowed_for_amr(task, amr):
                    continue
                if getattr(amr, "is_charging", False):
                    continue
                if self._needs_post_task_recharge(amr):
                    continue
                single_jobs.append(
                    {
                        "order": (task_order, amr_order),
                        "amr": amr,
                        "task_or_tasks": task,
                        "task_prefers_multi_stop": task_prefers_multi_stop,
                        "is_preferred_multi_stop_amr": task_prefers_multi_stop
                        and self._is_multi_stop_amr(amr),
                        "fn": self._estimate_task_for_amr_cached,
                        "args": (amr, task, False),
                    }
                )

        best_by_task = {}
        preferred_by_task = {}

        for job, estimate in self._estimate_candidate_jobs(single_jobs):
            if estimate is None:
                continue

            task = job["task_or_tasks"]
            task_key = str(getattr(task, "id", ""))
            finish_time = estimate["finish_time"]
            order_tuple = job.get("order", (0, 0))
            flat_order = (order_tuple[0] * max(len(self.amrs), 1)) + order_tuple[1]
            candidate = (finish_time, flat_order, job["amr"], task, estimate)

            current = best_by_task.get(task_key)
            if current is None or candidate[:2] < current[:2]:
                best_by_task[task_key] = candidate

            if bool(job.get("is_preferred_multi_stop_amr", False)):
                current = preferred_by_task.get(task_key)
                if current is None or candidate[:2] < current[:2]:
                    preferred_by_task[task_key] = candidate

        best = None
        best_finish = math.inf
        best_order = math.inf

        for _priority, _release, _counter, task in candidate_tasks:
            task_key = str(getattr(task, "id", ""))
            chosen = preferred_by_task.get(task_key) or best_by_task.get(task_key)
            if chosen is None:
                continue
            chosen_finish, chosen_order, amr, task, estimate = chosen
            if (chosen_finish, chosen_order) < (best_finish, best_order):
                best_finish = chosen_finish
                best_order = chosen_order
                best = (amr, task, estimate)

        return best

    def _route_possible_between_locations(
        self,
        amr: AMR,
        from_loc: Location,
        to_loc: Location,
        payload: PayloadType,
        rules: Optional[dict] = None,
    ) -> bool:
        """Return True when the graph/lift network can physically route this leg.

        This is deliberately a feasibility check, not an assignment estimate.  It
        ignores whether a particular AMR is currently busy and does not reserve
        anything, so it is safe to use when deciding whether a pending task is
        impossible and should be failed.
        """
        try:
            if from_loc.floor == to_loc.floor:
                return (
                    self._shortest_path_same_floor(
                        from_loc.floor,
                        from_loc.name,
                        to_loc.name,
                        rules=rules,
                    )
                    is not None
                )

            return (
                self._nearest_compatible_lift_plan(
                    self.current_time,
                    amr,
                    from_loc,
                    to_loc,
                    payload,
                    rules=rules,
                )
                is not None
            )
        except Exception:
            return False

    def _released_task_terminal_failure_reason(self, task: Task) -> str:
        """Return a failure reason only for tasks that cannot ever run.

        Temporary states such as waiting for release time, AMRs being busy, AMRs
        charging, or a return payload not yet being available are intentionally
        left pending.
        """
        if task.release_time > self.current_time:
            return ""

        pickup_name = str(getattr(task, "pickup", "") or "").strip()
        dropoff_name = str(getattr(task, "dropoff", "") or "").strip()

        if pickup_name not in self.locations:
            return f"Pickup location '{pickup_name}' does not exist"
        if dropoff_name not in self.locations:
            return f"Drop-off location '{dropoff_name}' does not exist"

        payload = self._payload_for_task(task)
        payload_name = str(getattr(task, "payload", "") or "").strip()
        if payload is None:
            return f"Payload '{payload_name}' does not exist"

        compatible_amrs = [
            amr
            for amr in self.amrs
            if self._task_allowed_for_amr(task, amr)
            and self._amr_can_carry_payload(amr, payload)
        ]
        if not compatible_amrs:
            return (
                f"No AMR has sufficient payload capacity/dimensions for "
                f"{payload.name} ({payload.weight_kg}kg, "
                f"{payload.length_m}m x {payload.width_m}m x {payload.height_m}m)"
            )

        if self._location_has_payload_inventory_spaces(dropoff_name):
            spaces = self.inventory_spaces_by_location.get(dropoff_name, [])
            compatible_space_count = sum(
                1
                for space in spaces
                if self._inventory_space_can_fit_payload(space, payload)
            )
            if compatible_space_count <= 0:
                return self._inventory_pending_reason(dropoff_name, payload)
            if self._find_free_inventory_space(
                dropoff_name, payload
            ) is None and not self._task_can_exchange_with_store_empty(task, payload):
                return self._inventory_pending_reason(dropoff_name, payload)

        if not self._pickup_instance_available(task):
            if bool(
                getattr(task, "is_return_task", False)
            ) and self._location_has_inventory_mass_collection_rotation(
                pickup_name, payload.name
            ):
                return f"No '{payload.name}'s available at {pickup_name} for exchange"
            # Do not fail other return/exchange tasks just because the physical
            # payload has not appeared at the pickup yet.  It can become available
            # when the outbound task completes.
            return ""

        pickup_loc = self.locations[pickup_name]
        dropoff_loc = self.locations[dropoff_name]
        loaded_rules = self._resolve_task_route_rules(task)

        dropoff_reachable = any(
            self._route_possible_between_locations(
                amr, pickup_loc, dropoff_loc, payload, rules=loaded_rules
            )
            for amr in compatible_amrs
        )
        if not dropoff_reachable and pickup_loc.floor != dropoff_loc.floor:
            serving_lifts = [
                lift
                for lift in self.lifts
                if lift.can_serve(pickup_loc.floor, dropoff_loc.floor)
            ]
            if serving_lifts and all(
                self._lift_health_speed_factor(lift) <= 0.0
                for lift in serving_lifts
            ):
                health_text = ", ".join(
                    f"{lift.id}={float(getattr(lift, 'health_percent', 0.0) or 0.0):.1f}% "
                    f"(minimum {float(getattr(lift, 'minimum_operational_health_percent', 0.0) or 0.0):.1f}%)"
                    for lift in serving_lifts
                )
                return (
                    f"All lifts serving floors {pickup_loc.floor} and {dropoff_loc.floor} "
                    f"are below their operational health threshold: {health_text}"
                )
            if serving_lifts and self.scenario_mode and all(
                float(
                    self._scenario_event_state(
                        "lift", lift.id, self.current_time
                    ).get("availability_percent", 100.0)
                )
                <= 0.0
                for lift in serving_lifts
            ):
                return (
                    f"All lifts serving floors {pickup_loc.floor} and {dropoff_loc.floor} "
                    f"are unavailable in scenario {self.scenario_name}"
                )
        if not dropoff_reachable:
            return (
                f"No graph/lift route from {pickup_name} to {dropoff_name} "
                f"for payload {payload.name} using the task route restrictions"
            )

        # If no compatible AMR can currently reach the pickup, keep it pending
        # only when every compatible AMR is busy or charging.  Otherwise this is a
        # static graph/lift problem and should not hang the simulation.
        any_busy_or_charging = any(
            getattr(amr, "is_charging", False) or amr.available_time > self.current_time
            for amr in compatible_amrs
        )
        pickup_reachable_now = any(
            self._route_possible_between_locations(
                amr,
                self.locations.get(amr.location_name, pickup_loc),
                pickup_loc,
                self.payloads.get(EMPTY_PAYLOAD_NAME, payload),
                rules=None,
            )
            for amr in compatible_amrs
            if amr.location_name in self.locations
        )
        if not pickup_reachable_now and not any_busy_or_charging:
            return f"No graph/lift route for any compatible AMR to reach pickup {pickup_name}"

        return ""

    def _fail_released_terminal_pending_task(self, now: float) -> bool:
        """Fail one released task that is terminally impossible, if any."""
        for _priority, _release, _counter, pending_task in list(self.pending_tasks):
            if self._pending_task_removed(pending_task):
                continue
            reason = self._released_task_terminal_failure_reason(pending_task)
            if reason:
                self._fail_task(pending_task, reason, now=now)
                return True
        return False

    def _remove_pending_task(self, target_task: Task):
        target_id = str(getattr(target_task, "id", "")).strip()
        if target_id:
            self._removed_pending_task_ids.add(target_id)
            self._mark_task_activity_changed()
        self._purge_removed_pending_task_heads()
        self._compact_pending_tasks_if_needed()

    def _refresh_pending_existing_payload_instances(self) -> None:
        """Attach newly available physical payloads to pending existing-container tasks.

        Shared waste-bin tasks can be generated while the single physical bin is
        away at the waste destination or already reserved for its return journey.
        At generation time there may be no record in the payload store, so the
        task cannot be given a payload_instance_id or corrected pickup location.
        Re-checking pending tasks immediately before assignment lets those tasks
        bind to the returned shared bin once it is stored again, instead of
        remaining pending forever at the contributing department's nominal pickup.
        """
        if not self.pending_tasks:
            return

        for _priority, _release, _counter, task in list(self.pending_tasks):
            if self._pending_task_removed(task):
                continue
            if not self._task_requires_existing_payload_instance(task):
                continue

            instance_id = str(getattr(task, "payload_instance_id", "") or "").strip()
            if instance_id and self._pickup_instance_available(task):
                continue

            # If a shared-container task was bound before the bin moved, clear the
            # stale assignment and re-resolve it from the current payload store.
            if instance_id and str(getattr(task, "container_group", "") or "").strip():
                self.payload_instance_store.release_reservation(
                    instance_id, str(getattr(task, "id", "") or "")
                )
                task.payload_instance_id = ""

            if not str(getattr(task, "payload_instance_id", "") or "").strip():
                self._assign_available_existing_payload_instance(task)

    # Task Runner - steps thru sequentially until task end

    def _schedule_assignment_continue(self, now: float) -> None:
        if self._assignment_continue_scheduled:
            return
        self._assignment_continue_scheduled = True
        self.push_event(
            now + self.assignment_continue_delay_sec, "assignment_continue", {}
        )

    def _task_commit_failure_is_transient(self, task: Task) -> bool:
        if bool(getattr(task, "is_idle_return", False)):
            return True
        return bool(
            self._task_requires_existing_payload_instance(task)
            and not self._pickup_instance_available(task)
        )

    def _defer_transient_commit_failure(self, task: Task, amr: AMR) -> None:
        if bool(getattr(task, "is_idle_return", False)):
            self._remove_pending_task(task)
            self._clear_amr_inventory_space_reservations(amr)
            setattr(amr, "target_inventory_space_name", "")
            setattr(amr, "target_charge_location", "")
        else:
            instance_id = str(getattr(task, "payload_instance_id", "") or "").strip()
            if instance_id:
                self.payload_instance_store.release_reservation(
                    instance_id, str(getattr(task, "id", "") or "")
                )
            if str(getattr(task, "container_group", "") or "").strip():
                task.payload_instance_id = ""
            self._set_task_pending_reason(
                task, self._pickup_instance_pending_reason(task)
            )
        self._invalidate_route_estimate_cache()

    def _try_assign_tasks(self, now: float, force_idle_return: bool = False):
        self.current_time = max(self.current_time, now)
        self._assignment_continue_scheduled = False
        self._purge_removed_pending_task_heads()

        if not self.pending_tasks and not force_idle_return:
            if self.current_time < self._next_idle_return_check_time:
                return
            self._next_idle_return_check_time = (
                self.current_time + self.idle_return_check_interval_sec
            )

        self._refresh_pending_existing_payload_instances()
        self._purge_idle_returns_blocked_by_locked_work()
        self._queue_idle_return_tasks(self.current_time)
        processed_this_tick = 0

        def chunk_limit_reached() -> bool:
            return (
                self.max_assignments_per_tick > 0
                and processed_this_tick >= self.max_assignments_per_tick
            )

        while self.pending_tasks:
            self._purge_removed_pending_task_heads()
            if not self.pending_tasks:
                break
            if chunk_limit_reached():
                self._schedule_assignment_continue(self.current_time)
                return
            # First, send any idle AMRs that need recharge to charge immediately
            charge_scheduled = False
            for amr in self.amrs:
                if getattr(amr, "is_charging", False):
                    continue
                if amr.available_time > self.current_time:
                    continue
                if self._needs_post_task_recharge(amr):
                    if self._schedule_charge_cycle(amr, self.current_time):
                        charge_scheduled = True

            if charge_scheduled:
                # Re-evaluate after charge events have been queued
                continue

            # Return trip to home location, but never ahead of locked physical-bin returns.
            self._purge_idle_returns_blocked_by_locked_work()
            self._queue_idle_return_tasks(self.current_time)

            choice = self._select_best_assignment()
            if choice is None:
                range_charge_scheduled = False
                for candidate_amr in self.amrs:
                    if getattr(candidate_amr, "is_charging", False):
                        continue
                    if float(getattr(candidate_amr, "available_time", 0.0) or 0.0) > self.current_time:
                        continue
                    if float(getattr(candidate_amr, "battery_soc_percent", 100.0) or 100.0) >= 99.999:
                        continue
                    if self._schedule_charge_cycle(candidate_amr, self.current_time):
                        range_charge_scheduled = True
                if range_charge_scheduled:
                    continue

                # If the scheduler cannot find any assignment, fail one released
                # task that is terminally impossible.  This prevents invalid
                # locations, missing payloads, impossible route restrictions,
                # incompatible AMR dimensions, or full/unsuitable inventory spaces
                # from remaining pending forever.
                if self._fail_released_terminal_pending_task(self.current_time):
                    processed_this_tick += 1
                    if chunk_limit_reached():
                        self._schedule_assignment_continue(self.current_time)
                        return
                    continue

                self._create_wait_event_for_pending_tasks(self.current_time)
                return

            amr, task_or_tasks, _ = choice

            if isinstance(task_or_tasks, list):
                tasks = task_or_tasks
                committed = self._estimate_multi_stop_for_amr(amr, tasks, reserve=True)

                if committed is None:
                    if any(
                        self._task_commit_failure_is_transient(failed_task)
                        for failed_task in tasks
                    ):
                        for deferred_task in tasks:
                            if self._task_commit_failure_is_transient(deferred_task):
                                self._defer_transient_commit_failure(deferred_task, amr)
                        self._create_wait_event_for_pending_tasks(self.current_time)
                        return
                    for failed_task in tasks:
                        reason = (
                            getattr(failed_task, "pending_reason", "")
                            or "No feasible multi-stop AMR/lift/battery/graph combination"
                        )
                        self._fail_task(failed_task, reason, now=self.current_time)
                    processed_this_tick += max(1, len(tasks))
                    if chunk_limit_reached():
                        self._schedule_assignment_continue(self.current_time)
                        return
                    continue

                for multi_task in tasks:
                    self._remove_pending_task(multi_task)
                    self._set_task_pending_reason(multi_task, "")

                start_time = committed["task_start_time"]
                finish_time = committed["finish_time"]
                previous_location = amr.location_name
                self._free_amr_inventory_space(amr)
                amr.total_busy_time += committed["duration"]
                amr.available_time = finish_time
                amr.location_name = committed["end_location"]
                amr.completed_tasks += len(tasks)

                for multi_task in tasks:
                    self.log_step(
                        event_time=start_time,
                        event_type="multi_stop_task_assigned",
                        task_id=multi_task.id,
                        amr_id=amr.id,
                        details=(
                            f"Assigned multi-stop batch to {amr.id}; "
                            f"slot={committed.get('slot_assignments', {}).get(multi_task.id, '')}; "
                            f"batch={','.join(task.id for task in tasks)}"
                        ),
                        from_location=multi_task.pickup,
                        to_location=multi_task.dropoff,
                        payload_name=self._payload_log_name(multi_task.payload),
                        payload_instance_id=getattr(
                            multi_task, "payload_instance_id", ""
                        ),
                        task_duration_sec=committed["duration"],
                        amr_location_before=previous_location,
                        amr_location_after=committed["end_location"],
                        start_time=start_time,
                        end_time=start_time + 1.0,
                        status="start",
                        task_source=getattr(multi_task, "task_source", ""),
                        department_id=getattr(multi_task, "department_id", ""),
                        waste_stream=getattr(multi_task, "waste_stream", ""),
                        waste_volume_m3=getattr(multi_task, "waste_volume_m3", 0.0),
                        container_type=getattr(multi_task, "container_type", ""),
                        **self._task_tracking_log_kwargs(multi_task),
                    )

                self._log_multi_stop_segments(amr, tasks, committed, start_time)

                self._invalidate_route_estimate_cache()

                self.push_event(
                    finish_time,
                    "multi_stop_complete",
                    {
                        "tasks": tasks,
                        "amr_id": amr.id,
                        "start_time": start_time,
                        "finish_time": finish_time,
                        "duration": committed["duration"],
                        "segments": committed["segments"],
                        "end_location": committed.get(
                            "end_location", amr.location_name
                        ),
                        "energy_kwh": committed["energy_kwh"],
                        "battery_soc_after": amr.battery_soc_percent,
                        "lift_energy_kwh": committed["lift_energy_kwh"],
                        "lift_empty_sec_total": committed["lift_empty_sec_total"],
                        "lift_loaded_sec_total": committed["lift_loaded_sec_total"],
                        "slot_assignments": committed.get("slot_assignments", {}),
                    },
                )
                processed_this_tick += max(1, len(tasks))
                if chunk_limit_reached():
                    self._schedule_assignment_continue(self.current_time)
                    return
                continue

            task = task_or_tasks
            committed = self._estimate_task_for_amr(amr, task, reserve=True)

            if committed is None:
                if self._task_commit_failure_is_transient(task):
                    self._defer_transient_commit_failure(task, amr)
                    self._create_wait_event_for_pending_tasks(self.current_time)
                    return
                reason = (
                    getattr(task, "pending_reason", "")
                    or "No feasible AMR/lift/battery/graph combination"
                )
                self._fail_task(task, reason, now=self.current_time)
                processed_this_tick += 1
                if chunk_limit_reached():
                    self._schedule_assignment_continue(self.current_time)
                    return
                continue

            self._remove_pending_task(task)
            self._set_task_pending_reason(task, "")
            start_time = committed["task_start_time"]
            finish_time = committed["finish_time"]
            previous_location = amr.location_name
            keep_target_reservation = bool(getattr(task, "is_idle_return", False))
            self._free_amr_inventory_space(
                amr, keep_target_reservation=keep_target_reservation
            )
            if getattr(task, "is_idle_return", False) and committed.get("end_location"):
                committed["amr_inventory_space"] = str(
                    committed.get("amr_inventory_space", "")
                    or getattr(amr, "target_inventory_space_name", "")
                    or ""
                )
            amr.total_busy_time += committed["duration"]
            amr.available_time = finish_time
            amr.location_name = committed["end_location"]
            amr.completed_tasks += 1

            self.log_step(
                event_time=start_time,
                event_type="task_assigned",
                task_id=task.id,
                amr_id=amr.id,
                details=f"Assigned task to {amr.id}",
                from_location=task.pickup,
                to_location=task.dropoff,
                payload_name=self._payload_log_name(task.payload),
                payload_instance_id=getattr(task, "payload_instance_id", ""),
                task_duration_sec=committed["duration"],
                amr_location_before=previous_location,
                amr_location_after=committed["end_location"],
                start_time=start_time,
                end_time=start_time + 1.0,
                status="start",
                task_source=getattr(task, "task_source", ""),
                department_id=getattr(task, "department_id", ""),
                waste_stream=getattr(task, "waste_stream", ""),
                waste_volume_m3=getattr(task, "waste_volume_m3", 0.0),
                container_type=getattr(task, "container_type", ""),
                **self._task_tracking_log_kwargs(task),
            )

            segment_start_time = start_time
            carrying_payload = False

            for segment in committed["segments"]:
                segment.setdefault("payload_orientation", committed.get("payload_orientation", getattr(task, "payload_orientation", "")))
                from_node = segment.get("from", "")
                to_node = segment.get("to", "")
                segment_type = segment.get("type", "")
                if segment_type == "lift_reposition":
                    wait_node = segment.get("amr_wait_node") or to_node or from_node
                    from_node = wait_node
                    to_node = wait_node

                lift_id = segment.get("lift_id", "")
                if not lift_id and segment.get("type", "").startswith("lift_"):
                    for key_node in (from_node, to_node):
                        if key_node:
                            for lift in self.lifts:
                                prefix = f"{lift.id}-F"
                                if key_node.startswith(prefix):
                                    lift_id = lift.id
                                    break
                            if lift_id:
                                break

                wait_time = float(segment.get("wait_time", 0.0))
                duration = float(segment.get("duration", 0.0))
                segment_type = segment.get("type", "")
                if segment_type == "lift_reposition":
                    # Lift repositioning is lift-car movement only. The AMR waits
                    # at the origin landing until the car arrives; logging the
                    # car's from/to floors as AMR coordinates made it appear to
                    # teleport in the visualiser.
                    wait_node = segment.get("amr_wait_node") or to_node or from_node
                    from_node = wait_node
                    to_node = wait_node

                from_coords = self.graph_nodes.get(from_node)
                to_coords = self.graph_nodes.get(to_node)
                segment_start_x = segment.get("from_x", getattr(from_coords, "x", None))
                segment_start_y = segment.get("from_y", getattr(from_coords, "y", None))
                segment_start_floor = segment.get(
                    "from_floor", getattr(from_coords, "floor", None)
                )
                segment_end_x = segment.get("to_x", getattr(to_coords, "x", None))
                segment_end_y = segment.get("to_y", getattr(to_coords, "y", None))
                segment_end_floor = segment.get(
                    "to_floor", getattr(to_coords, "floor", None)
                )
                if segment_type == "lift_reposition":
                    segment_start_x = getattr(from_coords, "x", segment_start_x)
                    segment_start_y = getattr(from_coords, "y", segment_start_y)
                    segment_start_floor = getattr(
                        from_coords, "floor", segment_start_floor
                    )
                    segment_end_x = segment_start_x
                    segment_end_y = segment_start_y
                    segment_end_floor = segment_start_floor

                segment_has_payload = (
                    carrying_payload
                    or segment_type in {"pickup", "dropoff"}
                    or bool(
                        getattr(task, "is_return_task", False)
                        and segment_type not in {"wait_for_location"}
                    )
                )
                segment_payload_name = task.payload if segment_has_payload else ""
                segment_payload_instance_id = (
                    getattr(task, "payload_instance_id", "")
                    if segment_has_payload
                    else ""
                )

                explicit_wait = duration if segment_type.startswith("wait_") else 0.0

                if wait_time > 0:
                    self.log_step(
                        event_time=segment_start_time,
                        event_type="segment_wait",
                        task_id=task.id,
                        amr_id=amr.id,
                        details=json.dumps(segment, ensure_ascii=False),
                        from_location=from_node or task.pickup,
                        to_location=to_node or task.dropoff,
                        payload_name=segment_payload_name,
                        payload_instance_id=segment_payload_instance_id,
                        lift_id=lift_id,
                        duration_sec=wait_time,
                        wait_time_sec=wait_time,
                        distance_m=0.0,
                        segment_type="wait",
                        start_time=segment_start_time,
                        end_time=segment_start_time + wait_time,
                        start_node=from_node,
                        end_node=from_node,
                        start_x=segment_start_x,
                        start_y=segment_start_y,
                        start_floor=segment_start_floor,
                        end_x=segment_start_x,
                        end_y=segment_start_y,
                        end_floor=segment_start_floor,
                        status="waiting",
                        energy_kwh=segment.get("energy_kwh", 0.0),
                    )
                    segment_start_time += wait_time

                if explicit_wait > 0:
                    self.log_step(
                        event_time=segment_start_time,
                        event_type="segment_wait",
                        task_id=task.id,
                        amr_id=amr.id,
                        details=json.dumps(segment, ensure_ascii=False),
                        from_location=from_node or task.pickup,
                        to_location=to_node or task.dropoff,
                        payload_name=segment_payload_name,
                        payload_instance_id=segment_payload_instance_id,
                        lift_id=lift_id,
                        duration_sec=explicit_wait,
                        wait_time_sec=explicit_wait,
                        distance_m=0.0,
                        segment_type=segment_type,
                        start_time=segment_start_time,
                        end_time=segment_start_time + explicit_wait,
                        start_node=from_node,
                        end_node=to_node or from_node,
                        start_x=segment_start_x,
                        start_y=segment_start_y,
                        start_floor=segment_start_floor,
                        end_x=segment_end_x,
                        end_y=segment_end_y,
                        end_floor=segment_end_floor,
                        status="waiting",
                        energy_kwh=segment.get("energy_kwh", 0.0),
                    )

                if explicit_wait > 0:
                    segment_start_time += explicit_wait
                    continue

                segment_end_time = segment_start_time + duration

                self.log_step(
                    event_time=segment_start_time,
                    event_type=f"segment_{segment.get('type', '')}",
                    task_id=task.id,
                    amr_id=amr.id,
                    details=json.dumps(
                        {
                            **segment,
                            "from_x": segment_start_x,
                            "from_y": segment_start_y,
                            "to_x": segment_end_x,
                            "to_y": segment_end_y,
                            "from_floor": segment_start_floor,
                            "to_floor": segment_end_floor,
                        },
                        ensure_ascii=False,
                    ),
                    from_location=from_node or task.pickup,
                    to_location=to_node or task.dropoff,
                    payload_name=segment_payload_name,
                    payload_instance_id=segment_payload_instance_id,
                    lift_id=lift_id,
                    duration_sec=duration,
                    wait_time_sec=wait_time,
                    distance_m=segment.get("distance_m", 0.0),
                    segment_type=segment_type,
                    start_time=segment_start_time,
                    end_time=segment_end_time,
                    start_node=from_node,
                    end_node=to_node,
                    start_x=segment_start_x,
                    start_y=segment_start_y,
                    start_floor=segment_start_floor,
                    end_x=segment_end_x,
                    end_y=segment_end_y,
                    end_floor=segment_end_floor,
                    status="completed",
                    energy_kwh=segment.get("energy_kwh", 0.0),
                    amr_rotation_start_deg=segment.get("amr_rotation_start_deg", None),
                    amr_rotation_end_deg=segment.get("amr_rotation_end_deg", None),
                    amr_rotation_deg=segment.get("amr_rotation_deg", None),
                )

                segment_start_time = segment_end_time
                if segment_type == "pickup":
                    carrying_payload = True
                elif segment_type == "dropoff":
                    carrying_payload = False

            self._invalidate_route_estimate_cache()

            self.push_event(
                finish_time,
                "task_complete",
                {
                    "task": task,
                    "amr_id": amr.id,
                    "start_time": start_time,
                    "finish_time": finish_time,
                    "duration": committed["duration"],
                    "target_time": task.target_time,
                    "segments": committed["segments"],
                    "energy_kwh": committed["energy_kwh"],
                    "battery_soc_after": amr.battery_soc_percent,
                    "lift_energy_kwh": committed["lift_energy_kwh"],
                    "lift_empty_sec_total": committed["lift_empty_sec_total"],
                    "lift_loaded_sec_total": committed["lift_loaded_sec_total"],
                    "amr_inventory_space": committed.get("amr_inventory_space", ""),
                    "end_location": committed.get("end_location", task.dropoff),
                    "payload_orientation": committed.get("payload_orientation", getattr(task, "payload_orientation", "")),
                    "payload_slot": committed.get("payload_slot", ""),
                },
            )
            processed_this_tick += 1
            if chunk_limit_reached():
                self._schedule_assignment_continue(self.current_time)
                return

    def _log_multi_stop_segments(
        self, amr: AMR, tasks: List[Task], committed: dict, start_time: float
    ) -> None:
        """Write visualiser-friendly rows for a committed multi-stop route.

        The visualiser needs the complete AMR slot/onboard state on every row,
        not just the payload associated with the current segment.  This logger
        therefore writes grouped pickup/dropoff rows and repeats the current
        onboard state across all movement, wait and lift rows.
        """
        tasks_by_id = {str(task.id): task for task in tasks}
        fallback_task = tasks[0] if tasks else None
        slot_assignments = committed.get("slot_assignments", {}) or {}
        all_task_ids = [str(task.id) for task in tasks]
        segment_start_time = start_time
        carrying_task_ids = set()

        def _task_ids_for_segment(segment: dict) -> List[str]:
            raw_ids = segment.get("task_ids")
            if isinstance(raw_ids, list):
                ids = [str(x).strip() for x in raw_ids if str(x).strip()]
                if ids:
                    return ids

            raw_id = str(segment.get("task_id", "") or "").strip()
            if raw_id:
                ids = [x.strip() for x in raw_id.split(",") if x.strip()]
                if ids:
                    return ids

            return []

        def _payload_name_for_ids(task_ids: List[str]) -> str:
            names = []
            for task_id in task_ids:
                task = tasks_by_id.get(task_id)
                if task is None:
                    continue
                payload_name = self._payload_log_name(getattr(task, "payload", ""))
                if payload_name:
                    names.append(payload_name)
            return ",".join(names)

        def _payload_instance_for_ids(task_ids: List[str]) -> str:
            values = []
            for task_id in task_ids:
                task = tasks_by_id.get(task_id)
                if task is None:
                    continue
                value = str(getattr(task, "payload_instance_id", "") or "").strip()
                if value:
                    values.append(value)
            return ",".join(values)

        def _payload_slot_for_ids(task_ids: List[str]) -> str:
            values = []
            for task_id in task_ids:
                value = str(slot_assignments.get(task_id, "") or "").strip()
                if value:
                    values.append(value)
            return ",".join(values)

        def _onboard_payload_records(task_ids) -> List[dict]:
            records = []
            for task_id in sorted(str(x) for x in task_ids):
                task = tasks_by_id.get(task_id)
                if task is None:
                    continue
                payload_name = self._payload_log_name(getattr(task, "payload", ""))
                if not payload_name:
                    continue
                slot_name = str(slot_assignments.get(task_id, "") or "").strip()
                records.append(
                    {
                        "task_id": task_id,
                        "payload": payload_name,
                        "payload_instance_id": str(
                            getattr(task, "payload_instance_id", "") or ""
                        ).strip(),
                        "pickup": str(getattr(task, "pickup", "") or "").strip(),
                        "dropoff": str(getattr(task, "dropoff", "") or "").strip(),
                        "slot_name": slot_name,
                    }
                )
            return records

        def _onboard_slot_records(task_ids) -> List[dict]:
            records = []
            for item in _onboard_payload_records(task_ids):
                records.append(
                    {
                        "slot_name": item.get("slot_name", ""),
                        "task_id": item.get("task_id", ""),
                        "payload": item.get("payload", ""),
                        "payload_instance_id": item.get("payload_instance_id", ""),
                    }
                )
            return records

        for segment in committed["segments"]:
            segment_type = str(segment.get("type", "") or "").strip()
            segment_task_ids = _task_ids_for_segment(segment)
            task = (
                tasks_by_id.get(segment_task_ids[0])
                if segment_task_ids
                else fallback_task
            )

            from_node = segment.get("from", "") or segment.get("location", "")
            to_node = segment.get("to", "") or segment.get("location", "")
            if segment_type == "lift_reposition":
                wait_node = segment.get("amr_wait_node") or to_node or from_node
                from_node = wait_node
                to_node = wait_node
            from_coords = self.graph_nodes.get(from_node)
            to_coords = self.graph_nodes.get(to_node)
            segment_start_x = segment.get("from_x", getattr(from_coords, "x", None))
            segment_start_y = segment.get("from_y", getattr(from_coords, "y", None))
            segment_start_floor = segment.get(
                "from_floor", getattr(from_coords, "floor", None)
            )
            segment_end_x = segment.get("to_x", getattr(to_coords, "x", None))
            segment_end_y = segment.get("to_y", getattr(to_coords, "y", None))
            segment_end_floor = segment.get(
                "to_floor", getattr(to_coords, "floor", None)
            )
            if segment_type == "lift_reposition":
                segment_start_x = getattr(from_coords, "x", segment_start_x)
                segment_start_y = getattr(from_coords, "y", segment_start_y)
                segment_start_floor = getattr(from_coords, "floor", segment_start_floor)
                segment_end_x = segment_start_x
                segment_end_y = segment_start_y
                segment_end_floor = segment_start_floor
            lift_id = segment.get("lift_id", "")
            if not lift_id and segment_type.startswith("lift_"):
                for key_node in (from_node, to_node):
                    if key_node:
                        for lift in self.lifts:
                            prefix = f"{lift.id}-F"
                            if key_node.startswith(prefix):
                                lift_id = lift.id
                                break
                        if lift_id:
                            break

            duration = float(segment.get("duration", 0.0) or 0.0)
            wait_time = float(segment.get("wait_time", 0.0) or 0.0)
            explicit_wait = duration if segment_type.startswith("wait_") else 0.0
            segment_end_time = segment_start_time + duration

            # For pickup/dropoff rows, publish the onboard state AFTER the load
            # action.  For movement/wait/lift rows, publish the state carried
            # through the segment.  This makes slot occupancy persist during
            # travel in the visualiser.
            onboard_after = set(carrying_task_ids)
            if segment_type == "pickup":
                onboard_after.update(segment_task_ids)
            elif segment_type == "dropoff":
                for task_id in segment_task_ids:
                    onboard_after.discard(task_id)

            visible_task_ids = segment_task_ids or sorted(carrying_task_ids)
            segment_task_id = ",".join(visible_task_ids)
            segment_payload_name = _payload_name_for_ids(visible_task_ids)
            segment_payload_instance_id = _payload_instance_for_ids(visible_task_ids)
            segment_payload_slot = _payload_slot_for_ids(visible_task_ids)

            self.log_step(
                event_time=segment_start_time,
                event_type=f"multi_stop_segment_{segment_type}",
                task_id=segment_task_id or (task.id if task is not None else ""),
                amr_id=amr.id,
                details=json.dumps(segment, ensure_ascii=False),
                from_location=from_node or (task.pickup if task is not None else ""),
                to_location=to_node or (task.dropoff if task is not None else ""),
                payload_name=segment_payload_name,
                payload_instance_id=segment_payload_instance_id,
                payload_slot=segment_payload_slot,
                onboard_payloads=_onboard_payload_records(onboard_after),
                onboard_slots=_onboard_slot_records(onboard_after),
                multi_stop_task_ids=all_task_ids,
                multi_stop_pickup_count=(
                    len(segment_task_ids) if segment_type == "pickup" else 0
                ),
                multi_stop_dropoff_count=(
                    len(segment_task_ids) if segment_type == "dropoff" else 0
                ),
                lift_id=lift_id,
                duration_sec=duration,
                wait_time_sec=wait_time or explicit_wait,
                distance_m=segment.get("distance_m", 0.0),
                segment_type=segment_type,
                start_time=segment_start_time,
                end_time=segment_end_time,
                start_node=from_node,
                end_node=to_node,
                start_x=segment_start_x,
                start_y=segment_start_y,
                start_floor=segment_start_floor,
                end_x=segment_end_x,
                end_y=segment_end_y,
                end_floor=segment_end_floor,
                status="waiting" if segment_type.startswith("wait_") else "completed",
                energy_kwh=segment.get("energy_kwh", 0.0),
                task_source=(
                    getattr(task, "task_source", "") if task is not None else ""
                ),
                department_id=(
                    getattr(task, "department_id", "") if task is not None else ""
                ),
                waste_stream=(
                    getattr(task, "waste_stream", "") if task is not None else ""
                ),
                waste_volume_m3=(
                    getattr(task, "waste_volume_m3", 0.0) if task is not None else 0.0
                ),
                container_type=(
                    getattr(task, "container_type", "") if task is not None else ""
                ),
            )

            carrying_task_ids = onboard_after
            segment_start_time = segment_end_time

    def _stagger_generated_task_release(self, task: Task) -> None:
        if self.generated_release_stagger_sec <= 0.0:
            return
        release_time = float(getattr(task, "release_time", 0.0) or 0.0)
        key = round(release_time, 6)
        index = self._generated_release_stagger_counts[key]
        self._generated_release_stagger_counts[key] += 1
        if index > 0:
            task.release_time = release_time + (
                index * self.generated_release_stagger_sec
            )

    def _update_task_generators_until(self, now: float):
        if not getattr(self, "task_generation_manager", None):
            return
        if now > getattr(self, "task_generation_horizon_sec", now):
            return

        for record in self.task_generation_manager.update_until(now):
            task = record.task
            self._mark_generated_waste_task_requires_existing_container(task)
            self._stagger_generated_task_release(task)
            self.schedule_task_release(task)

            # The simulator may adjust the task pickup for shared physical
            # containers so that the task collects the actual seeded bin location
            # rather than the contributing department that triggered the threshold.
            pickup_location_name = str(
                getattr(task, "pickup", record.pickup_location)
                or record.pickup_location
            )
            dropoff_location_name = str(
                getattr(task, "dropoff", record.dropoff_location)
                or record.dropoff_location
            )
            pickup = self.locations.get(pickup_location_name)
            dropoff = self.locations.get(dropoff_location_name)

            self.log_step(
                event_time=task.release_time,
                event_type=record.event_type,
                task_id=task.id,
                details=record.details,
                from_location=pickup_location_name,
                to_location=dropoff_location_name,
                payload_name=self._payload_log_name(record.payload_name),
                payload_instance_id=getattr(task, "payload_instance_id", ""),
                duration_sec=0.0,
                wait_time_sec=0.0,
                distance_m=0.0,
                start_time=task.release_time,
                end_time=task.release_time,
                start_node=pickup_location_name,
                end_node=dropoff_location_name,
                start_x=getattr(pickup, "x", None),
                start_y=getattr(pickup, "y", None),
                start_floor=getattr(pickup, "floor", None),
                end_x=getattr(dropoff, "x", None),
                end_y=getattr(dropoff, "y", None),
                end_floor=getattr(dropoff, "floor", None),
                status="generated",
                energy_kwh=0.0,
                task_source=record.task_source,
                department_id=record.department_id,
                waste_stream=record.waste_stream,
                waste_volume_m3=record.waste_volume_m3,
                container_type=record.container_type,
                **self._task_tracking_log_kwargs(task),
            )

    def _prune_historical_reservations(self, now: float) -> None:
        if now < self._next_reservation_prune_time:
            return

        self._next_reservation_prune_time = now + self.reservation_prune_interval_sec
        cutoff = max(0.0, float(now or 0.0) - self.reservation_history_retention_sec)

        def prune_mapping(mapping, duration_mapping):
            remove_keys = []
            for key, reservations in list(mapping.items()):
                if not reservations:
                    remove_keys.append(key)
                    continue
                # Lists are sorted by start time but end times can differ, so
                # retain by end time and recompute the overlap-search bound.
                kept = [item for item in reservations if float(item[1]) >= cutoff]
                if kept:
                    mapping[key] = kept
                    duration_mapping[key] = max(
                        max(0.0, float(item[1]) - float(item[0])) for item in kept
                    )
                else:
                    remove_keys.append(key)
            for key in remove_keys:
                mapping.pop(key, None)
                duration_mapping.pop(key, None)

        prune_mapping(
            self.location_reservations, self.location_reservation_max_duration
        )
        prune_mapping(self.edge_reservations, self.edge_reservation_max_duration)
        prune_mapping(
            self.directed_edge_reservations,
            self.directed_edge_reservation_max_duration,
        )
        prune_mapping(self.node_reservations, self.node_reservation_max_duration)

    def run(self):
        self.wall_start_time = time.time()

        try:
            while True:
                with self.lock:
                    if not self.events:
                        break

                    event = heapq.heappop(self.events)
                    self._mark_task_activity_changed()
                    self.current_time = max(self.current_time, event.time)
                    self._prune_historical_reservations(self.current_time)
                    self._handle_event(event)

                self._print_progress()

                if self.stop_requested:
                    break
        finally:
            if self.routing_executor is not None:
                self.routing_executor.shutdown(wait=True, cancel_futures=False)
                self.routing_executor = None

        self._print_progress_complete()
        self.print_short_summary()
        print()

    def _queue_same_time_task_releases(self, event: Event) -> List[Task]:
        """Queue the current task release and all other releases at the same
        simulation time before trying to assign work.

        Generated multi-stop workloads often create several tasks with the same
        release timestamp.  Assigning after the first release lets a single task
        win before the rest of the batch has reached pending_tasks.
        """
        released_tasks: List[Task] = []

        task = event.payload.get("task")
        if task is not None:
            self._queue_pending_task(task)
            released_tasks.append(task)

        deferred_event = None
        while self.events:
            next_event = heapq.heappop(self.events)
            if (
                next_event.event_type == "task_release"
                and abs(float(next_event.time) - float(event.time)) <= 1e-9
            ):
                next_task = next_event.payload.get("task")
                if next_task is not None:
                    self._queue_pending_task(next_task)
                    released_tasks.append(next_task)
                continue

            deferred_event = next_event
            break

        if deferred_event is not None:
            heapq.heappush(self.events, deferred_event)

        return released_tasks

    def _handle_event(self, event: Event):
        if event.event_type == "task_release":
            self._queue_same_time_task_releases(event)
            self._try_assign_tasks(event.time)
        elif event.event_type == "assignment_continue":
            self._assignment_continue_scheduled = False
            self._try_assign_tasks(event.time)
        elif event.event_type == "task_complete":
            task: Task = event.payload["task"]
            payload_obj = self._payload_for_task(task)
            if payload_obj is not None and not is_empty_payload_name(task.payload):
                try:
                    self._pickup_payload_instance_for_task(task)
                except RuntimeError as exc:
                    self._fail_task(task, str(exc), now=event.payload["finish_time"])
                    return
                self._free_inventory_space_for_pickup(task, payload_obj)
                self._consume_store_empty_for_exchange(task, payload_obj)
                skip_dropoff_payload_store = bool(
                    getattr(task, "return_same_payload_instance", False)
                    and self._location_has_inventory_mass_collection_rotation(
                        task.dropoff, payload_obj.name
                    )
                )
                # Verify/claim a stowage space before writing a stored physical
                # payload record.  If the location is full, fail the task with a
                # precise reason rather than silently adding stock and inflating
                # peak occupancy.
                if (
                    not skip_dropoff_payload_store
                    and self._location_has_payload_inventory_spaces(task.dropoff)
                ):
                    claimed_space = self._reserve_inventory_space_for_task(
                        task, payload_obj
                    )
                    if (
                        claimed_space is None
                        and not str(
                            getattr(task, "assigned_inventory_space", "") or ""
                        ).strip()
                    ):
                        reason = self._inventory_pending_reason(
                            task.dropoff, payload_obj
                        )
                        self._fail_task(task, reason, now=event.payload["finish_time"])
                        return
                if not skip_dropoff_payload_store:
                    self._store_payload_instance_for_task(task)
                    if not self._occupy_inventory_space_for_completed_task(
                        task, payload_obj
                    ):
                        reason = str(
                            getattr(task, "pending_reason", "")
                            or self._inventory_pending_reason(task.dropoff, payload_obj)
                        )
                        self._fail_task(task, reason, now=event.payload["finish_time"])
                        return
            completed_amr = self.amrs_by_id.get(event.payload.get("amr_id"))
            completed_end_location_name = str(event.payload.get("end_location") or task.dropoff or "")
            completed_amr_loc = self.locations.get(completed_end_location_name)
            completed_amr_space = None
            if completed_amr is not None:
                planned_space = str(
                    event.payload.get("amr_inventory_space", "") or ""
                ).strip()
                if not planned_space and getattr(task, "is_idle_return", False):
                    for segment in reversed(event.payload.get("segments", []) or []):
                        planned_space = str(
                            segment.get("inventory_space", "") or ""
                        ).strip()
                        if planned_space:
                            break
                if planned_space:
                    setattr(completed_amr, "target_inventory_space_name", planned_space)
                completed_amr_space = self._occupy_amr_inventory_space(
                    completed_amr, completed_end_location_name
                )
                if (
                    getattr(task, "is_idle_return", False)
                    and completed_amr_space is None
                ):
                    self.synthetic_task_counter += 1
                    relocation = Task(
                        id=f"RETURN-{completed_amr.id}-{self.synthetic_task_counter}",
                        pickup=task.dropoff,
                        dropoff=task.dropoff,
                        payload=EMPTY_PAYLOAD_NAME,
                        release_time=event.payload["finish_time"],
                        priority=999999,
                        target_time=0.0,
                        labels=["idle_charge_return", "bay_relocation"],
                        route_profile=None,
                    )
                    relocation.created_during_runtime = True
                    relocation.is_idle_return = True
                    relocation.amr_id = completed_amr.id
                    relocation.locked_amr_id = completed_amr.id
                    alternative, reserved_space = (
                        self._reserve_best_idle_return_destination(
                            completed_amr,
                            relocation,
                            event.payload["finish_time"],
                            exclude_locations={task.dropoff},
                        )
                    )
                    if alternative:
                        relocation.dropoff = alternative
                        relocation.target_charge_location = alternative
                        relocation.assigned_amr_inventory_space = str(
                            (reserved_space or {}).get("name", "") or ""
                        )
                        self._queue_pending_task(relocation)
                        self.log_step(
                            event_time=event.payload["finish_time"],
                            event_type="idle_return_replanned",
                            task_id=relocation.id,
                            amr_id=completed_amr.id,
                            details=(
                                f"Arrival bay unavailable at {task.dropoff}; "
                                f"replanned to {alternative}"
                            ),
                            from_location=task.dropoff,
                            to_location=alternative,
                            status="pending",
                        )
                    else:
                        self._clear_amr_inventory_space_reservations(completed_amr)
                        setattr(completed_amr, "target_inventory_space_name", "")
                        setattr(completed_amr, "target_charge_location", "")
                        self.log_step(
                            event_time=event.payload["finish_time"],
                            event_type="idle_return_waiting_for_bay",
                            task_id=task.id,
                            amr_id=completed_amr.id,
                            details=(
                                "No compatible AMR bay currently available at any "
                                "configured charging location; retry deferred"
                            ),
                            from_location=task.dropoff,
                            to_location=task.dropoff,
                            status="pending",
                        )
                completed_amr_loc = (
                    self._amr_display_location(completed_amr, task.dropoff)
                    or completed_amr_loc
                )

            self.log_step(
                event_time=event.payload["finish_time"],
                event_type="task_complete",
                task_id=task.id,
                amr_id=event.payload["amr_id"],
                details=f"Task {task.id} completed",
                from_location=task.pickup,
                to_location=task.dropoff,
                payload_name=self._payload_log_name(task.payload),
                payload_instance_id=getattr(task, "payload_instance_id", ""),
                duration_sec=0.0,
                wait_time_sec=0.0,
                distance_m=0.0,
                start_time=event.payload["finish_time"],
                end_time=event.payload["finish_time"],
                start_node=completed_end_location_name,
                end_node=completed_end_location_name,
                start_x=getattr(completed_amr_loc, "x", None),
                start_y=getattr(completed_amr_loc, "y", None),
                start_floor=getattr(completed_amr_loc, "floor", None),
                end_x=getattr(completed_amr_loc, "x", None),
                end_y=getattr(completed_amr_loc, "y", None),
                end_floor=getattr(completed_amr_loc, "floor", None),
                status="finish",
                task_source=getattr(task, "task_source", ""),
                department_id=getattr(task, "department_id", ""),
                waste_stream=getattr(task, "waste_stream", ""),
                waste_volume_m3=getattr(task, "waste_volume_m3", 0.0),
                container_type=getattr(task, "container_type", ""),
                amr_inventory_space=(
                    planned_space if getattr(task, "is_idle_return", False) else ""
                ),
            )

            self.completed_task_records.append(
                {
                    "task_id": task.id,
                    "pickup": task.pickup,
                    "dropoff": task.dropoff,
                    "payload": (
                        "" if self.scenario_mode and not self.scenario_enhanced_logging
                        else self._payload_log_name(task.payload)
                    ),
                    "payload_instance_id": (
                        "" if self.scenario_mode and not self.scenario_enhanced_logging
                        else getattr(task, "payload_instance_id", "")
                    ),
                    "amr_id": event.payload["amr_id"],
                    "start_datetime": self.clock.format_sim_time(
                        event.payload["start_time"]
                    ),
                    "finish_datetime": self.clock.format_sim_time(
                        event.payload["finish_time"]
                    ),
                    "duration_hms": format_duration(event.payload["duration"]),
                    "target_duration_hms": (
                        format_duration(event.payload["target_time"])
                        if event.payload.get("target_time", 0.0) > 0
                        else ""
                    ),
                    "overrun": (
                        event.payload["duration"]
                        > event.payload.get("target_time", 0.0)
                        if event.payload.get("target_time", 0.0) > 0
                        else False
                    ),
                    "overrun_sec": (
                        round(
                            event.payload["duration"] - event.payload["target_time"], 3
                        )
                        if event.payload.get("target_time", 0.0) > 0
                        and event.payload["duration"] > event.payload["target_time"]
                        else 0.0
                    ),
                    "duration_sec": round(float(event.payload["duration"]), 3),
                    "distance_m": round(sum(float(seg.get("distance_m", 0.0) or 0.0) for seg in event.payload.get("segments", []) or []), 3),
                    "energy_kwh": round(event.payload["energy_kwh"], 4),
                    "payload_orientation": str(event.payload.get("payload_orientation", getattr(task, "payload_orientation", "")) or ""),
                    "wash_cycle_required": bool(getattr(task, "wash_cycle_required", False)),
                    "scenario_name": self.scenario_name,
                    "battery_soc_after": round(event.payload["battery_soc_after"], 2),
                    "segments": event.payload["segments"],
                    "lift_energy_kwh": round(
                        event.payload.get("lift_energy_kwh", 0.0), 4
                    ),
                    "lift_empty_sec_total": round(
                        event.payload.get("lift_empty_sec_total", 0.0), 3
                    ),
                    "lift_loaded_sec_total": round(
                        event.payload.get("lift_loaded_sec_total", 0.0), 3
                    ),
                    "tracked_item_exchange": bool(
                        getattr(task, "tracked_item_exchange", False)
                    ),
                    "exchange_mode": str(getattr(task, "exchange_mode", "") or ""),
                    "tracked_item_source_payload": str(
                        getattr(task, "tracked_item_source_payload", "") or ""
                    ),
                    "tracked_items": getattr(task, "tracked_items", {}) or {},
                }
            )

            self._schedule_configured_return_task(
                task, event.payload["finish_time"], event.payload.get("amr_id", "")
            )
            self._notify_task_generation_state(task, "completed")

            target_time = event.payload.get("target_time", 0.0)
            actual_duration = event.payload["duration"]

            if target_time > 0 and actual_duration > target_time:
                self.push_event(
                    event.time,
                    "task_overrun",
                    {
                        "task": task,
                        "amr_id": event.payload["amr_id"],
                        "actual_duration": actual_duration,
                        "target_time": target_time,
                        "overrun_duration": actual_duration - target_time,
                        "start_time": event.payload["start_time"],
                        "finish_time": event.payload["finish_time"],
                    },
                )
            self._try_assign_tasks(event.time, force_idle_return=True)

        elif event.event_type == "multi_stop_complete":
            tasks: List[Task] = event.payload["tasks"]
            for task in tasks:
                payload_obj = self._payload_for_task(task)
                if payload_obj is not None and not is_empty_payload_name(task.payload):
                    try:
                        self._pickup_payload_instance_for_task(task)
                    except RuntimeError as exc:
                        self._fail_task(
                            task, str(exc), now=event.payload["finish_time"]
                        )
                        continue
                    self._free_inventory_space_for_pickup(task, payload_obj)
                    skip_dropoff_payload_store = bool(
                        getattr(task, "return_same_payload_instance", False)
                        and self._location_has_inventory_mass_collection_rotation(
                            task.dropoff, payload_obj.name
                        )
                    )
                    if (
                        not skip_dropoff_payload_store
                        and self._location_has_payload_inventory_spaces(task.dropoff)
                    ):
                        claimed_space = self._reserve_inventory_space_for_task(
                            task, payload_obj
                        )
                        if (
                            claimed_space is None
                            and not str(
                                getattr(task, "assigned_inventory_space", "") or ""
                            ).strip()
                        ):
                            self._fail_task(
                                task,
                                self._inventory_pending_reason(
                                    task.dropoff, payload_obj
                                ),
                                now=event.payload["finish_time"],
                            )
                            continue
                    if not skip_dropoff_payload_store:
                        self._store_payload_instance_for_task(task)
                        if not self._occupy_inventory_space_for_completed_task(
                            task, payload_obj
                        ):
                            self._fail_task(
                                task,
                                str(
                                    getattr(task, "pending_reason", "")
                                    or self._inventory_pending_reason(
                                        task.dropoff, payload_obj
                                    )
                                ),
                                now=event.payload["finish_time"],
                            )
                            continue

                final_location_name = str(
                    event.payload.get("end_location") or task.dropoff or ""
                )
                final_amr = self.amrs_by_id.get(event.payload.get("amr_id"))
                if final_amr is not None:
                    self._occupy_amr_inventory_space(final_amr, final_location_name)
                    final_location = self._amr_display_location(
                        final_amr, final_location_name
                    )
                else:
                    final_location = self.locations.get(final_location_name)

                self.log_step(
                    event_time=event.payload["finish_time"],
                    event_type="multi_stop_task_complete",
                    task_id=task.id,
                    amr_id=event.payload["amr_id"],
                    details=(
                        f"Task {task.id} completed as part of a multi-stop route; "
                        f"planned_origin={task.pickup}; planned_destination={task.dropoff}; "
                        f"amr_final_location={final_location_name}"
                    ),
                    from_location=final_location_name,
                    to_location=final_location_name,
                    payload_name=self._payload_log_name(task.payload),
                    payload_instance_id=getattr(task, "payload_instance_id", ""),
                    duration_sec=0.0,
                    wait_time_sec=0.0,
                    distance_m=0.0,
                    start_time=event.payload["finish_time"],
                    end_time=event.payload["finish_time"],
                    start_node=final_location_name,
                    end_node=final_location_name,
                    start_x=getattr(final_location, "x", None),
                    start_y=getattr(final_location, "y", None),
                    start_floor=getattr(final_location, "floor", None),
                    end_x=getattr(final_location, "x", None),
                    end_y=getattr(final_location, "y", None),
                    end_floor=getattr(final_location, "floor", None),
                    status="finish",
                    task_source=getattr(task, "task_source", ""),
                    department_id=getattr(task, "department_id", ""),
                    waste_stream=getattr(task, "waste_stream", ""),
                    waste_volume_m3=getattr(task, "waste_volume_m3", 0.0),
                    container_type=getattr(task, "container_type", ""),
                )

                self.completed_task_records.append(
                    {
                        "task_id": task.id,
                        "pickup": task.pickup,
                        "dropoff": task.dropoff,
                        "payload": ("" if self.scenario_mode and not self.scenario_enhanced_logging else self._payload_log_name(task.payload)),
                        "payload_instance_id": ("" if self.scenario_mode and not self.scenario_enhanced_logging else getattr(task, "payload_instance_id", "")),
                        "amr_id": event.payload["amr_id"],
                        "start_datetime": self.clock.format_sim_time(
                            event.payload["start_time"]
                        ),
                        "finish_datetime": self.clock.format_sim_time(
                            event.payload["finish_time"]
                        ),
                        "duration_hms": format_duration(event.payload["duration"]),
                        "target_duration_hms": (
                            format_duration(task.target_time)
                            if getattr(task, "target_time", 0.0) > 0
                            else ""
                        ),
                        "overrun": (
                            event.payload["duration"]
                            > getattr(task, "target_time", 0.0)
                            if getattr(task, "target_time", 0.0) > 0
                            else False
                        ),
                        "overrun_sec": (
                            round(event.payload["duration"] - task.target_time, 3)
                            if getattr(task, "target_time", 0.0) > 0
                            and event.payload["duration"] > task.target_time
                            else 0.0
                        ),
                        "duration_sec": round(float(event.payload["duration"]), 3),
                        "distance_m": round(sum(float(seg.get("distance_m", 0.0) or 0.0) for seg in event.payload.get("segments", []) or []), 3),
                        "energy_kwh": round(event.payload["energy_kwh"], 4),
                        "payload_orientation": str(getattr(task, "payload_orientation", "") or ""),
                        "wash_cycle_required": bool(getattr(task, "wash_cycle_required", False)),
                        "scenario_name": self.scenario_name,
                        "battery_soc_after": round(
                            event.payload["battery_soc_after"], 2
                        ),
                        "segments": event.payload["segments"],
                        "lift_energy_kwh": round(
                            event.payload.get("lift_energy_kwh", 0.0), 4
                        ),
                        "lift_empty_sec_total": round(
                            event.payload.get("lift_empty_sec_total", 0.0), 3
                        ),
                        "lift_loaded_sec_total": round(
                            event.payload.get("lift_loaded_sec_total", 0.0), 3
                        ),
                        "multi_stop": True,
                        "payload_slot": event.payload.get("slot_assignments", {}).get(
                            task.id, ""
                        ),
                        "tracked_item_exchange": bool(
                            getattr(task, "tracked_item_exchange", False)
                        ),
                        "exchange_mode": str(getattr(task, "exchange_mode", "") or ""),
                        "tracked_item_source_payload": str(
                            getattr(task, "tracked_item_source_payload", "") or ""
                        ),
                        "tracked_items": getattr(task, "tracked_items", {}) or {},
                    }
                )
                self._schedule_configured_return_task(
                    task, event.payload["finish_time"], event.payload.get("amr_id", "")
                )
                self._notify_task_generation_state(task, "completed")

            self._try_assign_tasks(event.time, force_idle_return=True)

        elif event.event_type == "task_wait":
            self.log_step(
                event_time=event.payload["start_time"],
                event_type="task_wait",
                details=event.payload["reason"],
                duration_sec=event.payload["end_time"] - event.payload["start_time"],
                start_time=event.payload["start_time"],
                end_time=event.payload["end_time"],
                status="waiting",
                energy_kwh=0.0,
            )
            self._try_assign_tasks(event.time)

        elif event.event_type == "mass_collection_visit":
            cfg = self._mass_collection_config_by_id(event.payload.get("config_id", ""))
            if cfg is not None:
                self._execute_mass_collection_visit(
                    cfg,
                    event.time,
                    str(event.payload.get("trigger", "scheduled") or "scheduled"),
                )

        elif event.event_type == "mass_collection_capacity_tick":
            cfg = self._mass_collection_config_by_id(event.payload.get("config_id", ""))
            if cfg is not None:
                self._handle_mass_collection_capacity_tick(cfg, event.time)

        elif event.event_type == "generator_tick":
            self._update_task_generators_until(event.time)
            next_tick = event.time + max(60.0, self.task_generation_interval_sec)
            if next_tick <= self.task_generation_horizon_sec:
                self.push_event(next_tick, "generator_tick", {})
            self._try_assign_tasks(event.time)

        elif event.event_type == "charge_cycle_start":
            amr = self.amrs_by_id[event.payload["amr_id"]]
            charge_location_name = str(
                event.payload.get("charge_location")
                or getattr(amr, "target_charge_location", "")
                or getattr(amr, "location_name", "")
                or getattr(self, "charge_location_name", "")
            ).strip()
            # Do not occupy the destination charging bay before the travel
            # segments have played.  Reserving happens when the charge plan is
            # created; occupancy should only start once the AMR reaches the bay.
            segment_start_time = event.time

            for segment in event.payload["travel_segments"]:
                segment_type = str(segment.get("type", "") or "").strip()
                from_node = segment.get("from", "")
                to_node = segment.get("to", "")

                lift_id = segment.get("lift_id", "")
                if not lift_id and segment_type.startswith("lift_"):
                    for key_node in (from_node, to_node):
                        if key_node:
                            for lift in self.lifts:
                                prefix = f"{lift.id}-F"
                                if key_node.startswith(prefix):
                                    lift_id = lift.id
                                    break
                            if lift_id:
                                break

                from_coords = self.graph_nodes.get(from_node)
                to_coords = self.graph_nodes.get(to_node)
                segment_start_x = segment.get("from_x", getattr(from_coords, "x", None))
                segment_start_y = segment.get("from_y", getattr(from_coords, "y", None))
                segment_start_floor = segment.get(
                    "from_floor", getattr(from_coords, "floor", None)
                )
                segment_end_x = segment.get("to_x", getattr(to_coords, "x", None))
                segment_end_y = segment.get("to_y", getattr(to_coords, "y", None))
                segment_end_floor = segment.get(
                    "to_floor", getattr(to_coords, "floor", None)
                )
                if segment_type == "lift_reposition":
                    segment_start_x = getattr(from_coords, "x", segment_start_x)
                    segment_start_y = getattr(from_coords, "y", segment_start_y)
                    segment_start_floor = getattr(
                        from_coords, "floor", segment_start_floor
                    )
                    segment_end_x = segment_start_x
                    segment_end_y = segment_start_y
                    segment_end_floor = segment_start_floor
                duration = segment.get("duration", 0.0)
                segment_end_time = segment_start_time + duration

                self.log_step(
                    event_time=segment_start_time,
                    event_type=f"segment_{segment_type}",
                    amr_id=amr.id,
                    details=json.dumps(segment, ensure_ascii=False),
                    from_location=from_node,
                    to_location=to_node,
                    duration_sec=duration,
                    distance_m=segment.get("distance_m", 0.0),
                    segment_type=segment.get("type", ""),
                    start_time=segment_start_time,
                    end_time=segment_end_time,
                    start_node=from_node,
                    end_node=to_node,
                    lift_id=lift_id,
                    start_x=segment_start_x,
                    start_y=segment_start_y,
                    start_floor=segment_start_floor,
                    end_x=segment_end_x,
                    end_y=segment_end_y,
                    end_floor=segment_end_floor,
                    status="completed",
                    energy_kwh=segment.get("energy_kwh", 0.0),
                    amr_rotation_start_deg=segment.get("amr_rotation_start_deg", None),
                    amr_rotation_end_deg=segment.get("amr_rotation_end_deg", None),
                    amr_rotation_deg=segment.get("amr_rotation_deg", None),
                )
                segment_start_time = segment_end_time

            if charge_location_name:
                amr.location_name = charge_location_name
            occupied_charger = self._occupy_amr_inventory_space(
                amr, amr.location_name, require_charger=True
            )
            if occupied_charger is None and self._location_has_any_amr_inventory_spaces(amr.location_name):
                self.failed_tasks.append({
                    "task_id": f"CHARGE-{amr.id}",
                    "reason": f"No charger-equipped AMR space available at {amr.location_name}",
                })
            charge_display_loc = self._amr_display_location(amr, amr.location_name)
            self.log_step(
                event_time=event.payload["charge_start"],
                event_type="segment_charge",
                amr_id=amr.id,
                from_location=charge_location_name,
                to_location=charge_location_name,
                duration_sec=event.payload["charge_duration"],
                segment_type="charge",
                start_time=event.payload["charge_start"],
                end_time=event.payload["charge_finish"],
                start_node=charge_location_name,
                end_node=charge_location_name,
                start_x=getattr(charge_display_loc, "x", None),
                start_y=getattr(charge_display_loc, "y", None),
                start_floor=getattr(charge_display_loc, "floor", None),
                end_x=getattr(charge_display_loc, "x", None),
                end_y=getattr(charge_display_loc, "y", None),
                end_floor=getattr(charge_display_loc, "floor", None),
                status="charging",
                energy_kwh=(
                    float(amr.battery_charge_rate_kw)
                    * float(event.payload["charge_duration"])
                    / 3600.0
                ),
                battery_soc_before=amr.battery_soc_percent,
                battery_soc_after=100.0,
                is_charging=True,
            )

        elif event.event_type == "charge_cycle_complete":
            amr = self.amrs_by_id[event.payload["amr_id"]]
            charge_location_name = str(
                event.payload.get("charge_location")
                or getattr(amr, "location_name", "")
                or getattr(amr, "target_charge_location", "")
                or getattr(self, "charge_location_name", "")
            ).strip()
            amr.total_charge_time += event.payload["charge_duration"]
            amr.charge_to_full()
            amr.is_charging = False

            self.log_step(
                event_time=event.time,
                event_type="charge_cycle_complete",
                amr_id=amr.id,
                details=f"{amr.id} fully charged",
                from_location=charge_location_name,
                to_location=charge_location_name,
                start_time=event.time,
                end_time=event.time,
                status="finish",
                energy_kwh=0.0,
                amr_inventory_space=str(
                    event.payload.get("amr_inventory_space", "") or ""
                ),
                battery_soc_before=100.0,
                battery_soc_after=amr.battery_soc_percent,
                is_charging=False,
            )

            self._try_assign_tasks(event.time, force_idle_return=True)

        elif event.event_type == "task_overrun":
            task: Task = event.payload["task"]

            self.log_step(
                event_time=event.payload["finish_time"],
                event_type="task_overrun",
                task_id=task.id,
                amr_id=event.payload["amr_id"],
                details=(
                    f"Task {task.id} exceeded target by "
                    f"{event.payload['overrun_duration']:.3f} seconds"
                ),
                from_location=task.pickup,
                to_location=task.dropoff,
                payload_name=self._payload_log_name(task.payload),
                payload_instance_id=getattr(task, "payload_instance_id", ""),
                duration_sec=event.payload["actual_duration"],
                task_duration_sec=event.payload["target_time"],
                status="overrun",
            )

    def request_stop(self):
        with self.lock:
            self.stop_requested = True

    def _staff_handling_summary(self) -> dict:
        categories = {}
        total_people = 0
        for category_key, pool in sorted(self.staff_resource_pools.items()):
            on_shift_people = self._staff_pool_on_shift_count(pool)
            shift_pattern = str(pool.get("shift_pattern", "none") or "none")
            shift_multiplier = float(pool.get("shift_multiplier", 1.0) or 1.0)
            people_required = self._staff_rostered_count(
                on_shift_people, shift_multiplier
            )
            initial_on_shift = int(pool.get("initial_people", 0) or 0)
            initial_people = self._staff_rostered_count(
                initial_on_shift, shift_multiplier
            )
            total_people += people_required
            categories[category_key] = {
                "resource_name": str(pool.get("resource_name", "") or ""),
                "initial_people": initial_people,
                "people_required": people_required,
                "on_shift_people_required": on_shift_people,
                "initial_on_shift_people": initial_on_shift,
                "shift_pattern": shift_pattern,
                "shift_multiplier": shift_multiplier,
                "assignments": list(pool.get("assignments", []) or []),
            }
        return {
            "total_people_required": total_people,
            "categories": categories,
            "assignments": list(self.staff_assignments),
        }

    @staticmethod
    def _verbose_fieldnames() -> List[str]:
        return [
            "amr_id",
            "task_id",
            "segment_type",
            "start_time",
            "end_time",
            "start_node",
            "end_node",
            "amr_location_before",
            "amr_location_after",
            "start_x",
            "start_y",
            "start_floor",
            "end_x",
            "end_y",
            "end_floor",
            "status",
            "sim_time_sec",
            "sim_datetime",
            "event_type",
            "payload",
            "payload_instance_id",
            "payload_runtime_population",
            "payload_known_instances",
            "payload_weight_kg",
            "payload_slot",
            "onboard_payloads",
            "onboard_slots",
            "multi_stop_task_ids",
            "multi_stop_pickup_count",
            "multi_stop_dropoff_count",
            "weight_kg",
            "from_location",
            "to_location",
            "lift_id",
            "duration_sec",
            "wait_time_sec",
            "distance_m",
            "energy_kwh",
            "battery_soc_before",
            "battery_soc_after",
            "is_charging",
            "amr_inventory_space",
            "inventory_space",
            "inventory_space_name",
            "amr_rotation_deg",
            "amr_rotation_start_deg",
            "amr_rotation_end_deg",
            "task_duration_sec",
            "details",
            "task_source",
            "department_id",
            "waste_stream",
            "waste_volume_m3",
            "container_type",
            "pending_reason",
            "tracked_item_exchange",
            "exchange_mode",
            "tracked_item_source_payload",
            "tracked_items",
            "person_resource",
            "person_id",
            "people_required",
            "staff_on_shift_people_required",
            "staff_shift_pattern",
            "staff_shift_team",
            "staff_shift_multiplier",
            "staff_initial_on_shift_people",
            "staff_initial_rostered_people",
            "staff_wait_for_travel_sec",
            "location_inventory_spaces_disabled",
            "location_configured_inventory_area_m2",
            "location_peak_payload_count",
            "location_peak_footprint_area_m2",
            "location_peak_volume_m3",
            "location_payload_footprint_area_m2",
            "location_payload_volume_m3",
            "location_recommended_area_m2",
            "location_recommended_volume_m3",
            "scenario_mode",
            "scenario_name",
            "scenario_delay_sec",
            "scenario_reason",
            "people_count",
            "people_groups",
            "people_speed_factor",
            "people_delay_sec",
            "route_lane_count",
            "corridor_width_m",
            "configured_corridor_width_m",
            "door_restricted",
            "door_nodes",
            "lane_width_m",
            "carrying_length_m",
            "payload_orientation",
            "wash_cycle",
            "lift_health_percent",
            "lift_operational",
            "charger_space",
        ]

    @staticmethod
    def _visualiser_fieldnames() -> List[str]:
        return [
            "amr_id",
            "task_id",
            "segment_type",
            "start_time",
            "end_time",
            "start_node",
            "end_node",
            "amr_location_before",
            "amr_location_after",
            "start_x",
            "start_y",
            "start_floor",
            "end_x",
            "end_y",
            "end_floor",
            "status",
            "sim_time_sec",
            "sim_datetime",
            "event_type",
            "payload",
            "payload_instance_id",
            "payload_slot",
            "onboard_payloads",
            "onboard_slots",
            "multi_stop_task_ids",
            "from_location",
            "to_location",
            "lift_id",
            "duration_sec",
            "wait_time_sec",
            "distance_m",
            "energy_kwh",
            "battery_soc_before",
            "battery_soc_after",
            "is_charging",
            "amr_inventory_space",
            "inventory_space",
            "inventory_space_name",
            "amr_rotation_deg",
            "amr_rotation_start_deg",
            "amr_rotation_end_deg",
            "task_duration_sec",
            "details",
            "task_source",
            "department_id",
            "waste_stream",
            "waste_volume_m3",
            "container_type",
            "pending_reason",
            "tracked_item_exchange",
            "exchange_mode",
            "tracked_item_source_payload",
            "tracked_items",
            "scenario_mode",
            "scenario_name",
            "people_count",
            "route_lane_count",
            "corridor_width_m",
            "configured_corridor_width_m",
            "door_restricted",
            "door_nodes",
            "payload_orientation",
            "wash_cycle",
            "lift_health_percent",
            "charger_space",
        ]

    def _append_verbose_row(self, row: dict) -> None:
        """Append a verbose row and stream a bounded chunk to disk when full."""
        if not self.verbose:
            return
        with self._verbose_write_lock:
            self.verbose_rows.append(row)
            if (
                self.verbose_csv_path
                and len(self.verbose_rows) >= self.verbose_row_buffer_size
            ):
                self._flush_verbose_rows_to_csv()

    def _flush_verbose_rows_to_csv(self) -> int:
        """Write the current verbose row buffer without rebuilding the CSV."""
        if not self.verbose_csv_path or not self.verbose_rows:
            return 0
        with self._verbose_write_lock:
            if not self.verbose_rows:
                return 0
            rows = self.verbose_rows
            mode = "a" if self._verbose_csv_started else "w"
            with open(self.verbose_csv_path, mode, newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=self._verbose_fieldnames(),
                    extrasaction="ignore",
                )
                if not self._verbose_csv_started:
                    writer.writeheader()
                writer.writerows(rows)
            written = len(rows)
            self._verbose_rows_written += written
            self.verbose_rows = []
            self._verbose_csv_started = True
            return written

    def _ensure_verbose_csv_header(self) -> None:
        """Create an empty verbose CSV with headers when no rows were produced."""
        if not self.verbose_csv_path or self._verbose_csv_started:
            return
        with self._verbose_write_lock:
            if self._verbose_csv_started:
                return
            with open(self.verbose_csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=self._verbose_fieldnames(),
                    extrasaction="ignore",
                )
                writer.writeheader()
            self._verbose_csv_started = True

    def _format_sim_time_cached(self, sim_time_sec: float) -> str:
        key = round(float(sim_time_sec or 0.0), 3)
        cached = self._format_sim_time_cache.get(key)
        if cached is not None:
            return cached
        if len(self._format_sim_time_cache) >= 200000:
            self._format_sim_time_cache.clear()
        value = self.clock.format_sim_time(key)
        self._format_sim_time_cache[key] = value
        return value

    def log_step(
        self,
        event_time: float,
        event_type: str = "",
        task_id: str = "",
        amr_id: str = "",
        details: str = "",
        from_location: str = "",
        to_location: str = "",
        payload_name: str = "",
        payload_instance_id: str = "",
        lift_id: str = "",
        duration_sec: float = 0.0,
        wait_time_sec: float = 0.0,
        distance_m: float = 0.0,
        task_duration_sec: float = 0.0,
        amr_location_before: str = "",
        amr_location_after: str = "",
        segment_type: str = "",
        start_time: float = 0.0,
        end_time: float = 0.0,
        start_node: str = "",
        end_node: str = "",
        start_x: Optional[float] = None,
        start_y: Optional[float] = None,
        start_floor: Optional[int] = None,
        end_x: Optional[float] = None,
        end_y: Optional[float] = None,
        end_floor: Optional[int] = None,
        status: str = "",
        energy_kwh: float = 0.0,
        task_source: str = "",
        department_id: str = "",
        waste_stream: str = "",
        waste_volume_m3: float = 0.0,
        container_type: str = "",
        pending_reason: str = "",
        payload_slot: str = "",
        onboard_payloads=None,
        onboard_slots=None,
        multi_stop_task_ids=None,
        multi_stop_pickup_count: int = 0,
        multi_stop_dropoff_count: int = 0,
        tracked_item_exchange: bool = False,
        exchange_mode: str = "",
        tracked_item_source_payload: str = "",
        tracked_items: Optional[dict] = None,
        battery_soc_before: Optional[float] = None,
        battery_soc_after: Optional[float] = None,
        is_charging: Optional[bool] = None,
        amr_inventory_space: str = "",
        inventory_space: str = "",
        amr_rotation_deg: Optional[float] = None,
        amr_rotation_start_deg: Optional[float] = None,
        amr_rotation_end_deg: Optional[float] = None,
        person_resource: str = "",
        person_id: str = "",
        people_required: int = 0,
        staff_on_shift_people_required: int = 0,
        staff_shift_pattern: str = "",
        staff_shift_team: str = "",
        staff_shift_multiplier: float = 1.0,
        staff_initial_on_shift_people: int = 0,
        staff_initial_rostered_people: int = 0,
        staff_wait_for_travel_sec: float = 0.0,
    ):
        if not self.verbose:
            return

        reduced_scenario_logging = self.scenario_mode and not self.scenario_enhanced_logging
        event_key = str(event_type or "").strip().lower()
        if reduced_scenario_logging and (
            event_key in {"location_payload_enter", "location_payload_exit", "payload_population_summary"}
            or "payload_transition" in event_key
        ):
            return
        if reduced_scenario_logging:
            payload_name = ""
            payload_instance_id = ""
            payload_slot = ""
            onboard_payloads = None
            onboard_slots = None
            tracked_item_exchange = False
            exchange_mode = ""
            tracked_item_source_payload = ""
            tracked_items = None
            container_type = ""

        segment_meta = {}
        if isinstance(details, str) and details.lstrip().startswith("{"):
            try:
                parsed_details = json.loads(details)
                if isinstance(parsed_details, dict):
                    segment_meta = parsed_details
            except Exception:
                segment_meta = {}
        if reduced_scenario_logging:
            if segment_meta:
                omitted_detail_keys = {
                    "payload",
                    "payload_name",
                    "payload_instance_id",
                    "payload_slot",
                    "slot_name",
                    "onboard_payloads",
                    "onboard_slots",
                    "tracked_items",
                    "tracked_item_source_payload",
                    "container_type",
                }
                details = json.dumps(
                    {
                        key: value
                        for key, value in segment_meta.items()
                        if str(key).strip().lower() not in omitted_detail_keys
                    },
                    ensure_ascii=False,
                )
            elif isinstance(details, str) and details:
                for known_payload_name in sorted(
                    (name for name in self.payloads if not is_empty_payload_name(name)),
                    key=len,
                    reverse=True,
                ):
                    details = details.replace(known_payload_name, "[payload omitted]")

        scenario_delay_value = max(0.0, float(segment_meta.get("scenario_delay_sec", 0.0) or 0.0))
        people_delay_value = max(0.0, float(segment_meta.get("people_delay_sec", 0.0) or 0.0))
        people_count_value = max(0, int(float(segment_meta.get("people_count", 0) or 0)))
        if amr_id:
            try:
                current_amr = self.amrs_by_id.get(amr_id)
            except Exception:
                current_amr = None
            if current_amr is not None:
                if battery_soc_after is None:
                    battery_soc_after = float(
                        getattr(current_amr, "battery_soc_percent", 0.0) or 0.0
                    )
                if battery_soc_before is None:
                    battery_soc_before = battery_soc_after
                if is_charging is None:
                    is_charging = bool(getattr(current_amr, "is_charging", False))

                # Only carry the AMR bay name on stationary/stowed rows.  Movement
                # rows with an inherited bay name are treated by the visualiser as
                # parked and therefore disappear from graph travel.
                moving_row = False
                try:
                    if (
                        start_x is not None
                        and start_y is not None
                        and end_x is not None
                        and end_y is not None
                    ):
                        moving_row = (
                            abs(float(end_x) - float(start_x)) > 1e-9
                            or abs(float(end_y) - float(start_y)) > 1e-9
                        )
                except Exception:
                    moving_row = False
                if not amr_inventory_space and not moving_row:
                    amr_inventory_space = str(
                        getattr(current_amr, "inventory_space_name", "") or ""
                    )

                # For movement rows, derive the heading from the actual row
                # coordinates unless a segment explicitly supplied a steering
                # start/end rotation.  The previous default copied the parked
                # bay rotation onto every corridor row, so AMRs stopped rotating
                # along the path.
                coordinate_heading = None
                if moving_row:
                    try:
                        coordinate_heading = math.degrees(
                            math.atan2(
                                float(end_y) - float(start_y),
                                float(end_x) - float(start_x),
                            )
                        )
                    except Exception:
                        coordinate_heading = None

                if amr_rotation_deg is None and coordinate_heading is not None:
                    amr_rotation_deg = coordinate_heading
                elif amr_rotation_deg is None:
                    try:
                        amr_rotation_deg = float(
                            getattr(current_amr, "rotation_deg", 0.0) or 0.0
                        )
                    except Exception:
                        amr_rotation_deg = 0.0

                if amr_rotation_start_deg is None:
                    amr_rotation_start_deg = amr_rotation_deg
                if amr_rotation_end_deg is None:
                    amr_rotation_end_deg = amr_rotation_deg

        self._append_verbose_row(
            {
                # Existing schema
                "sim_time_sec": round(event_time, 3),
                "sim_datetime": self._format_sim_time_cached(event_time),
                "event_type": event_type,
                "task_id": task_id,
                "amr_id": amr_id,
                "payload": self._payload_log_name(payload_name),
                "payload_instance_id": payload_instance_id,
                "from_location": from_location,
                "to_location": to_location,
                "lift_id": lift_id,
                "duration_sec": round(duration_sec, 3),
                "wait_time_sec": round(wait_time_sec, 3),
                "distance_m": round(distance_m, 3),
                "task_duration_sec": round(task_duration_sec, 3),
                "amr_location_before": amr_location_before,
                "amr_location_after": amr_location_after,
                "details": details,
                # New schema
                "segment_type": segment_type,
                "start_time": self._format_sim_time_cached(start_time),
                "end_time": self._format_sim_time_cached(end_time),
                "start_node": start_node,
                "end_node": end_node,
                "start_x": start_x,
                "start_y": start_y,
                "start_floor": start_floor,
                "end_x": end_x,
                "end_y": end_y,
                "end_floor": end_floor,
                "status": status,
                "energy_kwh": energy_kwh,
                "battery_soc_before": (
                    round(float(battery_soc_before), 2)
                    if battery_soc_before is not None
                    else ""
                ),
                "battery_soc_after": (
                    round(float(battery_soc_after), 2)
                    if battery_soc_after is not None
                    else ""
                ),
                "is_charging": bool(is_charging) if is_charging is not None else False,
                "amr_inventory_space": amr_inventory_space,
                "inventory_space": inventory_space,
                "inventory_space_name": inventory_space,
                "amr_rotation_deg": (
                    round(float(amr_rotation_deg), 3)
                    if amr_rotation_deg is not None
                    else ""
                ),
                "amr_rotation_start_deg": (
                    round(float(amr_rotation_start_deg), 3)
                    if amr_rotation_start_deg is not None
                    else ""
                ),
                "amr_rotation_end_deg": (
                    round(float(amr_rotation_end_deg), 3)
                    if amr_rotation_end_deg is not None
                    else ""
                ),
                "task_source": task_source,
                "department_id": department_id,
                "waste_stream": waste_stream,
                "waste_volume_m3": round(float(waste_volume_m3 or 0.0), 6),
                "container_type": container_type,
                "pending_reason": pending_reason,
                "payload_slot": payload_slot,
                "onboard_payloads": (
                    json.dumps(onboard_payloads, ensure_ascii=False)
                    if onboard_payloads
                    else "[]"
                ),
                "onboard_slots": (
                    json.dumps(onboard_slots, ensure_ascii=False)
                    if onboard_slots
                    else "[]"
                ),
                "multi_stop_task_ids": (
                    json.dumps(multi_stop_task_ids, ensure_ascii=False)
                    if multi_stop_task_ids
                    else "[]"
                ),
                "multi_stop_pickup_count": int(multi_stop_pickup_count or 0),
                "multi_stop_dropoff_count": int(multi_stop_dropoff_count or 0),
                "tracked_item_exchange": bool(tracked_item_exchange),
                "exchange_mode": exchange_mode,
                "tracked_item_source_payload": tracked_item_source_payload,
                "tracked_items": (
                    json.dumps(tracked_items, ensure_ascii=False)
                    if tracked_items
                    else "{}"
                ),
                "person_resource": person_resource,
                "person_id": person_id,
                "people_required": int(people_required or 0),
                "staff_on_shift_people_required": int(
                    staff_on_shift_people_required or 0
                ),
                "staff_shift_pattern": staff_shift_pattern,
                "staff_shift_team": staff_shift_team,
                "staff_shift_multiplier": float(staff_shift_multiplier or 1.0),
                "staff_initial_on_shift_people": int(
                    staff_initial_on_shift_people or 0
                ),
                "staff_initial_rostered_people": int(
                    staff_initial_rostered_people or 0
                ),
                "staff_wait_for_travel_sec": round(
                    float(staff_wait_for_travel_sec or 0.0), 3
                ),
                "scenario_mode": bool(self.scenario_mode),
                "scenario_name": self.scenario_name if self.scenario_mode else "Normal operation",
                "scenario_delay_sec": round(scenario_delay_value, 3),
                "scenario_reason": str(segment_meta.get("scenario_reason", "") or ""),
                "people_count": people_count_value,
                "people_groups": str(segment_meta.get("people_groups", "") or ""),
                "people_speed_factor": round(float(segment_meta.get("people_speed_factor", 1.0) or 1.0), 4),
                "people_delay_sec": round(people_delay_value, 3),
                "route_lane_count": int(float(segment_meta.get("route_lane_count", 0) or 0)),
                "corridor_width_m": round(float(segment_meta.get("corridor_width_m", 0.0) or 0.0), 3),
                "configured_corridor_width_m": round(float(segment_meta.get("configured_corridor_width_m", segment_meta.get("corridor_width_m", 0.0)) or 0.0), 3),
                "door_restricted": bool(segment_meta.get("door_restricted", False)),
                "door_nodes": str(segment_meta.get("door_nodes", "") or ""),
                "lane_width_m": round(float(segment_meta.get("lane_width_m", 0.0) or 0.0), 3),
                "carrying_length_m": round(float(segment_meta.get("carrying_length_m", 0.0) or 0.0), 3),
                "payload_orientation": str(segment_meta.get("payload_orientation", "") or ""),
                "wash_cycle": str(segment_type or segment_meta.get("type", "")).strip().lower() == "wash_cycle",
                "lift_health_percent": (
                    round(float(getattr(next((x for x in self.lifts if x.id == lift_id), None), "health_percent", 0.0)), 3)
                    if lift_id else ""
                ),
                "lift_operational": (
                    self._lift_health_speed_factor(next((x for x in self.lifts if x.id == lift_id), None)) > 0.0
                    if lift_id and any(x.id == lift_id for x in self.lifts) else ""
                ),
                "charger_space": bool(
                    amr_inventory_space and any(
                        self._space_name(space) == amr_inventory_space and self._inventory_space_has_charger(space)
                        for spaces in self.inventory_spaces_by_location.values() for space in spaces
                    )
                ),
            }
        )

    def _record_committed_segment_impacts(self, segments: List[dict]) -> None:
        """Accumulate operational impacts once for a committed route plan.

        Impact reporting must remain available when verbose CSV logging is disabled,
        so this is deliberately independent from ``log_step``.
        """
        for segment in segments or []:
            scenario_delay = max(0.0, float(segment.get("scenario_delay_sec", 0.0) or 0.0))
            people_delay = max(0.0, float(segment.get("people_delay_sec", 0.0) or 0.0))
            people_count = max(0, int(float(segment.get("people_count", 0) or 0)))
            if scenario_delay > 0.0:
                self.scenario_delay_sec += scenario_delay
                self.scenario_affected_segments += 1
            if people_count > 0:
                self.people_delay_sec += people_delay
                self.people_affected_segments += 1
            if str(segment.get("type", "") or "").strip().lower() == "wash_cycle":
                self.wash_cycles_completed += 1

    def _configured_charger_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for location_name in self.charge_location_names:
            spaces = self.inventory_spaces_by_location.get(location_name, [])
            amr_spaces = [space for space in spaces if bool(space.get("stores_amr", False)) or str(space.get("space_type", "")).lower() == "amr"]
            if amr_spaces:
                counts[location_name] = sum(1 for space in amr_spaces if self._inventory_space_has_charger(space))
            elif location_name in self.locations:
                # Backwards-compatible location-level charger when no bays are drawn.
                counts[location_name] = 1
        return counts

    @staticmethod
    def _peak_interval_concurrency(intervals: List[dict]) -> int:
        events = []
        for item in intervals:
            start = float(item.get("start_time", 0.0) or 0.0)
            end = float(item.get("end_time", start) or start)
            if end <= start:
                continue
            events.append((start, 1))
            events.append((end, -1))
        current = 0
        peak = 0
        # End events precede start events at identical timestamps.
        for _time_value, delta in sorted(events, key=lambda item: (item[0], item[1])):
            current += delta
            peak = max(peak, current)
        return peak

    def charger_estimate_summary(self) -> dict:
        configured_by_location = self._configured_charger_counts()
        intervals_by_location: Dict[str, List[dict]] = defaultdict(list)
        for interval in self.charge_intervals:
            intervals_by_location[str(interval.get("location", "") or "")].append(interval)
        all_intervals = list(self.charge_intervals)
        required_peak = self._peak_interval_concurrency(all_intervals)
        configured = sum(configured_by_location.values())
        duration_hours = max(float(self.current_time) / 3600.0, 1e-9)
        charge_hours = sum(max(0.0, float(x.get("end_time", 0.0)) - float(x.get("start_time", 0.0))) for x in all_intervals) / 3600.0
        location_rows = []
        for location_name in sorted(set(configured_by_location) | set(intervals_by_location)):
            intervals = intervals_by_location.get(location_name, [])
            required = self._peak_interval_concurrency(intervals)
            location_rows.append({
                "location": location_name,
                "configured_chargers": int(configured_by_location.get(location_name, 0)),
                "required_peak_concurrent_chargers": required,
                "recommended_n_plus_one": required + 1 if required > 0 else 0,
                "shortfall": max(0, required - int(configured_by_location.get(location_name, 0))),
                "charge_cycles": len(intervals),
                "charging_hours": round(sum(max(0.0, float(x.get("end_time", 0.0)) - float(x.get("start_time", 0.0))) for x in intervals) / 3600.0, 3),
            })
        return {
            "configured_chargers": configured,
            "required_peak_concurrent_chargers": required_peak,
            "recommended_n_plus_one": required_peak + 1 if required_peak > 0 else 0,
            "shortfall": max(0, required_peak - configured),
            "charge_cycles": len(all_intervals),
            "charging_hours": round(charge_hours, 3),
            "configured_utilisation_percent": round(100.0 * charge_hours / max(configured * duration_hours, 1e-9), 2) if configured else 0.0,
            "locations": location_rows,
        }

    def _append_scenario_summary_row(self) -> None:
        if not self.verbose or getattr(self, "_scenario_summary_written", False):
            return
        self._scenario_summary_written = True
        charger = self.charger_estimate_summary()
        details = {
            "scenario_mode": bool(self.scenario_mode),
            "scenario_name": self.scenario_name,
            "description": self.scenario_description,
            "enhanced_logging": bool(self.scenario_enhanced_logging),
            "scenario_delay_sec": round(self.scenario_delay_sec, 3),
            "scenario_affected_segments": self.scenario_affected_segments,
            "people_delay_sec": round(self.people_delay_sec, 3),
            "people_affected_segments": self.people_affected_segments,
            "wash_cycles": self.wash_cycles_completed,
            "completed_tasks": len(self.completed_task_records),
            "failed_tasks": len(self.failed_tasks),
            "configured_chargers": charger["configured_chargers"],
            "required_peak_concurrent_chargers": charger["required_peak_concurrent_chargers"],
            "charger_shortfall": charger["shortfall"],
        }
        self._append_verbose_row({
            "sim_time_sec": round(self.current_time, 3),
            "sim_datetime": self._format_sim_time_cached(self.current_time),
            "event_type": "scenario_impact_summary",
            "status": "summary",
            "scenario_mode": bool(self.scenario_mode),
            "scenario_name": self.scenario_name,
            "scenario_delay_sec": round(self.scenario_delay_sec, 3),
            "people_delay_sec": round(self.people_delay_sec, 3),
            "wash_cycle": self.wash_cycles_completed > 0,
            "details": json.dumps(details, ensure_ascii=False),
        })

    def write_transport_matrix_csv(self, path: str) -> str:
        output_path = Path(path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        path = str(output_path)
        grouped: Dict[Tuple[str, str], dict] = {}
        for record in self.completed_task_records:
            origin = str(record.get("pickup", "") or "")
            destination = str(record.get("dropoff", "") or "")
            key = (origin, destination)
            item = grouped.setdefault(key, {
                "origin": origin, "destination": destination, "trips": 0,
                "total_distance_m": 0.0, "total_duration_sec": 0.0,
                "total_energy_kwh": 0.0, "wash_cycle_trips": 0,
                "payloads": defaultdict(int), "amrs": set(),
            })
            item["trips"] += 1
            segments = list(record.get("segments", []) or [])
            record_task_id = str(record.get("task_id", "") or "").strip()
            carrying = False
            pickup_seen = False
            loaded_distance = 0.0
            loaded_duration = 0.0
            for segment in segments:
                seg_type = str(segment.get("type", "") or "").lower()
                raw_ids = segment.get("task_ids", segment.get("task_id", []))
                if isinstance(raw_ids, str):
                    segment_task_ids = {x.strip() for x in raw_ids.split(",") if x.strip()}
                elif isinstance(raw_ids, (list, tuple, set)):
                    segment_task_ids = {str(x).strip() for x in raw_ids if str(x).strip()}
                else:
                    segment_task_ids = set()
                applies_to_record = not segment_task_ids or record_task_id in segment_task_ids
                if seg_type == "pickup" and applies_to_record:
                    carrying = True
                    pickup_seen = True
                    loaded_duration += float(segment.get("duration", 0.0) or 0.0)
                    continue
                if seg_type == "dropoff" and carrying and applies_to_record:
                    loaded_duration += float(segment.get("duration", 0.0) or 0.0)
                    carrying = False
                    continue
                if carrying:
                    loaded_distance += float(segment.get("distance_m", 0.0) or 0.0)
                    loaded_duration += float(segment.get("duration", 0.0) or 0.0)
            item["total_distance_m"] += (
                loaded_distance
                if pickup_seen
                else float(record.get("distance_m", 0.0) or 0.0)
            )
            item["total_duration_sec"] += (
                loaded_duration
                if pickup_seen
                else float(record.get("duration_sec", 0.0) or 0.0)
            )
            item["total_energy_kwh"] += float(record.get("energy_kwh", 0.0) or 0.0)
            item["wash_cycle_trips"] += int(bool(record.get("wash_cycle_required", False)))
            payload_name = str(record.get("payload", "") or "")
            if payload_name:
                item["payloads"][payload_name] += 1
            amr_id = str(record.get("amr_id", "") or "")
            if amr_id:
                item["amrs"].add(amr_id)
        fieldnames = [
            "origin", "destination", "trips", "total_distance_m", "average_distance_m",
            "total_duration_sec", "average_duration_sec", "total_energy_kwh",
            "wash_cycle_trips", "unique_amrs", "payload_mix", "scenario_name",
        ]
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for key in sorted(grouped):
                item = grouped[key]
                trips = max(1, int(item["trips"]))
                writer.writerow({
                    "origin": item["origin"], "destination": item["destination"], "trips": item["trips"],
                    "total_distance_m": round(item["total_distance_m"], 3),
                    "average_distance_m": round(item["total_distance_m"] / trips, 3),
                    "total_duration_sec": round(item["total_duration_sec"], 3),
                    "average_duration_sec": round(item["total_duration_sec"] / trips, 3),
                    "total_energy_kwh": round(item["total_energy_kwh"], 4),
                    "wash_cycle_trips": item["wash_cycle_trips"],
                    "unique_amrs": len(item["amrs"]),
                    "payload_mix": ("" if self.scenario_mode and not self.scenario_enhanced_logging else json.dumps(dict(sorted(item["payloads"].items())), ensure_ascii=False)),
                    "scenario_name": self.scenario_name,
                })
        return str(path)

    def write_route_lengths_csv(self, path: str) -> str:
        output_path = Path(path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        path = str(output_path)
        fieldnames = [
            "origin", "destination", "origin_floor", "destination_floor",
            "route_length_m", "horizontal_length_m", "vertical_length_m",
            "lift_id", "route_nodes", "reachable",
        ]
        locations = [self.locations[name] for name in sorted(self.locations)]
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for origin in locations:
                for destination in locations:
                    if origin.name == destination.name:
                        continue
                    best = None
                    if origin.floor == destination.floor:
                        route = self._shortest_path_same_floor(origin.floor, origin.name, destination.name)
                        if route is not None:
                            nodes = [origin.name] + [edge["to"] for edge in route["edges"]]
                            best = (float(route["distance_m"]), float(route["distance_m"]), 0.0, "", nodes)
                    else:
                        for lift in self.lifts:
                            if not lift.can_serve(origin.floor, destination.floor):
                                continue
                            origin_lift = lift.location_on_floor(origin.floor)
                            destination_lift = lift.location_on_floor(destination.floor)
                            first = self._shortest_path_same_floor(origin.floor, origin.name, origin_lift.name)
                            second = self._shortest_path_same_floor(destination.floor, destination_lift.name, destination.name)
                            if first is None or second is None:
                                continue
                            horizontal = float(first["distance_m"]) + float(second["distance_m"])
                            vertical = abs(destination.floor - origin.floor) * self.floor_height_m
                            total = horizontal + vertical
                            nodes = [origin.name] + [edge["to"] for edge in first["edges"]] + [destination_lift.name] + [edge["to"] for edge in second["edges"]]
                            if best is None or total < best[0]:
                                best = (total, horizontal, vertical, lift.id, nodes)
                    if best is None:
                        writer.writerow({
                            "origin": origin.name, "destination": destination.name,
                            "origin_floor": origin.floor, "destination_floor": destination.floor,
                            "route_length_m": "", "horizontal_length_m": "", "vertical_length_m": "",
                            "lift_id": "", "route_nodes": "", "reachable": False,
                        })
                    else:
                        writer.writerow({
                            "origin": origin.name, "destination": destination.name,
                            "origin_floor": origin.floor, "destination_floor": destination.floor,
                            "route_length_m": round(best[0], 3), "horizontal_length_m": round(best[1], 3),
                            "vertical_length_m": round(best[2], 3), "lift_id": best[3],
                            "route_nodes": " -> ".join(best[4]), "reachable": True,
                        })
        return str(path)

    def write_charger_estimate_csv(self, path: str) -> str:
        output_path = Path(path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        path = str(output_path)
        summary = self.charger_estimate_summary()
        fieldnames = [
            "scope", "location", "configured_chargers", "required_peak_concurrent_chargers",
            "recommended_n_plus_one", "shortfall", "charge_cycles", "charging_hours",
            "configured_utilisation_percent", "scenario_name",
        ]
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerow({
                "scope": "overall", "location": "ALL",
                "configured_chargers": summary["configured_chargers"],
                "required_peak_concurrent_chargers": summary["required_peak_concurrent_chargers"],
                "recommended_n_plus_one": summary["recommended_n_plus_one"],
                "shortfall": summary["shortfall"], "charge_cycles": summary["charge_cycles"],
                "charging_hours": summary["charging_hours"],
                "configured_utilisation_percent": summary["configured_utilisation_percent"],
                "scenario_name": self.scenario_name,
            })
            for row in summary["locations"]:
                writer.writerow({"scope": "location", **row, "scenario_name": self.scenario_name})
        return str(path)

    def write_scenario_impact_csv(self, path: str) -> str:
        output_path = Path(path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        path = str(output_path)
        charger = self.charger_estimate_summary()
        rows = [
            ("Scenario mode", bool(self.scenario_mode), ""),
            ("Scenario", self.scenario_name, ""),
            ("Scenario-attributed delay", round(self.scenario_delay_sec, 3), "seconds"),
            ("Scenario-affected route segments", self.scenario_affected_segments, "segments"),
            ("People-attributed delay", round(self.people_delay_sec, 3), "seconds"),
            ("People-affected route segments", self.people_affected_segments, "segments"),
            ("Wash cycles", self.wash_cycles_completed, "cycles"),
            ("Completed tasks", len(self.completed_task_records), "tasks"),
            ("Failed tasks", len(self.failed_tasks), "tasks"),
            ("Configured chargers", charger["configured_chargers"], "chargers"),
            ("Peak concurrent chargers required", charger["required_peak_concurrent_chargers"], "chargers"),
            ("Charger shortfall", charger["shortfall"], "chargers"),
        ]
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["metric", "value", "unit", "description"])
            for metric, value, unit in rows:
                writer.writerow([metric, value, unit, self.scenario_description if metric == "Scenario" else ""])
        return str(path)

    def write_verbose_csv(self):
        """Flush the bounded verbose buffer and append final summary rows."""
        if not self.verbose_csv_path:
            return

        self._append_scenario_summary_row()
        if not (self.scenario_mode and not self.scenario_enhanced_logging):
            self._append_payload_population_summary_rows()
        self._append_location_space_recommendation_rows()
        self._flush_verbose_rows_to_csv()
        self._ensure_verbose_csv_header()

    def _row_is_visualiser_relevant(self, row: dict) -> bool:
        """Return True for verbose rows needed by the playback visualiser.

        Full verbose CSV remains unchanged for reporting/debugging.  This filter
        creates a smaller animation-oriented CSV containing AMR motion, lift
        movement, pickup/drop-off, charging, task assignment/generation markers,
        and physical payload inventory changes.
        """
        event_type = str(row.get("event_type", "") or "").strip().lower()
        segment_type = str(row.get("segment_type", "") or "").strip().lower()
        status = str(row.get("status", "") or "").strip().lower()
        amr_id = str(row.get("amr_id", "") or "").strip()
        lift_id = str(row.get("lift_id", "") or "").strip()
        inventory_space = str(
            row.get("inventory_space", "") or row.get("inventory_space_name", "") or ""
        ).strip()
        if event_type in {
            "location_payload_enter",
            "location_payload_exit",
            "mass_collection_visit",
            "task_generated",
            "return_task_generated",
            "waste_task_generated",
            "task_assigned",
            "multi_stop_task_assigned",
        } or event_type.endswith("_generated"):
            return True
        text = f"{event_type} {segment_type} {status}"
        if any(
            token in text
            for token in (
                "travel",
                "move",
                "movement",
                "corridor",
                "edge",
                "lift",
                "pickup",
                "pick_up",
                "dropoff",
                "drop_off",
                "deliver",
                "unload",
                "load",
                "charge",
                "wait",
                "queue",
                "board",
                "door",
            )
        ):
            return True
        if amr_id and (
            row.get("start_x") not in (None, "") or row.get("end_x") not in (None, "")
        ):
            return True
        if lift_id or inventory_space:
            return True
        return False

    def write_visualiser_csv(self, path: Optional[str] = None) -> str:
        """Write a smaller CSV intended for animation playback only.

        The full verbose CSV is streamed in bounded chunks.  Build the visualiser
        extract from that file row-by-row so long simulations do not need to load
        the full verbose dataset back into memory.
        """
        output_path = str(path or "simulation_visualiser_steps.csv")
        self.write_verbose_csv()
        fieldnames = self._visualiser_fieldnames()

        source_path = str(self.verbose_csv_path or "").strip()
        if source_path and os.path.exists(source_path):
            if os.path.abspath(source_path) == os.path.abspath(output_path):
                raise ValueError(
                    "Visualiser CSV path must differ from the full verbose CSV path"
                )
            with open(source_path, "r", newline="", encoding="utf-8") as source, open(
                output_path, "w", newline="", encoding="utf-8"
            ) as target:
                reader = csv.DictReader(source)
                writer = csv.DictWriter(
                    target, fieldnames=fieldnames, extrasaction="ignore"
                )
                writer.writeheader()
                for row in reader:
                    if self._row_is_visualiser_relevant(row):
                        writer.writerow(row)
            return output_path

        # Backwards-compatible in-memory fallback for callers that enable
        # verbose logging without providing a verbose CSV path.
        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(
                row
                for row in self.verbose_rows
                if self._row_is_visualiser_relevant(row)
            )
        return output_path

    def write_failed_tasks_csv(self, path: Optional[str] = None) -> str:
        """Write failed task diagnostics to CSV on every simulator run.

        The file is created even when there are no failures so downstream
        visualisation/reporting tools can rely on its presence and headers.
        """
        output_path = str(path or "failed_tasks.csv")
        fieldnames = [
            "sim_time_sec",
            "sim_datetime",
            "task_id",
            "reason",
            "pickup",
            "dropoff",
            "payload",
            "payload_instance_id",
            "task_source",
            "department_id",
            "waste_stream",
            "container_type",
            "pickup_exists",
            "pickup_floor",
            "pickup_x",
            "pickup_y",
            "pickup_inventory_spaces_total",
            "pickup_inventory_spaces_occupied",
            "pickup_inventory_spaces_reserved",
            "pickup_inventory_spaces_free",
            "pickup_stored_payload_count",
            "pickup_stored_matching_payload_count",
            "pickup_stored_payloads",
            "dropoff_exists",
            "dropoff_floor",
            "dropoff_x",
            "dropoff_y",
            "dropoff_inventory_spaces_total",
            "dropoff_inventory_spaces_occupied",
            "dropoff_inventory_spaces_reserved",
            "dropoff_inventory_spaces_free",
            "dropoff_compatible_spaces_total",
            "dropoff_compatible_spaces_occupied",
            "dropoff_compatible_spaces_reserved",
            "dropoff_compatible_spaces_free",
            "dropoff_stored_payload_count",
            "dropoff_stored_matching_payload_count",
            "dropoff_stored_payloads",
            "pickup_status_json",
            "dropoff_status_json",
        ]

        output = Path(output_path)
        if output.parent and str(output.parent) not in {"", "."}:
            output.parent.mkdir(parents=True, exist_ok=True)

        with open(output, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(self.failed_tasks)

        return str(output)

    def _estimate_total_sim_time(self) -> float:
        times = [0.0]

        for _, _, _, task in self._live_pending_task_items():
            times.append(task.release_time)

        for event in self.events:
            times.append(event.time)

        if self.completed_task_records:
            finish_times = [
                parse_datetime(x["finish_datetime"])
                for x in self.completed_task_records
            ]
            times.append(
                max(
                    (dt - self.clock.start_datetime).total_seconds()
                    for dt in finish_times
                )
            )

        if getattr(self, "task_generation_manager", None) and getattr(
            self.task_generation_manager, "generators", []
        ):
            times.append(getattr(self, "task_generation_horizon_sec", 0.0))

        return max(times)

    def _print_progress(self):
        now_wall = time.time()

        if now_wall - self.last_progress_update < self.progress_update_interval:
            return

        self.last_progress_update = now_wall

        elapsed = now_wall - self.wall_start_time

        total_sim = max(self.estimated_total_sim_time, self.current_time, 1e-9)
        progress = min(1.0, self.current_time / max(total_sim, 1e-9))

        bar_length = 40
        filled = int(bar_length * progress)
        bar = "#" * filled + "-" * (bar_length - filled)

        if progress > 0:
            eta = elapsed * (1 - progress) / progress
        else:
            eta = 0

        line = (
            f"\r[{bar}] "
            f"{progress*100:6.2f}% | "
            f"Sim: {self.clock.format_sim_time(self.current_time)} | "
            f"Elapsed: {elapsed:6.1f}s | "
            f"Events: {len(self.events)} | "
            f"ETA: {eta:6.1f}s | "
        )
        print(line, end="", flush=True)

    def summary(self) -> dict:
        makespan = 0.0
        if self.completed_task_records:
            finish_times = [
                parse_datetime(x["finish_datetime"])
                for x in self.completed_task_records
            ]
            makespan = max(
                (dt - self.clock.start_datetime).total_seconds() for dt in finish_times
            )

        return {
            "tick_rate": self.clock.tick_rate,
            "sim_datetime": self.clock.format_sim_time(self.current_time),
            "makespan_hms": format_duration(makespan),
            "completed_tasks": len(self.completed_task_records),
            "pending_tasks": len(self._live_pending_task_items()),
            "failed_tasks": self.failed_tasks,
            "scenario": {
                "enabled": bool(self.scenario_mode),
                "name": self.scenario_name,
                "description": self.scenario_description,
                "enhanced_logging": bool(self.scenario_enhanced_logging),
                "scenario_delay_sec": round(self.scenario_delay_sec, 3),
                "scenario_affected_segments": self.scenario_affected_segments,
                "people_delay_sec": round(self.people_delay_sec, 3),
                "people_affected_segments": self.people_affected_segments,
                "wash_cycles": self.wash_cycles_completed,
            },
            "charger_estimate": self.charger_estimate_summary(),
            "staff_payload_handling": self._staff_handling_summary(),
            "stores_payload_handling": self._staff_handling_summary()
            .get("categories", {})
            .get(
                "stores", {"people_required": 0, "initial_people": 0, "assignments": []}
            ),
            "lifts": [
                {
                    "lift_id": lift.id,
                    "current_floor": lift.current_floor,
                    "available_time": round(lift.available_time, 3),
                    "health_percent": round(lift.health_percent, 3),
                    "minimum_operational_health_percent": round(float(getattr(lift, "minimum_operational_health_percent", 0.0) or 0.0), 3),
                    "health_speed_factor": round(self._lift_health_speed_factor(lift), 4),
                    "operational": bool(self._lift_health_speed_factor(lift) > 0.0 and float(getattr(lift, "failed_until", 0.0) or 0.0) <= self.current_time),
                    "journeys_completed": lift.journeys_completed,
                    "mean_time_between_failures_hours": lift.mean_time_between_failures_hours,
                    "mean_time_to_repair_hours": lift.mean_time_to_repair_hours,
                    "failures_count": lift.failures_count,
                    "failed_until": (
                        self.clock.format_sim_time(lift.failed_until)
                        if lift.failed_until
                        else ""
                    ),
                }
                for lift in self.lifts
            ],
            "amrs": [
                {
                    "amr_id": amr.id,
                    "completed_tasks": amr.completed_tasks,
                    "current_location": amr.location_name,
                    "battery_soc_percent": round(amr.battery_soc_percent, 2),
                    "total_energy_used_kwh": round(amr.total_energy_used_kwh, 4),
                    "total_charge_time_hms": format_duration(amr.total_charge_time),
                }
                for amr in self.amrs
            ],
        }

    def short_summary(self) -> dict:
        makespan = 0.0
        if self.completed_task_records:
            finish_times = [
                parse_datetime(x["finish_datetime"])
                for x in self.completed_task_records
            ]
            makespan = max(
                (dt - self.clock.start_datetime).total_seconds() for dt in finish_times
            )

        return {
            "sim_datetime": self.clock.format_sim_time(self.current_time),
            "duration_hms": format_duration(makespan),
            "completed_tasks": len(self.completed_task_records),
            "pending_tasks": len(self._live_pending_task_items()),
            "failed_tasks": self.failed_tasks,
            "scenario_mode": bool(self.scenario_mode),
            "scenario_name": self.scenario_name,
            "scenario_delay_sec": round(self.scenario_delay_sec, 3),
            "people_delay_sec": round(self.people_delay_sec, 3),
            "wash_cycles": self.wash_cycles_completed,
            "configured_chargers": self.charger_estimate_summary().get("configured_chargers", 0),
            "peak_chargers_required": self.charger_estimate_summary().get("required_peak_concurrent_chargers", 0),
            "charger_shortfall": self.charger_estimate_summary().get("shortfall", 0),
            "staff_people_required": self._staff_handling_summary().get(
                "total_people_required", 0
            ),
            "stores_people_required": self._staff_handling_summary()
            .get("categories", {})
            .get("stores", {})
            .get("people_required", 0),
        }

    def print_summary(self):
        data = self.summary()
        print("\n=== Simulation Summary ===")
        print(json.dumps(data, indent=2))

    def print_short_summary(self):
        data = self.short_summary()
        print("\n=== Short Simulation Summary ===")
        print(json.dumps(data, indent=2))

    def print_completed_tasks(self):
        print("\n=== Completed Tasks ===")
        print(json.dumps(self.completed_task_records, indent=2))

    def _print_progress_complete(self):
        if self.wall_start_time is None:
            return

        elapsed = time.time() - self.wall_start_time
        bar_length = 40
        bar = "#" * bar_length

        print(
            f"\r[{bar}] "
            f"{100.00:6.2f}% | "
            f"Sim: {self.clock.format_sim_time(self.current_time)} | "
            f"Elapsed: {elapsed:6.1f}s | "
            f"Events: {0}",
            f"ETA: {0.0:6.1f}s | ",
            end="",
            flush=True,
        )

    def _next_pending_task_release_for_amr(self, amr: AMR) -> Optional[float]:
        feasible_release_times = []

        for _, _, _, task in self._live_pending_task_items():
            locked_amr_id = str(getattr(task, "locked_amr_id", "") or "").strip()
            if (
                locked_amr_id
                and locked_amr_id != str(getattr(amr, "id", "") or "").strip()
            ):
                continue
            if task.pickup not in self.locations or task.dropoff not in self.locations:
                continue
            payload = self._payload_for_task(task)
            if payload is None:
                continue
            if not self._amr_can_carry_payload(amr, payload):
                continue

            feasible_release_times.append(task.release_time)

        if not feasible_release_times:
            return None

        return min(feasible_release_times)

    def _amr_has_return_task_pending(self, amr: AMR) -> bool:
        for _, _, _, task in self._live_pending_task_items():
            if (
                getattr(task, "is_idle_return", False)
                and getattr(task, "amr_id", "") == amr.id
            ):
                return True
        return False

    def _amr_has_locked_work_pending(self, amr: AMR) -> bool:
        """Return True when this AMR already has a specific non-idle task reserved.

        Bin-return tasks are locked to the AMR that collected the full bin.  The
        idle-return scheduler must not insert an empty AMR-centre return ahead of
        that locked physical-bin return, otherwise the visualiser shows the AMR
        going home first and only then collecting the bin.
        """
        amr_id = str(getattr(amr, "id", "") or "").strip()
        if not amr_id:
            return False
        for _, _, _, task in self._live_pending_task_items():
            if getattr(task, "is_idle_return", False):
                continue
            locked_amr_id = str(getattr(task, "locked_amr_id", "") or "").strip()
            if locked_amr_id == amr_id:
                return True
        return False

    def _remove_pending_idle_return_tasks_for_amr(self, amr_id: str) -> None:
        """Drop queued empty home-return tasks when real locked work appears."""
        amr_id = str(amr_id or "").strip()
        if not amr_id or not self.pending_tasks:
            return

        removed = False
        for _priority, _release, _counter, task in self._live_pending_task_items():
            is_idle_for_amr = (
                bool(getattr(task, "is_idle_return", False))
                and str(getattr(task, "amr_id", "") or "").strip() == amr_id
            )
            if is_idle_for_amr:
                task_id = str(getattr(task, "id", "") or "").strip()
                if task_id:
                    self._removed_pending_task_ids.add(task_id)
                    self._mark_task_activity_changed()
                removed = True

        if removed:
            amr = self.amrs_by_id.get(amr_id)
            if amr is not None:
                self._clear_amr_inventory_space_reservations(amr)
                setattr(amr, "target_inventory_space_name", "")
                setattr(amr, "target_charge_location", "")
            self._assignment_continue_scheduled = False
            self._purge_removed_pending_task_heads()
            self._compact_pending_tasks_if_needed()

    def _purge_idle_returns_blocked_by_locked_work(self) -> None:
        """Remove queued empty home returns for AMRs with pending locked work."""
        locked_amr_ids = {
            str(getattr(task, "locked_amr_id", "") or "").strip()
            for _, _, _, task in self._live_pending_task_items()
            if not getattr(task, "is_idle_return", False)
            and str(getattr(task, "locked_amr_id", "") or "").strip()
        }
        for amr_id in locked_amr_ids:
            self._remove_pending_idle_return_tasks_for_amr(amr_id)

    def _charge_location_has_available_bay_for_amr(
        self, location_name: str, amr: AMR
    ) -> bool:
        """Return True when a configured charge location can actually stow this AMR.

        Locations without explicit AMR bays remain valid legacy charge points.
        Locations with AMR bays must have either a reservation for this AMR or
        a compatible free bay.
        """
        location_name = str(location_name or "").strip()
        if not location_name or location_name not in self.locations:
            return False
        location_has_amr_bays = self._location_has_any_amr_inventory_spaces(
            location_name
        )
        compatible_spaces = [
            space
            for space in self.inventory_spaces_by_location.get(location_name, [])
            if self._inventory_space_accepts_amr(space, amr)
        ]
        if location_has_amr_bays and not compatible_spaces:
            return False
        if compatible_spaces and (
            self._reserved_amr_inventory_space(location_name, amr) is None
            and self._find_free_amr_inventory_space(location_name, amr) is None
        ):
            return False
        return True

    def _amr_is_stowed_at_configured_charge_space(self, amr: AMR) -> bool:
        location_name = str(getattr(amr, "location_name", "") or "").strip()
        if location_name not in {
            str(x).strip()
            for x in (getattr(self, "charge_location_names", []) or [])
            if str(x).strip()
        }:
            return False
        # If there are no AMR bays at the location, legacy location-level
        # charging means being at the location is enough.
        if not self._location_has_any_amr_inventory_spaces(location_name):
            return True
        space_name = str(getattr(amr, "inventory_space_name", "") or "").strip()
        if not space_name:
            return False
        for space in self.inventory_spaces_by_location.get(location_name, []):
            if self._space_name(space) != space_name:
                continue
            if (
                str(space.get("amr_id", "") or "").strip()
                == str(getattr(amr, "id", "") or "").strip()
            ):
                return True
        return False

    def _charge_location_for_idle_return(self, amr: AMR, now: float) -> str:
        """Select the best configured charging location for an idle AMR return.

        This must consider every configured charging location, not only the
        legacy AMR-CENTRE/first charger.  If all compatible bays are occupied,
        return blank so the idle return waits instead of creating an impossible
        task to the fixed fallback.
        """
        current_loc = self.locations.get(str(getattr(amr, "location_name", "") or ""))
        if current_loc is not None:
            selected = self._select_charge_location_for_amr(amr, current_loc, now)
            if selected is not None:
                return selected.name

        for location_name in list(getattr(self, "charge_location_names", []) or []):
            if self._charge_location_has_available_bay_for_amr(location_name, amr):
                return str(location_name or "").strip()

        legacy = str(getattr(self, "charge_location_name", "") or "").strip()
        if legacy and self._charge_location_has_available_bay_for_amr(legacy, amr):
            return legacy
        return ""

    def _amr_is_at_configured_charge_location(self, amr: AMR) -> bool:
        return str(getattr(amr, "location_name", "") or "").strip() in {
            str(x).strip()
            for x in (getattr(self, "charge_location_names", []) or [])
            if str(x).strip()
        }

    def _create_idle_return_task(self, amr: AMR, now: float) -> Optional[Task]:
        charge_location = self._charge_location_for_idle_return(amr, now)
        if not charge_location or charge_location not in self.locations:
            return None

        # If the AMR is already at a configured charger, first try to stow it
        # without creating a zero-length movement.  When that bay has become
        # unavailable, continue into the atomic all-charger reservation below
        # so another configured charging location is tried immediately.
        if self._amr_is_stowed_at_configured_charge_space(amr):
            return None
        if self._amr_is_at_configured_charge_location(amr):
            if self._occupy_amr_inventory_space(amr, amr.location_name) is not None:
                return None

        self.synthetic_task_counter += 1

        task = Task(
            id=f"RETURN-{amr.id}-{self.synthetic_task_counter}",
            pickup=amr.location_name,
            dropoff=charge_location,
            payload=EMPTY_PAYLOAD_NAME,
            release_time=now,
            priority=999999,
            target_time=0.0,
            labels=["idle_charge_return"],
            route_profile=None,
        )
        task.created_during_runtime = True
        task.is_idle_return = True
        task.amr_id = amr.id
        task.locked_amr_id = amr.id
        task.target_charge_location = charge_location
        selected_location, selected_space = self._reserve_best_idle_return_destination(
            amr, task, now
        )
        if not selected_location:
            return None
        task.dropoff = selected_location
        task.target_charge_location = selected_location
        task.assigned_amr_inventory_space = str(
            (selected_space or {}).get("name", "") or ""
        )
        return task

    def _queue_idle_return_tasks(self, now: float):
        if not self.enable_idle_return:
            return

        for amr in self.amrs:
            if getattr(amr, "is_charging", False):
                continue
            if amr.available_time > now:
                continue
            if self._needs_post_task_recharge(amr):
                continue
            if self._amr_has_return_task_pending(amr):
                continue
            if self._amr_has_locked_work_pending(amr):
                continue

            next_release = self._next_pending_task_release_for_amr(amr)

            should_return = False

            if next_release is None:
                should_return = True
            elif next_release - now > self.idle_return_window_sec:
                should_return = True

            if not should_return:
                continue

            return_task = self._create_idle_return_task(amr, now)
            if return_task is not None:
                self._queue_pending_task(return_task)


class RuntimeInputThread(threading.Thread):
    def __init__(self, sim: Simulation):
        super().__init__(daemon=True)
        self.sim = sim

    def _update_task_generators_until(self, now: float):
        if not getattr(self.sim, "task_generation_manager", None):
            return

        for record in self.sim.task_generation_manager.update_until(now):
            task = record.task
            self.sim.schedule_task_release(task)

            pickup = self.sim.locations.get(record.pickup_location)
            dropoff = self.sim.locations.get(record.dropoff_location)

            self.sim.log_step(
                event_time=task.release_time,
                event_type=record.event_type,
                task_id=task.id,
                details=record.details,
                from_location=record.pickup_location,
                to_location=record.dropoff_location,
                payload_name=self.sim._payload_log_name(record.payload_name),
                payload_instance_id=getattr(task, "payload_instance_id", ""),
                duration_sec=0.0,
                wait_time_sec=0.0,
                distance_m=0.0,
                start_time=task.release_time,
                end_time=task.release_time,
                start_node=record.pickup_location,
                end_node=record.dropoff_location,
                start_x=getattr(pickup, "x", None),
                start_y=getattr(pickup, "y", None),
                start_floor=getattr(pickup, "floor", None),
                end_x=getattr(dropoff, "x", None),
                end_y=getattr(dropoff, "y", None),
                end_floor=getattr(dropoff, "floor", None),
                status="generated",
                energy_kwh=0.0,
                task_source=record.task_source,
                department_id=record.department_id,
                waste_stream=record.waste_stream,
                waste_volume_m3=record.waste_volume_m3,
                container_type=record.container_type,
                **self.sim._task_tracking_log_kwargs(task),
            )

    def run(self):
        print("\nInteractive mode enabled.")
        print("Paste a JSON task object to add a task at runtime.")
        print(
            "Use release_time (seconds from simulation start) or release_datetime (ISO format)."
        )
        print("Commands: status, quit")
        while True:
            try:
                line = input().strip()
            except EOFError:
                self.sim.request_stop()
                break

            if not line:
                continue

            if line.lower() == "quit":
                self.sim.request_stop()
                break

            if line.lower() == "status":
                self.sim.print_summary()
                continue

            if line.lower() == "short":
                self.sim.print_short_summary()
                continue

            try:
                task_dict = json.loads(line)
                required = {"id", "pickup", "dropoff", "payload"}
                missing = required - set(task_dict.keys())
                if missing:
                    print(f"Missing task fields: {sorted(missing)}")
                    continue
                task_dict.setdefault("target_time", 0.0)
                task_dict.setdefault("release_time", 0.0)
                task_dict.setdefault("quantity", 1)
                task_dict.setdefault("priority", 100)
                self.sim.add_runtime_task(task_dict)
                print(f"Task {task_dict['id']} added.")
            except Exception as exc:
                print(f"Could not add task: {exc}")


EXAMPLE_CONFIG = {
    "simulation": {"start_datetime": "2026-01-01T08:00:00", "tick_rate": 120.0},
    "building": {
        "load_unload_time_sec": 20.0,
        "floor_height_m": 4.0,
        "charge_locations": ["Stores"],
    },
    "locations": [
        {"name": "Stores", "floor": 0, "x": 0, "y": 0},
        {"name": "Pharmacy", "floor": 0, "x": 20, "y": 8},
        {"name": "Ward-1A", "floor": 1, "x": 10, "y": 2},
        {"name": "Ward-2A", "floor": 2, "x": 12, "y": 5},
        {"name": "Ward-3A", "floor": 3, "x": 16, "y": 4},
        {"name": "Lab", "floor": 2, "x": 3, "y": 15},
    ],
    "corridors": {
        "nodes": [
            {"name": "C0-A", "floor": 0, "x": 4, "y": 0},
            {"name": "C0-B", "floor": 0, "x": 10, "y": 0},
            {"name": "C0-C", "floor": 0, "x": 16, "y": 4},
            {"name": "C1-A", "floor": 1, "x": 5, "y": 2},
            {"name": "C1-B", "floor": 1, "x": 10, "y": 2},
            {"name": "C2-A", "floor": 2, "x": 5, "y": 2},
            {"name": "C2-B", "floor": 2, "x": 12, "y": 5},
            {"name": "C2-C", "floor": 2, "x": 3, "y": 15},
            {"name": "C3-A", "floor": 3, "x": 5, "y": 2},
            {"name": "C3-B", "floor": 3, "x": 16, "y": 4},
        ],
        "edges": [
            {"from": "Stores", "to": "C0-A"},
            {"from": "C0-A", "to": "C0-B"},
            {"from": "C0-B", "to": "C0-C"},
            {"from": "C0-C", "to": "Pharmacy"},
            {"from": "Lift-1-F0", "to": "C0-B"},
            {"from": "Lift-2-F0", "to": "C0-C"},
            {"from": "Lift-1-F1", "to": "C1-A"},
            {"from": "C1-A", "to": "C1-B"},
            {"from": "C1-B", "to": "Ward-1A"},
            {"from": "Lift-1-F2", "to": "C2-A"},
            {"from": "C2-A", "to": "C2-B"},
            {"from": "C2-B", "to": "Ward-2A"},
            {"from": "C2-B", "to": "C2-C"},
            {"from": "C2-C", "to": "Lab"},
            {"from": "Lift-1-F3", "to": "C3-A"},
            {"from": "C3-A", "to": "C3-B"},
            {"from": "C3-B", "to": "Ward-3A"},
        ],
        "auto_connect": False,
    },
    "route_profiles": {
        "dirty": {
            "allowed_lifts": ["Lift-2"],
            "allowed_nodes": [
                "Pharmacy",
                "Ward-2A",
                "C0-C",
                "C2-B",
                "Lift-2-F0",
                "Lift-2-F2",
            ],
            "allowed_edges": [
                ["Pharmacy", "C0-C"],
                ["C0-C", "Pharmacy"],
                ["C0-C", "Lift-2-F0"],
                ["Lift-2-F0", "C0-C"],
                ["Lift-2-F2", "C2-B"],
                ["C2-B", "Lift-2-F2"],
                ["C2-B", "Ward-2A"],
                ["Ward-2A", "C2-B"],
            ],
        }
    },
    "payloads": [
        {
            "name": "food_trolley",
            "weight_kg": 120,
            "length_m": 1.0,
            "width_m": 0.7,
            "height_m": 1.2,
        },
        {
            "name": "drugs_box",
            "weight_kg": 15,
            "length_m": 0.4,
            "width_m": 0.3,
            "height_m": 0.3,
        },
        {
            "name": "linen_cart",
            "weight_kg": 80,
            "length_m": 0.9,
            "width_m": 0.6,
            "height_m": 1.1,
        },
    ],
    "amrs": [
        {
            "id": "AMR-A",
            "quantity": 2,
            "payload_capacity_kg": 150,
            "payload_length_capacity_m": 1.2,
            "payload_width_capacity_m": 0.8,
            "payload_height_capacity_m": 1.3,
            "length_m": 0.8,
            "width_m": 0.6,
            "height_m": 1.2,
            "speed_m_per_sec": 1.2,
            "motor_power_w": 900,
            "battery_capacity_kwh": 6.5,
            "battery_charge_rate_kw": 2.2,
            "recharge_threshold_percent": 20.0,
            "battery_soc_percent": 100.0,
            "start_location": "Stores",
        }
    ],
    "lifts": [
        {
            "id": "Lift-1",
            "served_floors": [0, 1, 2, 3],
            "speed_floors_per_sec": 0.5,
            "door_time_sec": 4,
            "boarding_time_sec": 6,
            "capacity_length_m": 1.5,
            "capacity_width_m": 1.2,
            "capacity_height_m": 2.1,
            "health_percent": 100.0,
            "health_loss_per_journey_percent": 0.05,
            "mean_time_between_failures_hours": 720.0,
            "mean_time_to_repair_hours": 4.0,
            "start_floor": 0,
            "floor_locations": {
                "0": {"x": 5, "y": 2},
                "1": {"x": 5, "y": 2},
                "2": {"x": 5, "y": 2},
                "3": {"x": 5, "y": 2},
            },
        },
        {
            "id": "Lift-2",
            "served_floors": [0, 1, 2, 3],
            "speed_floors_per_sec": 0.67,
            "door_time_sec": 4,
            "boarding_time_sec": 5,
            "capacity_length_m": 1.5,
            "capacity_width_m": 1.2,
            "capacity_height_m": 2.1,
            "health_percent": 100.0,
            "health_loss_per_journey_percent": 0.05,
            "mean_time_between_failures_hours": 720.0,
            "mean_time_to_repair_hours": 4.0,
            "start_floor": 0,
            "floor_locations": {
                "0": {"x": 18, "y": 6},
                "1": {"x": 18, "y": 6},
                "2": {"x": 18, "y": 6},
                "3": {"x": 18, "y": 6},
            },
        },
    ],
    "tasks": [
        {
            "id": "T1",
            "pickup": "Stores",
            "dropoff": "Ward-1A",
            "payload": "food_trolley",
            "release_datetime": "2026-01-01T08:00:00",
            "priority": 10,
        },
        {
            "id": "T2",
            "pickup": "Pharmacy",
            "dropoff": "Ward-2A",
            "payload": "drugs_box",
            "release_datetime": "2026-01-01T08:05:00",
            "priority": 20,
        },
        {
            "id": "TEST1",
            "pickup": "Stores",
            "dropoff": "Pharmacy",
            "payload": "drugs_box",
            "release_datetime": "2026-01-01T08:00:00",
            "priority": 1,
        },
        {
            "id": "DIRTY-1",
            "pickup": "Pharmacy",
            "dropoff": "Ward-2A",
            "payload": "drugs_box",
            "labels": ["dirty"],
            "route_profile": "dirty",
            "release_datetime": "2026-01-01T08:10:00",
            "priority": 15,
        },
    ],
}


def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _find_input_task_csv_path(config: dict) -> Optional[str]:
    """Return the configured input task CSV path, when one is present.

    The simulator can be driven from JSON-only task generation or from a JSON
    configuration that references a task CSV.  When a task CSV is present, the
    default failed-task export should follow that filename, for example:
    dynamic_tasks.csv -> dynamic_tasks_failed_tasks.csv.
    """
    if not isinstance(config, dict):
        return None

    direct_keys = (
        "task_csv",
        "tasks_csv",
        "task_csv_path",
        "tasks_csv_path",
        "dynamic_tasks_csv",
        "dynamic_tasks_csv_path",
        "input_task_csv",
        "input_tasks_csv",
        "task_file",
        "tasks_file",
        "task_file_path",
        "tasks_file_path",
    )

    def clean_csv(value) -> Optional[str]:
        if not isinstance(value, str):
            return None
        text = value.strip()
        if text and text.lower().endswith(".csv"):
            return text
        return None

    for key in direct_keys:
        found = clean_csv(config.get(key))
        if found:
            return found

    for section_name in ("simulation", "task_generation", "inputs", "input", "files"):
        section = config.get(section_name)
        if not isinstance(section, dict):
            continue
        for key in direct_keys:
            found = clean_csv(section.get(key))
            if found:
                return found

    # Last-resort guarded recursive search.  Only accept CSV values where the
    # key clearly refers to tasks so unrelated CSV exports are not selected.
    def walk(value):
        if isinstance(value, dict):
            for key, child in value.items():
                key_text = str(key).lower()
                if isinstance(child, str):
                    found = clean_csv(child)
                    if found and "task" in key_text:
                        return found
                result = walk(child)
                if result:
                    return result
        elif isinstance(value, list):
            for child in value:
                result = walk(child)
                if result:
                    return result
        return None

    return walk(config)


def default_failed_tasks_csv_path(
    config: dict,
    config_path: Optional[str] = None,
    explicit_path: Optional[str] = None,
) -> str:
    """Resolve the failed-task CSV path for a simulator run.

    Explicit paths are honoured.  Otherwise, when the input configuration
    references a task CSV, the failed-task export follows that CSV's filename:
    <input_stem>_failed_tasks<input_suffix>.  If no task CSV is configured, the
    export falls back to failed_tasks.csv beside the config file when possible.
    """
    if explicit_path:
        return str(explicit_path)

    task_csv = _find_input_task_csv_path(config)
    if task_csv:
        task_path = Path(task_csv)
        if not task_path.is_absolute() and config_path:
            task_path = Path(config_path).resolve().parent / task_path
        return str(
            task_path.with_name(f"{task_path.stem}_failed_tasks{task_path.suffix}")
        )

    if config_path:
        return str(Path(config_path).resolve().parent / "failed_tasks.csv")

    return "failed_tasks.csv"


def write_example_config(path: Path):
    path.write_text(json.dumps(EXAMPLE_CONFIG, indent=2), encoding="utf-8")
    print(f"Example config written to {path}")


def main():
    parser = argparse.ArgumentParser(
        description="AMR delivery simulator with graph routing"
    )
    parser.add_argument("--config", type=str, help="Path to config JSON")
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("--write-example", type=str)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--verbose-csv", type=str, default="simulation_steps.csv")
    parser.add_argument(
        "--visualiser-csv",
        type=str,
        default=None,
        help=(
            "Optional smaller animation CSV. Use with --verbose to write only "
            "rows needed by the visualiser playback layer."
        ),
    )
    parser.add_argument(
        "--failed-tasks-csv",
        type=str,
        default=None,
        help=(
            "Optional failed task diagnostics CSV path. If omitted, the file is "
            "written by default using the input task CSV filename pattern, e.g. "
            "dynamic_tasks.csv -> dynamic_tasks_failed_tasks.csv."
        ),
    )
    parser.add_argument("--transport-matrix-csv", type=str, default=None, help="Origin-to-destination transport matrix CSV path.")
    parser.add_argument("--route-lengths-csv", type=str, default=None, help="All-pairs graph route length CSV path.")
    parser.add_argument("--charger-estimate-csv", type=str, default=None, help="Charger demand and shortfall CSV path.")
    parser.add_argument("--scenario-impact-csv", type=str, default=None, help="Scenario operational impact CSV path.")
    args = parser.parse_args()

    if args.write_example:
        write_example_config(Path(args.write_example))
        return

    if not args.config:
        raise SystemExit(
            "Please provide --config path, or use --write-example example.json first."
        )

    config = load_json(args.config)
    sim = Simulation(config, verbose=args.verbose, verbose_csv_path=args.verbose_csv)

    input_thread = None
    if args.interactive:
        input_thread = RuntimeInputThread(sim)
        input_thread.start()

    try:
        sim.run()
    except KeyboardInterrupt:
        sim.request_stop()

    # print(json.dumps(sim.summary(), indent=2))
    sim.write_verbose_csv()
    failed_tasks_csv_path = default_failed_tasks_csv_path(
        config, config_path=args.config, explicit_path=args.failed_tasks_csv
    )
    failed_tasks_csv_path = sim.write_failed_tasks_csv(failed_tasks_csv_path)
    print(f"Failed tasks CSV written to {failed_tasks_csv_path}")

    config_path = Path(args.config).resolve()
    export_paths = {
        "Transport matrix": args.transport_matrix_csv or str(config_path.with_name(f"{config_path.stem}_transport_matrix.csv")),
        "Route lengths": args.route_lengths_csv or str(config_path.with_name(f"{config_path.stem}_route_lengths.csv")),
        "Charger estimate": args.charger_estimate_csv or str(config_path.with_name(f"{config_path.stem}_charger_estimate.csv")),
        "Scenario impact": args.scenario_impact_csv or str(config_path.with_name(f"{config_path.stem}_scenario_impact.csv")),
    }
    written_exports = {
        "Transport matrix": sim.write_transport_matrix_csv(export_paths["Transport matrix"]),
        "Route lengths": sim.write_route_lengths_csv(export_paths["Route lengths"]),
        "Charger estimate": sim.write_charger_estimate_csv(export_paths["Charger estimate"]),
        "Scenario impact": sim.write_scenario_impact_csv(export_paths["Scenario impact"]),
    }
    for label, output_path in written_exports.items():
        print(f"{label} CSV written to {output_path}")

    if args.verbose:
        print(f"Verbose CSV written to {args.verbose_csv}")
        if args.visualiser_csv:
            visualiser_csv_path = sim.write_visualiser_csv(args.visualiser_csv)
            print(f"Visualiser CSV written to {visualiser_csv_path}")


if __name__ == "__main__":
    main()
